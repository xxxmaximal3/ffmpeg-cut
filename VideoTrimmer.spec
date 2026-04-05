# VideoTrimmer.spec
# PyInstaller spec — bundles ffmpeg.exe + ffprobe.exe into a single .exe
#
# Usage:
#   pyinstaller VideoTrimmer.spec
#
# Requirements:
#   pip install pyinstaller tkinterdnd2
#   Place ffmpeg.exe and ffprobe.exe next to this file before building.
#   Download from: https://www.gyan.dev/ffmpeg/builds/  (ffmpeg-release-essentials.zip)

import os
from PyInstaller.utils.hooks import collect_data_files

# ── Locate ffmpeg binaries ────────────────────────────────────────────────────
# Expect ffmpeg.exe and ffprobe.exe in the same directory as this .spec file.
spec_dir = os.path.dirname(os.path.abspath(SPEC))

ffmpeg_exe  = os.path.join(spec_dir, "ffmpeg.exe")
ffprobe_exe = os.path.join(spec_dir, "ffprobe.exe")

binaries = []
for exe in (ffmpeg_exe, ffprobe_exe):
    if os.path.isfile(exe):
        # (source_path, dest_folder_inside_bundle)
        binaries.append((exe, "."))
    else:
        import warnings
        warnings.warn(f"WARNING: {exe} not found — ffmpeg will NOT be bundled!")

# ── tkinterdnd2 data files ────────────────────────────────────────────────────
datas = collect_data_files("tkinterdnd2")

# ─────────────────────────────────────────────────────────────────────────────

a = Analysis(
    ["video_trimmer.py"],
    pathex=[spec_dir],
    binaries=binaries,
    datas=datas,
    hiddenimports=["tkinterdnd2"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VideoTrimmer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,           # compress with UPX if available (reduces size)
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # no black console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=None,          # replace with "icon.ico" if you have one
)
