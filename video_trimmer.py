#!/usr/bin/env python3
"""
Video Trimmer — Cut clips without re-encoding (stream copy via FFmpeg).
Drag & drop a video, set IN/OUT points, export.
"""

import tkinter as tk
from tkinter import filedialog, messagebox
import subprocess
import os
import re
import threading
import sys
import tempfile

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False


# ─── FFmpeg path resolution ────────────────────────────────────────────────────
# When bundled with PyInstaller (--onefile), sys._MEIPASS points to the
# temp folder where binaries are extracted.  We ship ffmpeg.exe / ffprobe.exe
# there via --add-binary.  On a normal Python run we fall back to PATH.

def _get_bin(name: str) -> str:
    """Return path to ffmpeg or ffprobe binary."""
    # Running inside a PyInstaller bundle
    base = getattr(sys, "_MEIPASS", None)
    if base:
        win_name = name + ".exe"
        for candidate in (win_name, name):
            full = os.path.join(base, candidate)
            if os.path.isfile(full):
                return full
    # Normal run: rely on system PATH
    return name + (".exe" if sys.platform == "win32" else "")


FFMPEG  = _get_bin("ffmpeg")
FFPROBE = _get_bin("ffprobe")


# ─── Colour palette ────────────────────────────────────────────────────────────
BG        = "#0f0f13"
PANEL     = "#17171e"
ACCENT    = "#e8ff47"        # electric lime
ACCENT2   = "#6c6fff"        # soft violet
MUTED     = "#3a3a48"
TEXT      = "#e8e8f0"
TEXT_DIM  = "#6b6b80"
DANGER    = "#ff4f4f"
SUCCESS   = "#3ddc84"

FONT_MONO = ("Courier New", 10)
FONT_UI   = ("Helvetica", 10)
FONT_BIG  = ("Helvetica", 13, "bold")
FONT_TINY = ("Helvetica", 8)


def hms_to_seconds(s: str) -> float:
    """Parse HH:MM:SS.mmm or SS.mmm → float seconds."""
    s = s.strip()
    parts = s.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        else:
            return float(parts[0])
    except ValueError:
        return 0.0


