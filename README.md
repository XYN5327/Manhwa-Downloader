# Manhwa-Downloader
just to use download some manhwa only work with manhwa website that allowed you to right click and download the picture of that manhwa.

## Setup วิธีการติดตั้ง

1. Install Python 3.10+
2. Install dependencies:
   ```
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

## Config options (config.yml)

| Key                        | Default         | Description                                      |
|----------------------------|-----------------|--------------------------------------------------|
| `manga.url`                | *(required)*    | Main page URL of the manga                       |
| `manga.chapter_start`      | `null`          | First episode to download (null = from EP1)      |
| `manga.chapter_end`        | `null`          | Last episode to download (null = until the end)  |
| `output.dir`               | `manga_output`  | Folder where PDFs are saved                      |
| `output.keep_images`       | `false`         | Keep raw images after PDF is created             |
| `browser.headless`         | `true`          | Run Chrome without a visible window              |
| `browser.restart_every`    | `10`            | Restart Chrome every N chapters to free RAM      |
| `timing.scroll_wait`       | `1.5`           | Seconds between scroll steps                     |
| `timing.page_wait`         | `4.0`           | Seconds to wait after page load                  |

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

```yaml
## ความเร็วในการดาวน์โหลด
download_workers: 5   # safe, unlikely to get rate-limited
download_workers: 10  # faster, fine for most sites
download_workers: 20  # maximum, may get throttled on slow servers
```

## Output

```
manga_output/
  EP1-ตอนที่ 1.pdf
  EP2-ตอนที่ 2.pdf
  EP3-ตอนที่ 3.pdf
  ...
```