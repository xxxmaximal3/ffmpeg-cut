@echo off

echo.
echo === VideoTrimmer Builder ===
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://python.org
    echo        Check "Add Python to PATH" during install.
    pause
    exit /b 1
)

echo Python OK:
python --version

if not exist "ffmpeg.exe" (
    echo.
    echo ERROR: ffmpeg.exe not found in current folder!
    echo.
    echo Download ffmpeg-release-essentials.zip from:
    echo   https://www.gyan.dev/ffmpeg/builds/
    echo Then copy ffmpeg.exe and ffprobe.exe here.
    pause
    exit /b 1
)

if not exist "ffprobe.exe" (
    echo ERROR: ffprobe.exe not found in current folder!
    pause
    exit /b 1
)

echo ffmpeg.exe OK
echo ffprobe.exe OK

echo.
echo [1/3] Installing dependencies...
python -m pip install pyinstaller tkinterdnd2 pillow --quiet --upgrade
if errorlevel 1 (
    echo ERROR: pip install failed
    pause
    exit /b 1
)
echo Dependencies OK

echo.
echo [2/3] Building exe (1-2 minutes)...
python -m PyInstaller VideoTrimmer.spec --noconfirm
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed
    pause
    exit /b 1
)

echo.
echo [3/3] Done!
echo.

if exist "dist\VideoTrimmer.exe" (
    echo Output: dist\VideoTrimmer.exe
    for %%F in ("dist\VideoTrimmer.exe") do echo Size: %%~zF bytes
    echo.
    explorer dist
) else (
    echo WARNING: dist\VideoTrimmer.exe not found
)

pause