def seconds_to_hms(sec: float) -> str:
    """Float seconds → HH:MM:SS.mmm"""
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def probe_duration(path: str) -> float:
    """Return video duration in seconds via ffprobe."""
    cmd = [
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip()
        return float(out)
    except Exception:
        return 0.0


def ffmpeg_cut(src: str, dst: str, start: float, end: float,
               progress_cb=None, done_cb=None):
    """
    Run FFmpeg stream-copy cut in a background thread.
    -ss before -i for fast seek; -to is relative to -ss.
    """
    duration = end - start
    cmd = [
        FFMPEG, "-y",
        "-ss", str(start),
        "-i", src,
        "-t", str(duration),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        dst
    ]

    def run():
        proc = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        time_re = re.compile(r"time=(\d+:\d+:\d+\.\d+)")
        for line in proc.stderr:
            if progress_cb:
                m = time_re.search(line)
                if m:
                    elapsed = hms_to_seconds(m.group(1))
                    pct = min(1.0, elapsed / duration) if duration > 0 else 0
                    progress_cb(pct)
        proc.wait()
        if done_cb:
            done_cb(proc.returncode)

    t = threading.Thread(target=run, daemon=True)
    t.start()


def ffmpeg_extract_frame(src: str, t: float, width: int = 240) -> str | None:
    """
    Extract a single frame at time t from src into a temp JPEG file.
    Returns the temp file path, or None on failure.
    Runs synchronously — call only from a background thread.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    cmd = [
        FFMPEG, "-y",
        "-ss", f"{t:.3f}",
        "-i", src,
        "-vframes", "1",
        "-vf", f"scale={width}:-1",
        "-q:v", "4",
        tmp.name
    ]
    try:
        # CREATE_NO_WINDOW on Windows so no console flashes
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        subprocess.run(cmd,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,
                       timeout=8,
                       **kwargs)
        if os.path.exists(tmp.name) and os.path.getsize(tmp.name) > 0:
            return tmp.name
    except Exception:
        pass
    try:
        os.unlink(tmp.name)
    except Exception:
        pass
    return None


import queue as _queue

class FrameExtractor:
    """
    Single background worker that extracts frames one at a time.
    Requests are placed in a depth-1 queue: a new request silently
    replaces any pending (not-yet-started) request, so only ONE
    ffmpeg process ever runs at a time regardless of how fast the
    slider moves.
    """
    def __init__(self):
        self._q      = _queue.Queue(maxsize=1)
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def request(self, src: str, t: float, width: int, callback):
        """
        Schedule a frame extraction.
        callback(tmp_path_or_None) will be called on the worker thread;
        caller must use root.after(0, ...) if it needs to touch the UI.
        """
        item = (src, t, width, callback)
        # non-blocking put: if queue full, drain it first (drop stale request)
        try:
            self._q.get_nowait()
        except _queue.Empty:
            pass
        self._q.put(item)

    def _worker(self):
        while True:
            src, t, width, callback = self._q.get()  # blocks until work arrives
            tmp = ffmpeg_extract_frame(src, t, width)
            try:
                callback(tmp)
            except Exception:
                pass


# Global singleton — one ffmpeg at a time for the whole app
_frame_extractor = FrameExtractor()




# ─── Preview popup ────────────────────────────────────────────────────────────

class PreviewPopup:
    """
    Floating thumbnail shown above a slider handle while dragging.
    Uses ffmpeg to extract a frame in a background thread with debounce.
    Requires Pillow (pip install pillow). Falls back gracefully without it.
    """
    THUMB_W     = 240
    DEBOUNCE_MS = 150

    def __init__(self, root: tk.Tk):
        self.root     = root
        self._win     = None
        self._label   = None
        self._tlbl    = None
        self._img_ref = None
        self._pending = None
        self._last_t  = None
        self._src     = None
        self._tmp     = None

    # ── public API ────────────────────────────────────────────────────
    def attach(self, src: str):
        self._src   = src
        self._last_t = None

    def show(self, x_root: int, y_root: int, t: float):
        if not HAS_PIL or not self._src:
            return
        self._create_window(x_root, y_root)
        if self._pending is not None:
            self.root.after_cancel(self._pending)
        self._pending = self.root.after(
            self.DEBOUNCE_MS,
            lambda: self._fetch(t, x_root, y_root)
        )

    def hide(self):
        if self._pending is not None:
            self.root.after_cancel(self._pending)
            self._pending = None
        if self._win:
            self._win.withdraw()
        self._cleanup_tmp()

    # ── internals ─────────────────────────────────────────────────────
    def _create_window(self, x_root: int, y_root: int):
        if self._win is None:
            self._win = tk.Toplevel(self.root)
            self._win.overrideredirect(True)
            self._win.attributes("-topmost", True)
            self._win.configure(bg=BG)
            border = tk.Frame(self._win, bg=ACCENT2, padx=1, pady=1)
            border.pack()
            inner = tk.Frame(border, bg=BG)
            inner.pack()
            self._label = tk.Label(inner, bg=BG, text="…",
                                   fg=TEXT_DIM, font=FONT_TINY,
                                   width=30, height=8)
            self._label.pack()
            self._tlbl = tk.Label(inner, bg="#0a0a10", fg=ACCENT,
                                  font=FONT_MONO, pady=3)
            self._tlbl.pack(fill="x")
        self._reposition(x_root, y_root)
        self._win.deiconify()

    def _reposition(self, x_root: int, y_root: int):
        if not self._win:
            return
        self._win.update_idletasks()
        pw = self._win.winfo_width() or (self.THUMB_W + 4)
        ph = self._win.winfo_height() or 160
        x  = x_root - pw // 2
        y  = y_root - ph - 14
        sw = self.root.winfo_screenwidth()
        x  = max(4, min(x, sw - pw - 4))
        self._win.geometry(f"+{x}+{y}")

    def _fetch(self, t: float, x_root: int, y_root: int):
        self._pending = None
        if self._last_t is not None and abs(t - self._last_t) < 0.05:
            return
        self._last_t = t
        src = self._src

        def worker():
            tmp = ffmpeg_extract_frame(src, t, self.THUMB_W)
            self.root.after(0, lambda: self._show_frame(tmp, t, x_root, y_root))

        threading.Thread(target=worker, daemon=True).start()

    def _show_frame(self, tmp_path, t: float, x_root: int, y_root: int):
        if not self._win or not self._win.winfo_exists():
            return
        if not self._win.winfo_viewable():
            return
        self._cleanup_tmp()
        self._tmp = tmp_path
        if tmp_path and HAS_PIL:
            try:
                img   = Image.open(tmp_path)
                photo = ImageTk.PhotoImage(img)
                self._img_ref = photo
                self._label.config(image=photo, text="",
                                   width=0, height=0)
            except Exception:
                self._label.config(image="", text="no preview",
                                   width=30, height=8)
        else:
            self._label.config(image="", text="no preview",
                               width=30, height=8)
        if self._tlbl:
            self._tlbl.config(text=f"  {seconds_to_hms(t)}  ")
        self._reposition(x_root, y_root)

    def _cleanup_tmp(self):
        if self._tmp:
            try:
                os.unlink(self._tmp)
            except Exception:
                pass
            self._tmp = None


# ─── Widgets ───────────────────────────────────────────────────────────────────

class TimeEntry(tk.Frame):
    """A labelled HH:MM:SS.mmm entry."""
    def __init__(self, parent, label: str, **kw):
        super().__init__(parent, bg=PANEL, **kw)
        tk.Label(self, text=label, bg=PANEL, fg=TEXT_DIM,
                 font=FONT_TINY).pack(anchor="w")
        self.var = tk.StringVar(value="00:00:00.000")
        self.entry = tk.Entry(
            self, textvariable=self.var,
            bg=MUTED, fg=ACCENT, insertbackground=ACCENT,
            relief="flat", font=FONT_MONO, width=14,
            highlightthickness=1, highlightcolor=ACCENT2,
            highlightbackground=MUTED
        )
        self.entry.pack()

    def get(self) -> float:
        return hms_to_seconds(self.var.get())

    def set(self, sec: float):
        self.var.set(seconds_to_hms(sec))


class ThumbnailStrip(tk.Canvas):
    """
    A horizontal strip of video thumbnails drawn below the range slider.
    All frames are extracted sequentially by ONE dedicated worker thread,
    so only one ffmpeg process ever runs at a time.
    A new load() call increments gen_id, causing the worker to skip stale jobs.
    """
    THUMB_H   = 52
    LABEL_H   = 14
    SPACING   = 74
    THUMB_W   = 64
    TOTAL_H   = THUMB_H + LABEL_H + 4

    def __init__(self, parent, **kw):
        super().__init__(parent, bg="#0a0a10", highlightthickness=0,
                         height=self.TOTAL_H, **kw)
        self._src      = None
        self._duration = 0.0
        self._photos   = {}       # index → PhotoImage (keep refs alive)
        self._gen_id   = 0        # bump on each new load to discard stale results
        self._q        = _queue.Queue()   # (gen_id, idx, cx, t, src)
        self._worker   = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()
        self.bind("<Configure>", self._on_resize)

    # ── public API ────────────────────────────────────────────────────
    def load(self, src: str, duration: float):
        self._src      = src
        self._duration = max(duration, 1.0)
        self._gen_id  += 1          # invalidate all pending jobs instantly
        self._photos.clear()
        self.delete("all")
        self.after(60, self._schedule_all)

    # ── internals ─────────────────────────────────────────────────────
    def _on_resize(self, _=None):
        if self._src:
            self._gen_id += 1
            self._photos.clear()
            self.delete("all")
            self.after(80, self._schedule_all)

    def _schedule_all(self):
        w = self.winfo_width() or 500
        positions = self._thumb_positions(w)
        gen_id = self._gen_id
        for idx, (cx, t) in enumerate(positions):
            x0 = cx - self.THUMB_W // 2
            x1 = cx + self.THUMB_W // 2
            self.create_rectangle(x0, 2, x1, 2 + self.THUMB_H,
                                  fill=MUTED, outline="", tags=f"ph_{idx}")
            self.create_text(cx, 2 + self.THUMB_H + self.LABEL_H // 2,
                             text=f"{int(t)}s", fill=TEXT_DIM,
                             font=("Helvetica", 7), tags=f"lbl_{idx}")
            self._q.put((gen_id, idx, cx, t, self._src))

    def _thumb_positions(self, width: int):
        PAD = self.SPACING // 2
        positions, x = [], PAD
        while x <= width - PAD:
            t = (x / width) * self._duration
            t = max(0.0, min(self._duration - 0.1, t))
            positions.append((x, t))
            x += self.SPACING
        return positions

    def _worker_loop(self):
        """Single background thread — pulls jobs one-by-one."""
        while True:
            gen_id, idx, cx, t, src = self._q.get()
            # skip stale jobs (from old load/resize)
            if gen_id != self._gen_id:
                continue
            tmp = ffmpeg_extract_frame(src, t, self.THUMB_W)
            self.after(0, lambda i=idx, x=cx, p=tmp, g=gen_id:
                       self._paint_one(i, x, p, g))

    def _paint_one(self, idx: int, cx: int, tmp_path, gen_id: int):
        if gen_id != self._gen_id:
            if tmp_path:
                try: os.unlink(tmp_path)
                except: pass
            return
        if not tmp_path or not HAS_PIL:
            return
        try:
            img   = Image.open(tmp_path)
            ratio = self.THUMB_H / img.height
            tw    = max(1, int(img.width * ratio))
            img   = img.resize((tw, self.THUMB_H), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._photos[idx] = photo
            x0 = cx - tw // 2
            self.delete(f"ph_{idx}")
            self.create_image(x0, 2, anchor="nw", image=photo,
                              tags=f"img_{idx}")
        except Exception:
            pass
        finally:
            try: os.unlink(tmp_path)
            except: pass


class RangeSlider(tk.Canvas):
    """
    A simple two-handle range slider drawn on a Canvas.
    Handles: IN (green) and OUT (red).
    """
    TRACK_H  = 6
    HANDLE_R = 9
    PAD      = 18

    def __init__(self, parent, duration=100.0, on_change=None, on_drag_preview=None, **kw):
        super().__init__(parent, bg=BG, highlightthickness=0,
                         height=40, **kw)
        self.duration         = max(duration, 1.0)
        self.on_change        = on_change
        self.on_drag_preview  = on_drag_preview   # callback(t, marker_name) | None
        self._in  = 0.0
        self._out = self.duration
        self._drag = None          # "in" | "out"

        self.bind("<Configure>",       self._redraw)
        self.bind("<ButtonPress-1>",   self._press)
        self.bind("<B1-Motion>",       self._drag_move)
        self.bind("<ButtonRelease-1>", self._release)

    # ── helpers ────────────────────────────────────────────
    def _x_of(self, t: float) -> int:
        w = self.winfo_width() or 400
        return int(self.PAD + (t / self.duration) * (w - 2 * self.PAD))

    def _t_of(self, x: int) -> float:
        w = self.winfo_width() or 400
        t = (x - self.PAD) / (w - 2 * self.PAD) * self.duration
        return max(0.0, min(self.duration, t))

    # ── drawing ────────────────────────────────────────────
    def _redraw(self, *_):
        self.delete("all")
        w = self.winfo_width() or 400
        cy = 20
        r  = self.HANDLE_R

        # background track
        self.create_line(self.PAD, cy, w - self.PAD, cy,
                         fill=MUTED, width=self.TRACK_H, capstyle="round")
        # selected range
        x_in  = self._x_of(self._in)
        x_out = self._x_of(self._out)
        self.create_line(x_in, cy, x_out, cy,
                         fill=ACCENT, width=self.TRACK_H, capstyle="round",
                         tags="range")
        # IN handle
        self.create_oval(x_in - r, cy - r, x_in + r, cy + r,
                         fill=SUCCESS, outline=BG, width=2, tags="h_in")
        self.create_text(x_in, cy, text="I", fill=BG,
                         font=("Helvetica", 7, "bold"))
        # OUT handle
        self.create_oval(x_out - r, cy - r, x_out + r, cy + r,
                         fill=DANGER, outline=BG, width=2, tags="h_out")
        self.create_text(x_out, cy, text="O", fill=BG,
                         font=("Helvetica", 7, "bold"))

    # ── interaction ────────────────────────────────────────
    def _press(self, e):
        x_in  = self._x_of(self._in)
        x_out = self._x_of(self._out)
        if abs(e.x - x_in) <= self.HANDLE_R + 4:
            self._drag = "in"
        elif abs(e.x - x_out) <= self.HANDLE_R + 4:
            self._drag = "out"

    def _drag_move(self, e):
        if not self._drag:
            return
        t = self._t_of(e.x)
        if self._drag == "in":
            self._in = min(t, self._out - 0.1)
            preview_t = self._in
        else:
            self._out = max(t, self._in + 0.1)
            preview_t = self._out
        self._redraw()
        if self.on_change:
            self.on_change(self._in, self._out)
        if self.on_drag_preview:
            self.on_drag_preview(preview_t, self._drag)

    def _release(self, e):
        self._drag = None

    # ── API ────────────────────────────────────────────────
    def set_range(self, t_in: float, t_out: float):
        self._in  = max(0.0, min(t_in, self.duration))
        self._out = max(0.0, min(t_out, self.duration))
        self._redraw()

    @property
    def value_in(self):  return self._in
    @property
    def value_out(self): return self._out


# ─── Main Application ──────────────────────────────────────────────────────────

class VideoTrimmerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("✂  Video Trimmer")
        self.root.configure(bg=BG)
        self.root.geometry("900x560")
        self.root.resizable(True, False)

        self.src_path   = None
        self.duration   = 0.0
        self._preview_img_ref = None   # keep PhotoImage alive

        self._build_ui()

    # ── UI construction ────────────────────────────────────
    def _build_ui(self):
        root = self.root

        # ════════════════════════════════════════════════════
        # Header
        # ════════════════════════════════════════════════════
        hdr = tk.Frame(root, bg=BG)
        hdr.pack(fill="x", padx=20, pady=(10, 4))
        tk.Label(hdr, text="✂  VIDEO TRIMMER", bg=BG, fg=ACCENT,
                 font=("Courier New", 14, "bold")).pack(side="left")
        tk.Label(hdr, text="stream copy · no re-encoding", bg=BG,
                 fg=TEXT_DIM, font=FONT_TINY).pack(side="left", padx=10)

        # ════════════════════════════════════════════════════
        # Two-column body: [preview | controls]
        # ════════════════════════════════════════════════════
        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=4)

        # ── LEFT: frame preview panel ────────────────────────
        PREV_W, PREV_H = 320, 200

        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="y", padx=(0, 14))

        self.preview_canvas = tk.Canvas(
            left, width=PREV_W, height=PREV_H,
            bg="#0a0a10", highlightthickness=1,
            highlightbackground=MUTED
        )
        self.preview_canvas.pack()
        # placeholder text
        self.preview_canvas.create_text(
            PREV_W // 2, PREV_H // 2,
            text="drag a marker\nto preview frame",
            fill=TEXT_DIM, font=FONT_UI, justify="center",
            tags="placeholder"
        )

        # timecode label under preview
        self.preview_tc_var = tk.StringVar(value="")
        tk.Label(left, textvariable=self.preview_tc_var,
                 bg=BG, fg=ACCENT, font=FONT_MONO,
                 anchor="center").pack(fill="x", pady=(4, 0))

        # which marker is shown
        self.preview_marker_var = tk.StringVar(value="")
        tk.Label(left, textvariable=self.preview_marker_var,
                 bg=BG, fg=TEXT_DIM, font=FONT_TINY,
                 anchor="center").pack(fill="x")

        # ── RIGHT: all controls ──────────────────────────────
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        # Drop zone
        self.drop_frame = tk.Frame(
            right, bg=PANEL, relief="flat",
            highlightthickness=2, highlightbackground=MUTED
        )
        self.drop_frame.pack(fill="x", pady=(0, 6))

        self.drop_label = tk.Label(
            self.drop_frame,
            text="⬇  Drop video here  or  click to browse",
            bg=PANEL, fg=TEXT_DIM, font=FONT_UI,
            pady=12, cursor="hand2"
        )
        self.drop_label.pack(fill="x")

        self.drop_frame.bind("<Button-1>", self._browse)
        self.drop_label.bind("<Button-1>", self._browse)
        self.drop_frame.bind("<Enter>", lambda _: self.drop_frame.config(
            highlightbackground=ACCENT2))
        self.drop_frame.bind("<Leave>", lambda _: self.drop_frame.config(
            highlightbackground=MUTED))

        if HAS_DND:
            self.drop_frame.drop_target_register(DND_FILES)
            self.drop_frame.dnd_bind("<<Drop>>", self._on_drop)

        # File info
        self.info_var = tk.StringVar(value="")
        tk.Label(right, textvariable=self.info_var, bg=BG, fg=ACCENT2,
                 font=FONT_MONO, anchor="w").pack(fill="x")

        # Slider
        tk.Label(right, text="SELECT RANGE", bg=BG, fg=TEXT_DIM,
                 font=FONT_TINY, anchor="w").pack(fill="x", pady=(4, 0))
        self.slider = RangeSlider(right, duration=100.0,
                                  on_change=self._slider_moved,
                                  on_drag_preview=self._update_preview_panel)
        self.slider.pack(fill="x", pady=(2, 0))

        # Thumbnail strip
        self.thumb_strip = ThumbnailStrip(right)
        self.thumb_strip.pack(fill="x", pady=(0, 4))

        # Time entries
        te_row = tk.Frame(right, bg=BG)
        te_row.pack(fill="x", pady=4)
        self.te_in  = TimeEntry(te_row, "IN  (start)")
        self.te_in.pack(side="left", padx=(0, 12))
        self.te_out = TimeEntry(te_row, "OUT (end)")
        self.te_out.pack(side="left")

        # Apply + duration
        btn_row = tk.Frame(right, bg=BG)
        btn_row.pack(fill="x", pady=(0, 2))
        self._mk_btn(btn_row, "Apply times →", self._apply_times,
                     fg=ACCENT2).pack(side="left")
        tk.Label(btn_row, text="(edit times above, then click)", bg=BG,
                 fg=TEXT_DIM, font=FONT_TINY).pack(side="left", padx=8)

        self.dur_var = tk.StringVar(value="Clip length: —")
        tk.Label(right, textvariable=self.dur_var, bg=BG, fg=TEXT_DIM,
                 font=FONT_TINY, anchor="w").pack(fill="x")

        # Divider
        tk.Frame(right, bg=MUTED, height=1).pack(fill="x", pady=4)

        # Output row
        out_row = tk.Frame(right, bg=BG)
        out_row.pack(fill="x", pady=1)
        tk.Label(out_row, text="OUTPUT", bg=BG, fg=TEXT_DIM,
                 font=FONT_TINY).pack(anchor="w")
        self.out_var = tk.StringVar(value="")
        self.out_entry = tk.Entry(
            out_row, textvariable=self.out_var,
            bg=MUTED, fg=TEXT, insertbackground=TEXT,
            relief="flat", font=FONT_MONO,
            highlightthickness=1, highlightcolor=ACCENT2,
            highlightbackground=MUTED
        )
        self.out_entry.pack(side="left", fill="x", expand=True)
        self._mk_btn(out_row, "…", self._browse_out, fg=TEXT_DIM,
                     padx=6).pack(side="left", padx=(4, 0))

        # Progress bar
        prog_frame = tk.Frame(right, bg=BG)
        prog_frame.pack(fill="x", pady=2)
        self.prog_canvas = tk.Canvas(prog_frame, bg=MUTED, height=6,
                                     highlightthickness=0)
        self.prog_canvas.pack(fill="x")
        self.prog_bar = None
        self.status_var = tk.StringVar(value="")
        tk.Label(prog_frame, textvariable=self.status_var, bg=BG,
                 fg=TEXT_DIM, font=FONT_TINY, anchor="w").pack(fill="x")

        # Cut button
        cut_btn = tk.Button(
            right, text="✂  CUT CLIP",
            bg=ACCENT, fg=BG, activebackground=ACCENT2, activeforeground=TEXT,
            font=("Helvetica", 12, "bold"), relief="flat",
            padx=24, pady=8, cursor="hand2",
            command=self._do_cut
        )
        cut_btn.pack(pady=6)
        self.cut_btn = cut_btn

    # ── preview panel (left column) ───────────────────────
    def _update_preview_panel(self, t: float, marker: str):
        """Called while dragging; fetch frame via global extractor (max 1 ffmpeg)."""
        if not HAS_PIL or not self.src_path:
            return
        src   = self.src_path
        PREV_W = self.preview_canvas.winfo_width()  or 320
        PREV_H = self.preview_canvas.winfo_height() or 200

        self.preview_tc_var.set(seconds_to_hms(t))
        self.preview_marker_var.set("▶ IN marker" if marker == "in" else "▶ OUT marker")

        def on_done(tmp):
            self.root.after(0, lambda: self._paint_preview(tmp, PREV_W, PREV_H))

        _frame_extractor.request(src, t, PREV_W, on_done)

    def _paint_preview(self, tmp_path, w: int, h: int):
        """Draw extracted frame onto the preview canvas, scaled to fit."""
        canvas = self.preview_canvas
        if not canvas.winfo_exists():
            return
        canvas.delete("all")
        if tmp_path and HAS_PIL:
            try:
                img = Image.open(tmp_path)
                img.thumbnail((w, h), Image.LANCZOS)
                bg_img = Image.new("RGB", (w, h), "#0a0a10")
                ox = (w - img.width)  // 2
                oy = (h - img.height) // 2
                bg_img.paste(img, (ox, oy))
                photo = ImageTk.PhotoImage(bg_img)
                self._preview_img_ref = photo
                canvas.create_image(0, 0, anchor="nw", image=photo)
            except Exception:
                canvas.create_text(w // 2, h // 2, text="preview error",
                                   fill=TEXT_DIM, font=FONT_UI)
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        else:
            canvas.create_text(w // 2, h // 2,
                               text="install Pillow\nfor previews",
                               fill=TEXT_DIM, font=FONT_UI, justify="center")

    def _show_first_frame(self):
        """Show frame at t=0 when a file is loaded."""
        if not self.src_path:
            return
        PREV_W = self.preview_canvas.winfo_width()  or 320
        PREV_H = self.preview_canvas.winfo_height() or 200
        self.preview_tc_var.set(seconds_to_hms(0.0))
        self.preview_marker_var.set("first frame")

        def on_done(tmp):
            self.root.after(0, lambda: self._paint_preview(tmp, PREV_W, PREV_H))

        _frame_extractor.request(self.src_path, 0.0, PREV_W, on_done)

    # ── small helper ──────────────────────────────────────
    def _mk_btn(self, parent, text, cmd, fg=TEXT, padx=10, **kw):
        return tk.Button(parent, text=text, command=cmd,
                         bg=MUTED, fg=fg, activebackground=ACCENT2,
                         relief="flat", font=FONT_UI, padx=padx,
                         cursor="hand2", **kw)

    # ── file loading ──────────────────────────────────────
    def _browse(self, *_):
        path = filedialog.askopenfilename(
            title="Select video",
            filetypes=[("Video files",
                        "*.mp4 *.mkv *.mov *.avi *.webm *.ts *.m4v *.flv"),
                       ("All files", "*.*")]
        )
        if path:
            self._load(path)

    def _on_drop(self, event):
        raw = event.data
        # tkinterdnd2 wraps paths with spaces in {}
        path = raw.strip("{}")
        if os.path.isfile(path):
            self._load(path)

    def _load(self, path: str):
        self.src_path = path
        self.duration = probe_duration(path)
        name = os.path.basename(path)
        dur  = seconds_to_hms(self.duration)
        self.info_var.set(f"📁  {name}   ·   {dur}")
        self.drop_label.config(text=f"✔  {name}", fg=SUCCESS)

        # reset slider
        self.slider.duration = max(self.duration, 1.0)
        self.slider._in  = 0.0
        self.slider._out = self.duration
        self.slider._redraw()

        # load thumbnail strip
        self.thumb_strip.load(path, self.duration)

        # reset time entries
        self.te_in.set(0.0)
        self.te_out.set(self.duration)
        self._update_dur_label(0.0, self.duration)

        # suggest output path
        base, ext = os.path.splitext(path)
        self.out_var.set(base + "_trimmed" + ext)

        self.status_var.set("")
        self._set_progress(0)
        # show first frame in preview panel
        self.root.after(50, self._show_first_frame)

    # ── slider / time sync ────────────────────────────────
    def _slider_moved(self, t_in: float, t_out: float):
        self.te_in.set(t_in)
        self.te_out.set(t_out)
        self._update_dur_label(t_in, t_out)

    def _apply_times(self):
        t_in  = self.te_in.get()
        t_out = self.te_out.get()
        if t_in >= t_out:
            messagebox.showerror("Invalid range", "IN must be before OUT.")
            return
        self.slider.set_range(t_in, t_out)
        self._update_dur_label(t_in, t_out)

    def _update_dur_label(self, t_in, t_out):
        clip = max(0.0, t_out - t_in)
        self.dur_var.set(f"Clip length:  {seconds_to_hms(clip)}")

    # ── output path ───────────────────────────────────────
    def _browse_out(self):
        path = filedialog.asksaveasfilename(
            title="Save trimmed clip as…",
            defaultextension=".mp4",
            filetypes=[("Video files",
                        "*.mp4 *.mkv *.mov *.avi *.webm *.ts *.m4v"),
                       ("All files", "*.*")]
        )
        if path:
            self.out_var.set(path)

    # ── progress ──────────────────────────────────────────
    def _set_progress(self, pct: float):
        self.prog_canvas.update_idletasks()
        w = self.prog_canvas.winfo_width()
        self.prog_canvas.delete("all")
        if pct > 0:
            self.prog_canvas.create_rectangle(
                0, 0, int(w * pct), 6,
                fill=ACCENT, outline=""
            )

    # ── cut ───────────────────────────────────────────────
    def _do_cut(self):
        if not self.src_path:
            messagebox.showwarning("No file", "Please load a video first.")
            return
        out = self.out_var.get().strip()
        if not out:
            messagebox.showwarning("No output", "Please set an output path.")
            return

        t_in  = self.slider.value_in
        t_out = self.slider.value_out
        if t_in >= t_out:
            messagebox.showerror("Invalid range", "IN must be before OUT.")
            return

        self.cut_btn.config(state="disabled", text="Cutting…")
        self.status_var.set("⏳  Running FFmpeg (stream copy)…")

        def on_progress(pct):
            self.root.after(0, self._set_progress, pct)

        def on_done(code):
            def _finish():
                self.cut_btn.config(state="normal", text="✂  CUT CLIP")
                if code == 0:
                    self._set_progress(1.0)
                    self.status_var.set(f"✔  Saved → {out}")
                    messagebox.showinfo("Done",
                                        f"Clip saved:\n{out}")
                else:
                    self._set_progress(0)
                    self.status_var.set("✘  FFmpeg error — check paths")
                    messagebox.showerror("FFmpeg error",
                                         "Something went wrong.\n"
                                         "Make sure ffmpeg is installed.")
            self.root.after(0, _finish)

        ffmpeg_cut(self.src_path, out, t_in, t_out, on_progress, on_done)


# ─── Entry point ───────────────────────────────────────────────────────────────

def main():
    if HAS_DND:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()

    app = VideoTrimmerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
