"""
Download all chapters & save each one as a CBZ.
Output: EP001-ตอนที่ 1.cbz, EP002-ตอนที่ 2.cbz, ...

Usage:
    python manga.py              # uses config.yml in the same folder
    python manga.py my.yml       # uses a custom config file

How it is fast:
    * Chrome only collects image URLs (images are blocked in the browser).
    * While Chrome scrapes chapter N+1, chapter N is already downloading,
      converting and packing in the background (pipeline).
    * Images are downloaded in parallel over one reused HTTP session.
    * Page conversion (WebP/JPEG) runs on all CPU cores.
    * Nothing touches the disk until the finished CBZ is written.
"""

import gc
import glob
import io
import os
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import requests
import yaml
from PIL import Image
from requests.adapters import HTTPAdapter
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from urllib3.util.retry import Retry

try:
    from webdriver_manager.chrome import ChromeDriverManager
    USE_WDM = True
except ImportError:
    USE_WDM = False

# Long webtoon strips are huge; never trip Pillow's decompression-bomb guard.
Image.MAX_IMAGE_PIXELS = None

WEBP_MAX_DIM = 16383    # hard limit of the WebP format
JPEG_MAX_DIM = 65500    # hard limit of the JPEG format
IMAGE_MODES = ("original", "webp", "jpeg")


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

def load_config(path: str = "config.yml") -> dict:
    if not os.path.exists(path):
        print(f"Config file not found: {path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get(cfg: dict, *keys, default=None):
    val = cfg
    for k in keys:
        if not isinstance(val, dict):
            return default
        val = val.get(k)
    return default if val is None else val


# ─────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────

# Set from config in main(): which browser to drive and where it lives.
BROWSER = {"kind": "chrome", "binary": None, "driver": None}


def make_driver(headless: bool, block_images: bool):
    kind = BROWSER["kind"]
    options = EdgeOptions() if kind == "edge" else Options()
    if BROWSER["binary"]:
        options.binary_location = BROWSER["binary"]
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--js-flags=--max-old-space-size=512")
    options.add_argument("--renderer-process-limit=1")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--disk-cache-size=33554432")
    options.add_argument("--media-cache-size=33554432")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    )
    # Return as soon as the DOM is ready instead of waiting for every asset.
    options.page_load_strategy = "eager"
    if block_images:
        # We download the images ourselves, so the browser doesn't need to.
        options.add_argument("--blink-settings=imagesEnabled=false")

    driver_cls = webdriver.Edge if kind == "edge" else webdriver.Chrome
    service_cls = EdgeService if kind == "edge" else Service

    if BROWSER["driver"]:
        # Use the driver you installed yourself (e.g. from scoop).
        return driver_cls(service=service_cls(BROWSER["driver"]), options=options)
    try:
        # Selenium Manager (built into selenium >= 4.6) finds/downloads the driver.
        return driver_cls(options=options)
    except Exception as e:
        if kind != "chrome" or not USE_WDM:
            raise
        print(f"Selenium Manager failed ({type(e).__name__}); trying webdriver-manager...")
        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=options)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def safe_filename(name: str) -> str:
    name = name.split("\n")[0].strip()
    name = re.sub(r'[\\/*?:"<>|\r\t]', "-", name)
    name = re.sub(r'-+', "-", name)
    name = re.sub(r'\s+', " ", name)
    return name.strip(" -")


