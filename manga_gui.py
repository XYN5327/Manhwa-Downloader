"""
Simple GUI for manga.py.

Edits config.yml (keeping all your comments) and runs the downloader,
showing its output in a log window.

Keep this file in the same folder as manga.py and config.yml.
    python manga_gui.py
"""

import multiprocessing
import os
import queue
import re
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from collections import namedtuple
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import yaml

import manga

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CONFIG_PATH = os.path.join(APP_DIR, "config.yml")
ICON_NAME = "icon.ico"      # change this if your icon file has another name


def find_icon():
    """The icon is bundled inside the exe, so look next to this script first."""
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, APP_DIR, os.getcwd()):
        path = os.path.join(base, ICON_NAME)
        if os.path.isfile(path):
            return path
    return None

# ─────────────────────────────────────────────
# Field definitions: one row in the form each
# kinds: text, text? (may be blank -> null), int, int? (blank -> null),
#        float, bool, choice, dir, file? (blank -> null)
# ─────────────────────────────────────────────

Field = namedtuple("Field", "section key label kind opts hint default")

FIELDS = [
    Field("manga", "url", "Manga page URL", "text", None,
          "The manga's main page, not a chapter page.", ""),
    Field("manga", "chapter_start", "First chapter", "int?", (1, 100000),
          "Leave blank to start from chapter 1.", None),
    Field("manga", "chapter_end", "Last chapter", "int?", (1, 100000),
          "Leave blank to download up to the latest chapter.", None),
    Field("manga", "referer", "Referer", "text?", None,
          "Leave blank unless images fail with 403 errors.", None),

    Field("output", "dir", "Save folder", "dir", None,
          "A relative folder is created next to the app.", "Manga-Download"),
    Field("output", "image_format", "Pictures", "choice",
          ("webp", "original", "jpeg"),
          "Raw saves pages exactly as the site serves them. Re-encode makes smaller files.",
          "webp"),
    Field("output", "quality", "Quality", "int", (1, 100),
          "Only used when re-encoding. 90 looks identical to the source; 75 is smallest.", 90),
    Field("output", "webp_method", "WebP effort", "int", (0, 6),
          "WebP only. 6 = smallest files but slowest. Lower it on a weak CPU.", 6),
    Field("output", "max_image_width", "Max page width (px)", "int", (0, 20000),
          "Only used when re-encoding. 0 = never resize. Resizing loses quality.", 0),
    Field("output", "comicinfo", "Write ComicInfo.xml", "bool", None,
          "Series and chapter info for Komga, Kavita, Mihon and similar apps.", True),
    Field("output", "keep_images", "Keep page images", "bool", None,
          "Also save the pages in a folder next to each CBZ.", False),

    Field("browser", "type", "Browser", "choice", ("chrome", "edge"),
          "Edge is already included with Windows.", "chrome"),
    Field("browser", "binary_path", "Browser program", "file?", None,
          "Only for portable installs. Blank = detect automatically.", None),
    Field("browser", "driver_path", "Driver program", "file?", None,
          "chromedriver.exe or msedgedriver.exe. Blank = downloaded automatically.", None),
    Field("browser", "headless", "Run browser without a window", "bool", None,
          "The browser works in the background.", True),
    Field("browser", "block_images", "Block images in the browser", "bool", None,
          "Faster. Turns itself off if a site shows no images.", True),
    Field("browser", "restart_every", "Restart browser every N chapters", "int", (0, 10000),
          "Frees memory. 0 = never.", 25),

    Field("speed", "download_workers", "Parallel downloads", "int", (1, 64),
          "8 = gentle, 16 = default, 32 = fast connection and tolerant site.", 16),
    Field("speed", "parallel_chapters", "Chapters at once", "int", (1, 8),
          "2 to 4 works best.", 3),
    Field("speed", "convert_workers", "Conversion processes", "int", (0, 64),
          "0 = automatic. Use 2 to 4 while gaming.", 3),
    Field("speed", "priority", "CPU priority", "choice", ("normal", "low", "idle"),
          "idle = only uses CPU time nothing else wants. Best while gaming.", "low"),
    Field("speed", "page_wait", "Page wait (seconds)", "float", (0, 60),
          "Longest wait for a chapter's images to appear.", 3.0),
    Field("speed", "scroll_wait", "Scroll wait (seconds)", "float", (0, 10),
          "Increase if some pages are missing.", 0.4),
    Field("speed", "chapter_delay", "Delay between chapters (seconds)", "float", (0, 30),
          "Be polite to the server.", 0.5),
]

