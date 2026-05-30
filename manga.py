"""
up-manga.com — Download all chapters & save each as a PDF
Output: EP1-ตอนที่-1.pdf, EP2-ตอนที่-2.pdf, ...

Usage:
    python manga.py              # uses config.yml in the same folder
    python manga.py my.yml       # uses a custom config file
"""

import gc
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import img2pdf
import requests
import yaml
from PIL import Image
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    from webdriver_manager.chrome import ChromeDriverManager
    USE_WDM = True
except ImportError:
    USE_WDM = False


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

def load_config(path: str = "config.yml") -> dict:
    if not os.path.exists(path):
        print(f"Config file not found: {path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get(cfg: dict, *keys, default=None):
    val = cfg
    for k in keys:
        if not isinstance(val, dict):
            return default
        val = val.get(k, default)
    return val


# ─────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────

def make_driver(headless: bool) -> webdriver.Chrome:
    options = Options()
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
    if USE_WDM:
        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=options)
    return webdriver.Chrome(options=options)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def safe_filename(name: str) -> str:
    name = name.split("\n")[0].strip()
    name = re.sub(r'[\\/*?:"<>|\r\t]', "-", name)
    name = re.sub(r'-+', "-", name)
    name = re.sub(r'\s+', " ", name)
    return name.strip(" -")


def scroll_to_load(driver: webdriver.Chrome, scroll_wait: float):
    last = driver.execute_script("return document.body.scrollHeight")
    for _ in range(8):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(scroll_wait)
        new = driver.execute_script("return document.body.scrollHeight")
        if new == last:
            break
        last = new
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(1)


# ─────────────────────────────────────────────
# Scraping
# ─────────────────────────────────────────────

def get_chapter_links(driver: webdriver.Chrome, manga_url: str) -> list:
    print(f"\nFetching chapter list from: {manga_url}")
    driver.get(manga_url)
    time.sleep(3)

    selectors = [
        ".eplister ul li a",
        ".wp-manga-chapter a",
        ".chapter-link",
        "li.wp-manga-chapter a",
    ]
    links = []
    for sel in selectors:
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        if els:
            links = [(e.get_attribute("href"), e.text.strip()) for e in els]
            break

    if not links:
        print("Could not find chapter links automatically.")
        print("Open the manga page, inspect a chapter link, and update the CSS selectors.")
        return []

    links = [(url, title) for url, title in reversed(links) if url]
    print(f"Found {len(links)} chapters")
    return links


def get_image_srcs(driver: webdriver.Chrome, url: str, page_wait: float, scroll_wait: float) -> list:
    """Use Selenium to load the chapter page and collect all image URLs."""
    driver.get(url)

    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#readerarea img"))
        )
    except Exception:
        pass

    time.sleep(page_wait)
    scroll_to_load(driver, scroll_wait)

    imgs = (
        driver.find_elements(By.CSS_SELECTOR, "#readerarea img")
        or driver.find_elements(By.CSS_SELECTOR, ".reading-content img")
        or driver.find_elements(By.CSS_SELECTOR, "img[data-src]")
    )

    src_list = []
    for img in imgs:
        src = img.get_attribute("src") or img.get_attribute("data-src") or ""
        if src.startswith("http") and "svg" not in src and "logo" not in src.lower():
            src_list.append(src)

    imgs = None
    gc.collect()
    return src_list


# ─────────────────────────────────────────────
# Parallel image download
# ─────────────────────────────────────────────

HEADERS = {
    "Referer": "https://www.up-manga.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
}


