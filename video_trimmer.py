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


class RangeSlider(tk.Canvas):
    """
    A simple two-handle range slider drawn on a Canvas.
    Handles: IN (green) and OUT (red).
    """
    TRACK_H  = 6
    HANDLE_R = 9
    PAD      = 18

    def __init__(self, parent, duration=100.0, on_change=None, **kw):
        super().__init__(parent, bg=BG, highlightthickness=0,
                         height=40, **kw)
        self.duration  = max(duration, 1.0)
        self.on_change = on_change
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
        else:
            self._out = max(t, self._in + 0.1)
        self._redraw()
        if self.on_change:
            self.on_change(self._in, self._out)

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
        self.root.geometry("700x520")
        self.root.resizable(True, False)

        self.src_path  = None
        self.duration  = 0.0

        self._build_ui()

    # ── UI construction ────────────────────────────────────
    def _build_ui(self):
        root = self.root

        # ── Header ──────────────────────────────────────────
        hdr = tk.Frame(root, bg=BG)
        hdr.pack(fill="x", padx=24, pady=(20, 4))
        tk.Label(hdr, text="✂  VIDEO TRIMMER", bg=BG, fg=ACCENT,
                 font=("Courier New", 15, "bold")).pack(side="left")
        tk.Label(hdr, text="stream copy · no re-encoding", bg=BG,
                 fg=TEXT_DIM, font=FONT_TINY).pack(side="left", padx=12)

        # ── Drop zone ───────────────────────────────────────
        self.drop_frame = tk.Frame(
            root, bg=PANEL, relief="flat",
            highlightthickness=2, highlightbackground=MUTED
        )
        self.drop_frame.pack(fill="x", padx=24, pady=8)

        self.drop_label = tk.Label(
            self.drop_frame,
            text="⬇  Drop video here  or  click to browse",
            bg=PANEL, fg=TEXT_DIM, font=FONT_UI,
            pady=22, cursor="hand2"
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

        # ── File info ───────────────────────────────────────
        self.info_var = tk.StringVar(value="")
        tk.Label(root, textvariable=self.info_var, bg=BG, fg=ACCENT2,
                 font=FONT_MONO, anchor="w").pack(fill="x", padx=26)

        # ── Slider ──────────────────────────────────────────
        slider_frame = tk.Frame(root, bg=BG)
        slider_frame.pack(fill="x", padx=24, pady=(10, 0))
        tk.Label(slider_frame, text="SELECT RANGE", bg=BG, fg=TEXT_DIM,
                 font=FONT_TINY).pack(anchor="w")
        self.slider = RangeSlider(slider_frame, duration=100.0,
                                  on_change=self._slider_moved)
        self.slider.pack(fill="x", pady=4)

        # ── Time entries ────────────────────────────────────
        te_row = tk.Frame(root, bg=BG)
        te_row.pack(fill="x", padx=24, pady=4)

        self.te_in  = TimeEntry(te_row, "IN  (start)")
        self.te_in.pack(side="left", padx=(0, 16))
        self.te_out = TimeEntry(te_row, "OUT (end)")
        self.te_out.pack(side="left")

        # sync buttons
        btn_row = tk.Frame(root, bg=BG)
        btn_row.pack(fill="x", padx=24, pady=(0, 2))
        self._mk_btn(btn_row, "Apply times →", self._apply_times,
                     fg=ACCENT2).pack(side="left")
        tk.Label(btn_row, text="(edit times above, then click)", bg=BG,
                 fg=TEXT_DIM, font=FONT_TINY).pack(side="left", padx=8)

        # duration display
        self.dur_var = tk.StringVar(value="Clip length: —")
        tk.Label(root, textvariable=self.dur_var, bg=BG, fg=TEXT_DIM,
                 font=FONT_TINY, anchor="w").pack(fill="x", padx=26)

        # ── Divider ─────────────────────────────────────────
        tk.Frame(root, bg=MUTED, height=1).pack(fill="x", padx=24, pady=10)

        # ── Output row ──────────────────────────────────────
        out_row = tk.Frame(root, bg=BG)
        out_row.pack(fill="x", padx=24, pady=4)
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

        # ── Progress bar ────────────────────────────────────
        prog_frame = tk.Frame(root, bg=BG)
        prog_frame.pack(fill="x", padx=24, pady=6)
        self.prog_canvas = tk.Canvas(prog_frame, bg=MUTED, height=6,
                                     highlightthickness=0)
        self.prog_canvas.pack(fill="x")
        self.prog_bar = None
        self.status_var = tk.StringVar(value="")
        tk.Label(prog_frame, textvariable=self.status_var, bg=BG,
                 fg=TEXT_DIM, font=FONT_TINY, anchor="w").pack(fill="x")

        # ── Cut button ──────────────────────────────────────
        cut_btn = tk.Button(
            root, text="✂  CUT CLIP",
            bg=ACCENT, fg=BG, activebackground=ACCENT2, activeforeground=TEXT,
            font=("Helvetica", 12, "bold"), relief="flat",
            padx=32, pady=10, cursor="hand2",
            command=self._do_cut
        )
        cut_btn.pack(pady=12)
        self.cut_btn = cut_btn

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

        # reset time entries
        self.te_in.set(0.0)
        self.te_out.set(self.duration)
        self._update_dur_label(0.0, self.duration)

        # suggest output path
        base, ext = os.path.splitext(path)
        self.out_var.set(base + "_trimmed" + ext)

        self.status_var.set("")
        self._set_progress(0)

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