TABS = {"manga": "Manga", "output": "Output", "browser": "Browser", "speed": "Speed"}
FIELD_BY_KEY = {(f.section, f.key): f for f in FIELDS}

# Friendly names shown in the window; the config keeps the short value.
CHOICE_LABELS = {
    ("output", "image_format"): {
        "original": "Raw: keep original pictures (biggest, no quality loss)",
        "webp": "Re-encode to WebP (smallest files)",
        "jpeg": "Re-encode to JPEG (works in every reader)",
    },
}


# ─────────────────────────────────────────────
# Reading values from the form / writing config.yml
# ─────────────────────────────────────────────

def read_field(f: Field, raw):
    """Convert what the user typed into a Python value. Raises ValueError."""
    if f.kind == "bool":
        return bool(raw)
    s = str(raw).strip()

    if f.kind in ("text?", "file?"):
        return s or None
    if f.kind in ("text", "dir"):
        if not s:
            raise ValueError(f"{f.label} can't be empty.")
        return s
    if f.kind == "choice":
        labels = CHOICE_LABELS.get((f.section, f.key))
        if labels:
            s = next((k for k, v in labels.items() if v == s), s)
        if s not in f.opts:
            raise ValueError(f"{f.label} must be one of: {', '.join(f.opts)}.")
        return s

    if f.kind == "int?" and not s:
        return None
    cast = float if f.kind == "float" else int
    try:
        num = cast(s)
    except ValueError:
        kind = "a number" if cast is float else "a whole number"
        raise ValueError(f"{f.label} must be {kind}.")
    if f.opts and not (f.opts[0] <= num <= f.opts[1]):
        raise ValueError(f"{f.label} must be between {f.opts[0]} and {f.opts[1]}.")
    return num


