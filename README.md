# Manhwa-Downloader
just to use download some manhwa only work with manhwa website that allowed you to right click and download the picture of that manhwa.

Each chapter is saved as a **CBZ** (a ZIP of page images) — smaller than PDF, keeps the original image quality, and opens in every comic reader (Komga, Kavita, Mihon, YACReader, CDisplayEx, ...).

## Setup วิธีการติดตั้ง

1. Install Python 3.10+ and Google Chrome
2. Install dependencies:
   ```
   $env:PYYAML_FORCE_LIBYAML = "0"
   pip install -r requirements.txt
   ```
3. Edit `config.yml` with your manga URL and chapter range

## Usage วิธีการใช้งาน

```bash
# Run with default config.yml
python manga.py

# Run with a custom config file
python manga.py my_other_manga.yml
```

Chapters that already exist are skipped, so you can re-run to resume or to retry failed chapters.

## Config options (config.yml)

| Key                         | Default      | Description                                                      |
|-----------------------------|--------------|------------------------------------------------------------------|
| `manga.url`                 | *(required)* | Main page URL of the manga                                       |
| `manga.chapter_start`       | `null`       | First episode to download (null = from EP1)                      |
| `manga.chapter_end`         | `null`       | Last episode to download (null = until the end)                  |
| `manga.referer`             | `null`       | Referer for image requests (null = the site's own address)       |
| `output.dir`                | `Manga-Download` | Folder where CBZ files are saved                             |
| `output.image_format`       | `webp`       | `webp` (smallest), `original` (untouched), or `jpeg`             |
| `output.quality`            | `90`         | Quality of lossy pages (webp/jpeg)                               |
| `output.webp_method`        | `6`          | WebP effort 0-6 (6 = smallest, slowest)                          |
| `output.max_image_width`    | `0`          | Resize pages wider than this (0 = never resize)                  |
| `output.comicinfo`          | `true`       | Add ComicInfo.xml metadata to each CBZ                           |
| `output.keep_images`        | `false`      | Also keep the page images in a `<chapter>_pages` folder          |
| `browser.headless`          | `true`       | Run Chrome without a visible window                              |
| `browser.block_images`      | `true`       | Don't load images in Chrome (faster); auto-retries if none found |
| `browser.restart_every`     | `25`         | Restart Chrome every N chapters to free RAM                      |
| `speed.download_workers`    | `16`         | Parallel image downloads                                         |
| `speed.parallel_chapters`   | `3`          | Chapters downloading/converting while the next is being scraped  |
| `speed.convert_workers`     | `0`          | CPU processes for conversion (0 = auto)                          |
| `speed.page_wait`           | `3.0`        | Max seconds to wait for images to appear                         |
| `speed.scroll_wait`         | `0.4`        | Seconds between scroll steps (lazy-loaded pages only)            |
| `speed.chapter_delay`       | `0.5`        | Seconds between chapters                                         |

## Image format & quality

```yaml
output:
  image_format: webp      # smallest files, ~30-40% smaller than the source JPEGs
  quality: 90             # visually identical; 85 = smaller, 75 = smallest
```

* `webp` — JPEG pages are re-encoded to lossy WebP; PNG pages become **lossless** WebP. A page is only replaced when the WebP is actually smaller, and pages WebP can't hold (e.g. strips taller than 16383 px) are kept as the original.
* `original` — no re-encoding, pixel-perfect, fastest.
* Re-encoding a JPEG is always slightly lossy. If you want zero loss, use `original`.
* Check that your reader supports WebP inside CBZ (all the common ones do). If not, use `jpeg` or `original`.

## Download speed

```yaml
speed:
  download_workers: 16   # 8 = gentle, 16 = default, 32 = fast connection
  parallel_chapters: 3
```

Download speed is limited by your internet connection and the site's server, not your CPU. If you see `429` / failed pages, lower `download_workers` and `parallel_chapters`.

## Chapter range examples

```yaml
# Download everything โหลดทุกตอน
chapter_start: null
chapter_end: null

# EP62 to the end โหลดตอน X ถึงตอนล่าสุด
chapter_start: 62
chapter_end: null

# EP10 to EP20 only โหลดเฉพาะตอน X ถึงตอน X
chapter_start: 10
chapter_end: 20
```

## Output

```
Manga-Download/
  EP001-ตอนที่ 1.cbz
  EP002-ตอนที่ 2.cbz
  EP003-ตอนที่ 3.cbz
  ...
```