def human_size(n: float) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def sniff_ext(data: bytes):
    """Identify an image by its magic bytes. Returns None if not an image."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    return None


# ─────────────────────────────────────────────
# Scraping (Selenium only collects URLs)
# ─────────────────────────────────────────────

CHAPTER_SELECTORS = [
    ".eplister ul li a",
    ".wp-manga-chapter a",
    ".chapter-link",
    "li.wp-manga-chapter a",
]

IMG_SELECTORS = ["#readerarea img", ".reading-content img", "img[data-src]"]

# One round-trip for the whole list instead of one WebDriver call per element.
JS_LINKS = (
    "return Array.from(document.querySelectorAll(arguments[0]))"
    ".map(e => [e.href, (e.innerText || e.textContent || '').trim()]);"
)

JS_COLLECT = """
const sels = arguments[0];
for (const s of sels) {
  const els = document.querySelectorAll(s);
  if (els.length) {
    return Array.from(els).map(e => [
      e.getAttribute('data-src') || e.getAttribute('data-lazy-src')
        || e.getAttribute('data-original') || '',
      e.getAttribute('src') || ''
    ]);
  }
}
return [];
"""


def _wait_any(driver, selectors: list, timeout: float):
    css = ", ".join(selectors)
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, css))
        )
    except Exception:
        pass


def get_series_title(driver) -> str:
    for sel in ("h1.entry-title", "h1.post-title", "h1"):
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        if els and els[0].text.strip():
            return safe_filename(els[0].text)
    return safe_filename(driver.title.split(" - ")[0]) or "Manhwa"


def get_chapter_links(driver, manga_url: str) -> tuple:
    print(f"\nFetching chapter list from: {manga_url}")
    driver.get(manga_url)
    _wait_any(driver, CHAPTER_SELECTORS, 10)

    links = []
    for sel in CHAPTER_SELECTORS:
        rows = driver.execute_script(JS_LINKS, sel)
        if rows:
            links = [(href, text) for href, text in rows]
            break

    if not links:
        print("Could not find chapter links automatically.")
        print("Open the manga page, inspect a chapter link, and update CHAPTER_SELECTORS.")
        return [], ""

    links = [(url, title) for url, title in reversed(links) if url]
    series = get_series_title(driver)
    print(f"Found {len(links)} chapters of: {series}")
    return links, series


def collect_srcs(driver, base_url: str) -> tuple:
    """
    Read every <img> URL in one browser call.
    Returns (urls, pending); pending=True means some images still have a
    placeholder (lazy loading) and the page needs more scrolling.
    """
    rows = driver.execute_script(JS_COLLECT, IMG_SELECTORS)
    srcs, pending = [], False
    for lazy, src in rows:
        cand = lazy or src
        if not cand or cand.startswith("data:"):
            pending = True
            continue
        cand = urljoin(base_url, cand)
        low = cand.lower()
        if cand.startswith("http") and ".svg" not in low and "logo" not in low:
            srcs.append(cand)
    return srcs, pending


def _settle(driver, base_url: str, max_wait: float) -> tuple:
    """Poll until the image list stops changing (instead of a fixed sleep)."""
    deadline = time.time() + max_wait
    last, srcs, pending = -1, [], True
    while time.time() < deadline:
        srcs, pending = collect_srcs(driver, base_url)
        if srcs and not pending and len(srcs) == last:
            break
        last = len(srcs)
        time.sleep(0.25)
    return srcs, pending


def _scroll_fully(driver, base_url: str, scroll_wait: float) -> list:
    """Slow path: scroll viewport by viewport until nothing new appears."""
    pos, stable, last_count = 0, 0, -1
    for _ in range(400):
        height = driver.execute_script("return document.body.scrollHeight")
        view = driver.execute_script("return window.innerHeight")
        pos = min(pos + view, height)
        driver.execute_script("window.scrollTo(0, arguments[0]);", pos)
        time.sleep(scroll_wait)
        if pos >= height:
            srcs, pending = collect_srcs(driver, base_url)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == height and not pending and len(srcs) == last_count:
                stable += 1
            else:
                stable = 0
            last_count = len(srcs)
            if stable >= 2:
                break
    driver.execute_script("window.scrollTo(0, 0);")
    return collect_srcs(driver, base_url)[0]


def get_image_srcs(driver, url: str, page_wait: float, scroll_wait: float) -> list:
    """Load a chapter page and return its image URLs in reading order."""
    driver.get(url)
    _wait_any(driver, IMG_SELECTORS, 15)
    base = driver.current_url

    srcs, pending = _settle(driver, base, page_wait)

    # Fast path check: jump to the bottom once. If nothing new shows up and no
    # placeholders remain, we're done. Otherwise fall back to a full scroll.
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(scroll_wait)
    more, pending2 = collect_srcs(driver, base)
    if pending or pending2 or len(more) != len(srcs):
        srcs = _scroll_fully(driver, base, scroll_wait)
    else:
        srcs = more

    gc.collect()
    return srcs


# ─────────────────────────────────────────────
# Download
# ─────────────────────────────────────────────

def make_session(manga_url: str, referer, workers: int) -> requests.Session:
    if not referer:
        p = urlparse(manga_url)
        referer = f"{p.scheme}://{p.netloc}/"
    session = requests.Session()
    session.headers.update({
        "Referer": referer,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        ),
    })
    retry = Retry(
        total=4, backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(
        max_retries=retry, pool_connections=16, pool_maxsize=max(workers, 4)
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch(session: requests.Session, src: str):
    """Download one image into memory. Returns bytes, or None on failure."""
    try:
        resp = session.get(src, timeout=(10, 30))
        resp.raise_for_status()
        data = resp.content
        if not data:
            raise ValueError("empty response")
        if sniff_ext(data) is None:
            # Unknown signature: only accept it if Pillow can really read it
            # (catches HTML error pages served with an image Content-Type).
            Image.open(io.BytesIO(data)).verify()
        return data
    except Exception as e:
        print(f"      Failed ...{src[-60:]}: {e}")
        return None


# ─────────────────────────────────────────────
# Image conversion (runs in worker processes)
# ─────────────────────────────────────────────

def _flatten(img: Image.Image) -> Image.Image:
    """Composite transparency onto white (JPEG has no alpha channel)."""
    rgba = img.convert("RGBA")
    bg = Image.new("RGB", rgba.size, (255, 255, 255))
    bg.paste(rgba, mask=rgba.getchannel("A"))
    return bg


def convert_page(args: tuple) -> tuple:
    """
    Convert one page. Returns (bytes, extension).
    Never makes a page worse: if conversion fails or isn't smaller, the
    original bytes are kept untouched.
    """
    data, mode, quality, method, max_width = args
    orig_ext = sniff_ext(data) or "jpg"

    if mode == "original":
        return data, orig_ext

    try:
        with Image.open(io.BytesIO(data)) as img:
            if getattr(img, "is_animated", False):
                return data, orig_ext
            src_fmt = img.format
            img.load()
            icc = img.info.get("icc_profile")

            resized = False
            if max_width and img.width > max_width:
                new_h = max(1, round(img.height * max_width / img.width))
                img = img.resize((max_width, new_h), Image.LANCZOS)
                resized = True

            out = io.BytesIO()
            if mode == "webp":
                if src_fmt == "WEBP" and not resized:
                    return data, orig_ext
                if max(img.size) > WEBP_MAX_DIM:
                    return data, orig_ext
                if img.mode not in ("RGB", "RGBA"):
                    has_alpha = img.mode in ("LA", "PA") or "transparency" in img.info
                    img = img.convert("RGBA" if has_alpha else "RGB")
                kw = {"format": "WEBP", "method": method}
                if src_fmt == "JPEG":
                    kw["quality"] = quality
                else:
                    # PNG/GIF/BMP sources: lossless WebP is much smaller than PNG
                    kw.update(lossless=True, quality=100)
                if icc:
                    kw["icc_profile"] = icc
                img.save(out, **kw)
                new_ext = "webp"
            else:  # jpeg
                if max(img.size) > JPEG_MAX_DIM:
                    return data, orig_ext
                if img.mode not in ("RGB", "L"):
                    img = _flatten(img)
                kw = {"format": "JPEG", "quality": quality,
                      "optimize": True, "progressive": True}
                if icc:
                    kw["icc_profile"] = icc
                img.save(out, **kw)
                new_ext = "jpg"

            new = out.getvalue()
            if not resized and len(new) >= len(data):
                return data, orig_ext
            return new, new_ext
    except Exception:
        return data, orig_ext


# ─────────────────────────────────────────────
# CBZ creation
# ─────────────────────────────────────────────

def comic_info_xml(series: str, title: str, number: int, page_count: int, web: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<ComicInfo>\n"
        f"  <Series>{escape(series)}</Series>\n"
        f"  <Title>{escape(title)}</Title>\n"
        f"  <Number>{number}</Number>\n"
        f"  <PageCount>{page_count}</PageCount>\n"
        f"  <Web>{escape(web)}</Web>\n"
        "</ComicInfo>\n"
    )


def write_cbz(path: str, pages: list, info_xml):
    """
    A CBZ is a ZIP of images. Images are already compressed, so ZIP_STORED
    (no compression) is faster and the same size. Written to .part first
    so a crash never leaves a half-finished file that looks complete.
    """
    tmp = path + ".part"
    width = max(3, len(str(len(pages))))
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for i, (data, ext) in enumerate(pages, start=1):
            z.writestr(f"{i:0{width}d}.{ext}", data)
        if info_xml:
            z.writestr("ComicInfo.xml", info_xml)
    os.replace(tmp, path)


# ─────────────────────────────────────────────
# One chapter: download -> convert -> pack  (runs in a background thread)
# ─────────────────────────────────────────────

def build_chapter(S, ep_num: int, title: str, url: str, srcs: list, out_path: str) -> tuple:
    """Returns (ep_num, ok, message). Never raises."""
    t0 = time.time()
    try:
        raw = [None] * len(srcs)
        for _attempt in range(3):          # initial pass + 2 retries of failures
            missing = [i for i, d in enumerate(raw) if d is None]
            if not missing:
                break
            got = list(S.dl_pool.map(lambda i: fetch(S.session, srcs[i]), missing))
            for i, d in zip(missing, got):
                raw[i] = d

        failed = sum(1 for d in raw if d is None)
        if failed:
            return ep_num, False, f"{failed}/{len(srcs)} pages failed to download (chapter not saved)"

        orig_total = sum(len(d) for d in raw)
        jobs = [(d, S.mode, S.quality, S.method, S.max_width) for d in raw]
        del raw
        if S.conv_pool:
            pages = list(S.conv_pool.map(convert_page, jobs, chunksize=2))
        else:
            pages = [convert_page(j) for j in jobs]
        del jobs

        info = comic_info_xml(S.series, title, ep_num, len(pages), url) if S.comicinfo else None
        write_cbz(out_path, pages, info)

        if S.keep_images:
            folder = out_path[:-4] + "_pages"
            os.makedirs(folder, exist_ok=True)
            width = max(3, len(str(len(pages))))
            for i, (data, ext) in enumerate(pages, start=1):
                with open(os.path.join(folder, f"{i:0{width}d}.{ext}"), "wb") as f:
                    f.write(data)

        new_total = os.path.getsize(out_path)
        change = (new_total / orig_total - 1) * 100 if orig_total else 0
        msg = (f"{len(pages)} pages, {human_size(orig_total)} -> {human_size(new_total)} "
               f"({change:+.0f}%) in {time.time() - t0:.1f}s")
        return ep_num, True, msg
    except Exception as e:
        return ep_num, False, f"{type(e).__name__}: {e}"


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def _on_chapter_done(slots, ep_num, fut):
    slots.release()
    _, ok, msg = fut.result()
    print(f"   [{'done' if ok else 'FAIL'}] EP{ep_num}: {msg}")


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yml"
    cfg = load_config(config_path)

    manga_url     = get(cfg, "manga",   "url")
    chapter_start = get(cfg, "manga",   "chapter_start")
    chapter_end   = get(cfg, "manga",   "chapter_end")
    referer       = get(cfg, "manga",   "referer")
    output_dir    = get(cfg, "output",  "dir",              default="manga_output")
    keep_images   = get(cfg, "output",  "keep_images",      default=False)
    comicinfo     = get(cfg, "output",  "comicinfo",        default=True)
    image_format  = str(get(cfg, "output", "image_format",  default="webp")).lower()
    # "jpeg_quality" is the old key name; still accepted.
    quality       = int(get(cfg, "output", "quality",
                            default=get(cfg, "output", "jpeg_quality", default=90)))
    webp_method   = int(get(cfg, "output", "webp_method",   default=6))
    max_width     = int(get(cfg, "output", "max_image_width", default=0))
    headless      = get(cfg, "browser", "headless",         default=True)
    BROWSER["kind"]   = str(get(cfg, "browser", "type", default="chrome")).lower()
    BROWSER["binary"] = get(cfg, "browser", "binary_path")
    BROWSER["driver"] = get(cfg, "browser", "driver_path")
    restart_every = int(get(cfg, "browser", "restart_every", default=25))
    block_images  = get(cfg, "browser", "block_images",     default=True)
    dl_workers    = int(get(cfg, "speed", "download_workers", default=16))
    conv_workers  = int(get(cfg, "speed", "convert_workers",  default=0))
    chapter_par   = int(get(cfg, "speed", "parallel_chapters", default=3))
    scroll_wait   = float(get(cfg, "speed", "scroll_wait",    default=0.4))
    page_wait     = float(get(cfg, "speed", "page_wait",      default=3.0))
    chapter_delay = float(get(cfg, "speed", "chapter_delay",  default=0.5))

    if not manga_url or manga_url == "URL-HERE":
        print("manga.url is not set in config.yml")
        sys.exit(1)
    if image_format not in IMAGE_MODES:
        print(f"output.image_format must be one of {IMAGE_MODES}, got '{image_format}'")
        sys.exit(1)
    if BROWSER["kind"] not in ("chrome", "edge"):
        print(f"browser.type must be 'chrome' or 'edge', got '{BROWSER['kind']}'")
        sys.exit(1)
    if conv_workers <= 0:
        conv_workers = max(1, (os.cpu_count() or 4) - 2)
    needs_conversion = image_format != "original"

    print(f"Pages: {image_format}"
          + (f" (quality {quality})" if needs_conversion else " (untouched originals)")
          + (f", max width {max_width}px" if needs_conversion and max_width else ""))
    print(f"Downloads: {dl_workers} parallel | chapters in flight: {chapter_par}"
          + (f" | convert processes: {conv_workers}" if needs_conversion else ""))

    os.makedirs(output_dir, exist_ok=True)
    for leftover in glob.glob(os.path.join(output_dir, "*.part")):
        os.remove(leftover)             # unfinished files from a crashed run

    S = SimpleNamespace(
        session=make_session(manga_url, referer, dl_workers),
        dl_pool=ThreadPoolExecutor(max_workers=dl_workers),
        conv_pool=ProcessPoolExecutor(max_workers=conv_workers) if needs_conversion else None,
        mode=image_format, quality=quality, method=webp_method, max_width=max_width,
        comicinfo=comicinfo, keep_images=keep_images, series="Manhwa",
    )
    chapter_pool = ThreadPoolExecutor(max_workers=chapter_par)
    slots = threading.BoundedSemaphore(chapter_par + 1)   # limits memory held in flight
    futures = []
    failed = []
    t_start = time.time()

    driver = make_driver(headless, block_images)
    try:
        chapters, S.series = get_chapter_links(driver, manga_url)
        if not chapters:
            return

        start = int(chapter_start) if chapter_start is not None else 1
        end   = int(chapter_end)   if chapter_end   is not None else len(chapters)
        start = max(1, min(start, len(chapters)))
        end   = max(1, min(end,   len(chapters)))
        if start > end:
            print(f"chapter_start ({start}) > chapter_end ({end}), nothing to do.")
            return

        selected = chapters[start - 1 : end]
        pad = len(str(len(chapters)))
        print(f"Downloading EP{start} to EP{end} ({len(selected)} chapters)")

        scraped = 0
        for ep_num, (url, title) in zip(range(start, end + 1), selected):
            clean_title = safe_filename(title) if title else f"chapter-{ep_num}"
            out_path = os.path.join(output_dir, f"EP{ep_num:0{pad}d}-{clean_title}.cbz")

            if os.path.exists(out_path):
                print(f"EP{ep_num} already exists, skipping.")
                continue

            print(f"EP{ep_num}/{len(chapters)} - {clean_title}: collecting pages...")
            srcs = get_image_srcs(driver, url, page_wait, scroll_wait)

            if not srcs and block_images:
                # Some sites only reveal images once the browser loads them.
                print("   No images found - retrying with browser images enabled")
                driver.quit()
                driver = make_driver(headless, False)
                block_images = False
                srcs = get_image_srcs(driver, url, page_wait, scroll_wait)

            if not srcs:
                print(f"   EP{ep_num}: no images found, skipping.")
                failed.append(ep_num)
                continue

            # Hand the chapter to the background pipeline and move straight on.
            slots.acquire()
            fut = chapter_pool.submit(
                build_chapter, S, ep_num, clean_title, url, srcs, out_path
            )
            fut.add_done_callback(
                lambda f, _ep=ep_num: _on_chapter_done(slots, _ep, f)
            )
            futures.append((ep_num, fut))

            driver.get("about:blank")
            scraped += 1
            if restart_every and scraped % restart_every == 0:
                print(f"Restarting Chrome at EP{ep_num} to free memory...")
                driver.quit()
                driver = make_driver(headless, block_images)
            time.sleep(chapter_delay)

        print("\nWaiting for the last chapters to finish...")
        failed += [ep for ep, fut in futures if not fut.result()[1]]

    finally:
        try:
            driver.quit()
        except Exception:
            pass
        chapter_pool.shutdown(wait=True, cancel_futures=True)
        S.dl_pool.shutdown(wait=True, cancel_futures=True)
        if S.conv_pool:
            S.conv_pool.shutdown(wait=True, cancel_futures=True)

    elapsed = time.time() - t_start
    print(f"\nAll done in {elapsed:.0f}s! CBZ files saved in: ./{output_dir}/")
    if failed:
        print("These chapters failed - run again to retry them: "
              + ", ".join(f"EP{ep}" for ep in sorted(failed)))
        sys.exit(1)


if __name__ == "__main__":
    main()