def fmt_value(f: Field, v) -> str:
    """Format a value as YAML text for config.yml."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if f.kind == "choice":
        return v
    if "\\" in v:      # Windows paths: single quotes, no escaping needed
        return "'" + v.replace("'", "''") + "'"
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


_TOP = re.compile(r"^([A-Za-z_]\w*):\s*(#.*)?$")
_KEY = re.compile(r"^(\s+)([A-Za-z_]\w*):.*$")


def patch_config(text: str, values: dict) -> str:
    """
    Replace values line by line so every comment in config.yml survives.
    values maps (section, key) -> already formatted YAML text.
    Keys missing from the file are added under their section.
    """
    lines = text.splitlines()
    pending = dict(values)
    section = None

    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _TOP.match(line)
        if m:
            section = m.group(1)
            continue
        m = _KEY.match(line)
        if m and section and (section, m.group(2)) in pending:
            key = (section, m.group(2))
            lines[i] = f"{m.group(1)}{m.group(2)}: {pending.pop(key)}"

    for (sec, key), val in pending.items():
        idx = next((i for i, l in enumerate(lines)
                    if re.match(rf"^{re.escape(sec)}:\s*(#.*)?$", l)), None)
        if idx is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"{sec}:")
            idx = len(lines) - 1
        lines.insert(idx + 1, f"  {key}: {val}")

    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────
# Log plumbing: manga.py prints, we show it in the window
# ─────────────────────────────────────────────

class QueueWriter:
    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)
        return len(s) if s else 0

    def flush(self):
        pass

    def isatty(self):
        return False


# ─────────────────────────────────────────────
# The window
# ─────────────────────────────────────────────

class App(ttk.Frame):
    def __init__(self, root: tk.Tk):
        super().__init__(root, padding=12)
        self.root = root
        self.vars = {}
        self.widgets = {}
        self.rows = {sec: 0 for sec in TABS}
        self.q = queue.Queue()
        self.running = False

        hint_font = tkfont.nametofont("TkDefaultFont").copy()
        hint_font.configure(size=max(8, hint_font.cget("size") - 1))
        ttk.Style().configure("Hint.TLabel", foreground="#6b6b6b", font=hint_font)

        self.build()
        self.load()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.poll)

    # ---- layout ----

    def build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        nb = ttk.Notebook(self)
        nb.grid(row=0, column=0, sticky="ew")
        frames = {}
        for sec, title in TABS.items():
            fr = ttk.Frame(nb, padding=(14, 6, 14, 14))
            fr.columnconfigure(1, weight=1)
            nb.add(fr, text=title)
            frames[sec] = fr
        for f in FIELDS:
            self.add_field(frames[f.section], f)

        bar = ttk.Frame(self)
        bar.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        self.btn_download = ttk.Button(bar, text="Save and download", command=self.on_download)
        self.btn_download.pack(side="left")
        self.btn_save = ttk.Button(bar, text="Save settings", command=self.on_save)
        self.btn_save.pack(side="left", padx=6)
        ttk.Button(bar, text="Open save folder", command=self.open_output).pack(side="left")
        self.status = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.status, style="Hint.TLabel").pack(side="right")

        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.grid(row=2, column=0, sticky="ew", pady=(10, 6))

        box = ttk.Frame(self)
        box.grid(row=3, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.log = tk.Text(box, height=12, wrap="word", state="disabled",
                           font=("Consolas", 9), relief="solid", borderwidth=1)
        scroll = ttk.Scrollbar(box, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

    def add_field(self, fr: ttk.Frame, f: Field):
        r = self.rows[f.section]
        self.rows[f.section] += 2
        key = (f.section, f.key)

        if f.kind == "bool":
            var = tk.BooleanVar()
            ttk.Checkbutton(fr, text=f.label, variable=var).grid(
                row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))
            ttk.Label(fr, text=f.hint, style="Hint.TLabel").grid(
                row=r + 1, column=0, columnspan=3, sticky="w", padx=(24, 0))
        else:
            var = tk.StringVar()
            ttk.Label(fr, text=f.label).grid(
                row=r, column=0, sticky="w", padx=(0, 14), pady=(10, 0))
            if f.kind == "choice":
                labels = CHOICE_LABELS.get(key)
                w = ttk.Combobox(fr, textvariable=var,
                                 values=list(labels.values()) if labels else f.opts,
                                 state="readonly", width=48 if labels else 12)
                sticky = "w"
            elif f.kind in ("int", "float"):
                step = 0.1 if f.kind == "float" else 1
                w = ttk.Spinbox(fr, textvariable=var, from_=f.opts[0], to=f.opts[1],
                                increment=step, width=10)
                sticky = "w"
            elif f.kind == "int?":
                w = ttk.Entry(fr, textvariable=var, width=12)
                sticky = "w"
            else:
                w = ttk.Entry(fr, textvariable=var)
                sticky = "ew"
            w.grid(row=r, column=1, sticky=sticky, pady=(10, 0))
            if f.kind in ("dir", "file?"):
                ttk.Button(fr, text="Browse...", command=lambda f=f, v=var: self.browse(f, v)
                           ).grid(row=r, column=2, padx=(6, 0), pady=(10, 0))
            ttk.Label(fr, text=f.hint, style="Hint.TLabel", wraplength=520,
                      justify="left").grid(row=r + 1, column=1, columnspan=2, sticky="w")
        self.vars[key] = var
        self.widgets[key] = w if f.kind != "bool" else None
        if key == ("output", "image_format"):
            var.trace_add("write", lambda *_: self.update_reencode_state())

    def update_reencode_state(self):
        """Grey out re-encode settings when pictures are kept raw."""
        mode = read_field(FIELD_BY_KEY[("output", "image_format")],
                          self.vars[("output", "image_format")].get()) \
            if self.vars[("output", "image_format")].get() else "webp"
        for key, active in (
            (("output", "quality"), mode != "original"),
            (("output", "max_image_width"), mode != "original"),
            (("output", "webp_method"), mode == "webp"),
        ):
            w = self.widgets.get(key)
            if w is not None:
                w.configure(state="normal" if active else "disabled")

    def browse(self, f: Field, var: tk.StringVar):
        start = var.get() or APP_DIR
        if f.kind == "dir":
            path = filedialog.askdirectory(initialdir=start if os.path.isdir(start) else APP_DIR)
        else:
            path = filedialog.askopenfilename(
                initialdir=os.path.dirname(start) if os.path.exists(start) else APP_DIR)
        if path:
            var.set(os.path.normpath(path))

    # ---- config <-> form ----

    def load(self):
        data = {}
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            self.status.set("No config.yml yet. It will be created when you save.")
        except yaml.YAMLError as e:
            messagebox.showerror("config.yml problem",
                                 f"config.yml could not be read, so defaults are shown.\n\n{e}")
        for f in FIELDS:
            val = manga.get(data, f.section, f.key, default=f.default)
            var = self.vars[(f.section, f.key)]
            if f.kind == "bool":
                var.set(bool(val))
            else:
                labels = CHOICE_LABELS.get((f.section, f.key))
                if labels:
                    val = labels.get(str(val).lower(), val)
                var.set("" if val is None else str(val))
        self.update_reencode_state()
        if str(self.vars[("manga", "url")].get()) == "URL-HERE":
            self.vars[("manga", "url")].set("")

    def collect(self) -> dict:
        values = {}
        for f in FIELDS:
            values[(f.section, f.key)] = read_field(f, self.vars[(f.section, f.key)].get())
        url = values[("manga", "url")]
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("Manga page URL must start with http:// or https://")
        first, last = values[("manga", "chapter_start")], values[("manga", "chapter_end")]
        if first is not None and last is not None and first > last:
            raise ValueError("First chapter can't be higher than last chapter.")
        return values

    def save(self) -> bool:
        try:
            values = self.collect()
        except ValueError as e:
            messagebox.showwarning("Check your settings", str(e))
            return False
        formatted = {k: fmt_value(FIELD_BY_KEY[k], v) for k, v in values.items()}
        try:
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except FileNotFoundError:
                text = ""
            new_text = patch_config(text, formatted)
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(new_text)
            os.replace(tmp, CONFIG_PATH)
        except OSError as e:
            messagebox.showerror("Could not save", f"config.yml could not be written.\n\n{e}")
            return False
        self.status.set("Settings saved.")
        return True

    # ---- buttons ----

    def on_save(self):
        self.save()

    def on_download(self):
        if self.save():
            self.start_download()

    def open_output(self):
        folder = os.path.join(APP_DIR, self.vars[("output", "dir")].get().strip()
                              or "Manga-Download")
        os.makedirs(folder, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except OSError as e:
            messagebox.showerror("Could not open folder", str(e))

    # ---- running the downloader ----

    def set_running(self, running: bool):
        self.running = running
        state = "disabled" if running else "normal"
        self.btn_download.configure(state=state)
        self.btn_save.configure(state=state)
        if running:
            self.progress.start(12)
            self.status.set("Downloading...")
        else:
            self.progress.stop()

    def start_download(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.set_running(True)
        threading.Thread(target=self.worker, daemon=True).start()

    def worker(self):
        old_out, old_err, old_argv = sys.stdout, sys.stderr, sys.argv
        sys.stdout = sys.stderr = QueueWriter(self.q)
        sys.argv = [old_argv[0], CONFIG_PATH]
        code = 0
        try:
            manga.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
        except Exception:
            traceback.print_exc()
            code = 1
        finally:
            sys.stdout, sys.stderr, sys.argv = old_out, old_err, old_argv
            self.q.put(("done", code))

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple):
                    self.on_finished(item[1])
                else:
                    self.append(item)
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def append(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def on_finished(self, code: int):
        self.set_running(False)
        if code == 0:
            self.status.set("Finished.")
        else:
            self.status.set("Finished with problems. See the log.")

    def on_close(self):
        if self.running and not messagebox.askyesno(
                "Download in progress",
                "A download is still running. Close anyway?\n\n"
                "Unfinished chapters are not saved and will be retried next time."):
            return
        self.root.destroy()
        if self.running:
            os._exit(0)


def main():
    try:
        os.chdir(APP_DIR)       # relative save folders land next to the app
    except OSError:
        pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # sharp text on HiDPI
        except Exception:
            pass
        try:
            import ctypes
            # Own taskbar identity, so Windows shows our icon and not Python's
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MangaDownloader.App")
        except Exception:
            pass
    root = tk.Tk()
    root.title("Manga Downloader")
    icon = find_icon()
    if icon:
        try:
            root.iconbitmap(default=icon)    # title bar, taskbar and dialogs
        except Exception:
            pass
    root.minsize(720, 720)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    App(root).grid(row=0, column=0, sticky="nsew")
    root.mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()    # needed for the converter processes in the exe
    main()