def _download_one(args: tuple) -> tuple:
    """
    Download a single image and save it to disk.
    Returns (page_index, saved_path) on success, or (page_index, None) on failure.
    Designed to run inside a ThreadPoolExecutor worker.
    """
    i, src, img_dir = args
    ext = src.split(".")[-1].split("?")[0].lower()
    if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
        ext = "jpg"
    path = os.path.join(img_dir, f"page_{i:03}.{ext}")
    try:
        with requests.get(src, headers=HEADERS, timeout=15, stream=True) as resp:
            resp.raise_for_status()
            with open(path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
        return (i, path)
    except Exception as e:
        print(f"      Failed page {i}: {e}")
        return (i, None)


def download_chapter_images(
    driver: webdriver.Chrome,
    url: str,
    img_dir: str,
    page_wait: float,
    scroll_wait: float,
    workers: int,
) -> list:
    """
    1. Use Selenium to scrape image URLs from the chapter page.
    2. Download all images in parallel using a thread pool.
    3. Return paths sorted by page order.
    """
    os.makedirs(img_dir, exist_ok=True)

    src_list = get_image_srcs(driver, url, page_wait, scroll_wait)
    if not src_list:
        print("   No images found on page.")
        return []

    tasks = [(i, src, img_dir) for i, src in enumerate(src_list, start=1)]
    results = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_download_one, task): task[0] for task in tasks}
        done = 0
        for future in as_completed(futures):
            i, path = future.result()
            if path:
                results[i] = path
            done += 1
            pct = int(done / len(tasks) * 20)
            bar = "#" * pct + "-" * (20 - pct)
            print(f"\r   Downloading [{bar}] {done}/{len(tasks)}", end="", flush=True)

    print()
    return [results[i] for i in sorted(results)]


# ─────────────────────────────────────────────
# Image optimization  ← main space savings
# ─────────────────────────────────────────────

def optimize_image(
    path: str,
    max_width: int,
    jpeg_quality: int,
    out_dir: str,
) -> str | None:
    """
    Convert any image to an optimized JPEG:
      - Downscale if wider than max_width (preserves aspect ratio)
      - Save as JPEG at jpeg_quality (lower = smaller file, less detail)
      - Drop EXIF / metadata (saves a few KB per image)
    Returns the new path, or None if the image is unreadable.
    """
    try:
        with Image.open(path) as img:
            # Convert palette/RGBA/P modes to RGB cleanly
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

            # Downscale only if wider than the cap
            if img.width > max_width:
                ratio = max_width / img.width
                new_size = (max_width, int(img.height * ratio))
                img = img.resize(new_size, Image.LANCZOS)

            # Build the output path (always .jpg)
            basename = os.path.splitext(os.path.basename(path))[0] + ".jpg"
            out_path = os.path.join(out_dir, basename)

            img.save(
                out_path,
                "JPEG",
                quality=jpeg_quality,
                optimize=True,       # Huffman table pass — free size reduction
                progressive=True,    # Progressive JPEG — slightly smaller, faster preview
            )
        return out_path
    except Exception as e:
        print(f"   Skipping bad image {os.path.basename(path)}: {e}")
        return None


def optimize_chapter_images(
    image_paths: list,
    opt_dir: str,
    max_width: int,
    jpeg_quality: int,
    workers: int,
) -> list:
    """
    Run optimize_image in parallel across all pages of a chapter.
    Returns optimized paths in original page order.
    """
    os.makedirs(opt_dir, exist_ok=True)

    def _task(args):
        idx, path = args
        out = optimize_image(path, max_width, jpeg_quality, opt_dir)
        return idx, out

    results = {}
    tasks = list(enumerate(image_paths))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_task, t): t[0] for t in tasks}
        done = 0
        for future in as_completed(futures):
            idx, out_path = future.result()
            if out_path:
                results[idx] = out_path
            done += 1
            pct = int(done / len(tasks) * 20)
            bar = "#" * pct + "-" * (20 - pct)
            print(f"\r   Optimizing  [{bar}] {done}/{len(tasks)}", end="", flush=True)

    print()
    return [results[i] for i in sorted(results) if results[i] is not None]


# ─────────────────────────────────────────────
# PDF creation
# ─────────────────────────────────────────────

def images_to_pdf(image_paths: list, pdf_path: str):
    if not image_paths:
        print("   No images to convert.")
        return

    try:
        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert(image_paths))
        print(f"   Saved PDF -> {pdf_path}")
    except Exception as e:
        print(f"   img2pdf failed ({e}), falling back to Pillow...")
        _pillow_fallback(image_paths, pdf_path)


def _pillow_fallback(image_paths: list, pdf_path: str):
    valid = []
    for path in image_paths:
        try:
            with Image.open(path) as img:
                img.verify()
            valid.append(path)
        except Exception as e:
            print(f"   Skipping bad image {os.path.basename(path)}: {e}")

    if not valid:
        return

    opened = []
    try:
        for path in valid:
            img = Image.open(path).convert("RGB")
            opened.append(img)
        opened[0].save(
            pdf_path, "PDF",
            save_all=True,
            append_images=opened[1:],
            resolution=100,
        )
        print(f"   Saved PDF (Pillow fallback) -> {pdf_path}")
    finally:
        for img in opened:
            try:
                img.close()
            except Exception:
                pass
        opened.clear()


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yml"
    cfg = load_config(config_path)

    manga_url      = get(cfg, "manga",   "url")
    chapter_start  = get(cfg, "manga",   "chapter_start")
    chapter_end    = get(cfg, "manga",   "chapter_end")
    output_dir     = get(cfg, "output",  "dir",               default="manga_output")
    keep_images    = get(cfg, "output",  "keep_images",        default=False)
    headless       = get(cfg, "browser", "headless",           default=True)
    restart_every  = get(cfg, "browser", "restart_every",      default=10)
    workers        = get(cfg, "speed",   "download_workers",   default=5)
    scroll_wait    = get(cfg, "speed",   "scroll_wait",        default=1.5)
    page_wait      = get(cfg, "speed",   "page_wait",          default=4.0)
    chapter_delay  = get(cfg, "speed",   "chapter_delay",      default=2.0)

    # ── Image optimisation settings (new) ──────────────────────────────
    # jpeg_quality: 75 is the sweet spot for manhwa — sharp enough to read,
    # roughly 40-55 % smaller than the default 92 used before.
    # max_image_width: manhwa panels are typically 800-1200 px wide.
    # 1600 keeps full detail on wide monitors; set lower (e.g. 1200) to
    # shrink further.  Use 0 or null in config to disable downscaling.
    jpeg_quality   = get(cfg, "output",  "jpeg_quality",       default=75)
    max_img_width  = get(cfg, "output",  "max_image_width",    default=1600)
    # ───────────────────────────────────────────────────────────────────

    if not manga_url:
        print("manga.url is not set in config.yml")
        sys.exit(1)

    print(f"Workers: {workers} parallel image downloads per chapter")
    print(f"Image optimisation: quality={jpeg_quality}, max_width={max_img_width or 'unlimited'}")

    os.makedirs(output_dir, exist_ok=True)
    driver = make_driver(headless)

    try:
        chapters = get_chapter_links(driver, manga_url)
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
        print(f"Downloading EP{start} to EP{end} ({len(selected)} chapters)")

        for loop_idx, (ep_num, (url, title)) in enumerate(
            zip(range(start, end + 1), selected), start=1
        ):
            clean_title = safe_filename(title) if title else f"chapter-{ep_num}"
            pdf_name    = f"EP{ep_num}-{clean_title}.pdf"
            pdf_path    = os.path.join(output_dir, pdf_name)

            if os.path.exists(pdf_path):
                print(f"EP{ep_num} already exists, skipping.")
                continue

            print(f"\nEP{ep_num} / {len(chapters)} — {clean_title}")
            print(f"   URL: {url}")

            img_dir  = os.path.join(output_dir, f"_tmp_ep{ep_num}")
            opt_dir  = os.path.join(output_dir, f"_opt_ep{ep_num}")

            # 1. Download raw images
            image_paths = download_chapter_images(
                driver, url, img_dir, page_wait, scroll_wait, workers
            )
            print(f"   {len(image_paths)} pages downloaded")

            # 2. Optimise (resize + recompress) — this is where space is saved
            if image_paths:
                effective_width = int(max_img_width) if max_img_width else 0
                image_paths = optimize_chapter_images(
                    image_paths, opt_dir,
                    max_width=effective_width or 99999,
                    jpeg_quality=int(jpeg_quality),
                    workers=workers,
                )
                print(f"   {len(image_paths)} pages optimised")

            # 3. Pack into PDF
            images_to_pdf(image_paths, pdf_path)

            # 4. Cleanup temp folders
            if not keep_images:
                shutil.rmtree(img_dir, ignore_errors=True)
                shutil.rmtree(opt_dir, ignore_errors=True)

            # Memory cleanup
            driver.get("about:blank")
            gc.collect()

            # Restart Chrome every N chapters to flush RAM
            if loop_idx % restart_every == 0:
                print(f"\nRestarting Chrome at EP{ep_num} to free memory...")
                driver.quit()
                time.sleep(2)
                driver = make_driver(headless)

            time.sleep(chapter_delay)

    finally:
        driver.quit()

    print(f"\nAll done! PDFs saved in: ./{output_dir}/")


if __name__ == "__main__":
    main()