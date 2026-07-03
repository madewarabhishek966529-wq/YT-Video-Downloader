# YouTube Downloader Pro

A professional desktop application (built with Python + CustomTkinter) for downloading
YouTube videos with full resolution selection, playlist support, download queueing,
history tracking, and a modern dark/light UI.

> **Status:** All 10 modules complete. Paste a link, fetch real video info,
> download instantly or queue it in the background (with a configurable
> concurrency limit), track everything in History, and configure it all from
> Settings. The app now also builds into a standalone Windows `.exe` with
> PyInstaller — see section 6 below.

---

## 1. What This Project Does

Once fully built, this app will let you:

- Paste any YouTube video or playlist URL and auto-fetch its info (thumbnail, title,
  channel, duration, views, upload date, description)
- See every available resolution (144p → 4K) with FPS, codec, and file size
- Choose Best/Lowest/Custom quality, Audio-only (MP3/M4A), Video-only, or
  Video+Audio merged automatically via FFmpeg
- Download playlists with per-video selection
- Queue multiple downloads with live progress (speed, ETA, %), all on a
  background thread so the UI never freezes
- Browse download history, reopen files/folders, and manage settings
  (theme, default folder, concurrency, auto-merge, etc.)

---

## 2. Project Structure

```text
youtube_downloader/
│
├── main.py             # App entry point (launches the GUI)
├── gui.py               # CustomTkinter UI: sidebar, Home/Downloads/History/Settings/About
├── downloader.py         # Core yt-dlp download engine + FFmpeg merging
├── video_info.py          # Fetches video/playlist metadata & formats
├── settings.py            # Loads/saves config.json
├── history.py             # Loads/saves history.json
├── queue_manager.py        # Manages concurrent downloads & status
├── utils.py                # Shared helpers: URL validation, formatting, logging
├── build.spec                # PyInstaller build spec (see section 6)
├── config.json              # User settings (theme, folders, etc.) -- created on first run
├── history.json              # Saved download history -- created on first run
├── assets/
│   ├── icons/                 # App icon (app.ico / app.png)
│   └── images/                 # Thumbnails, logo, etc.
├── downloads/                   # Default download output folder
├── logs/                         # Daily rotating log files
├── requirements.txt               # Runtime Python dependencies
└── requirements-dev.txt            # Build-only dependencies (PyInstaller)
```

All 10 modules are implemented and working: settings, GUI shell, video info
fetching, resolution detection, downloading, live progress, history, full
settings (including an FFmpeg path override and yt-dlp update checking), a
multi-item download queue with concurrency limits, and PyInstaller packaging
into a standalone `.exe`.

---

## 3. Requirements

- **Python 3.12+**
- **FFmpeg** (system-installed, must be on PATH) — needed to merge separate
  video+audio streams for high-resolution downloads
- Internet connection (obviously — it's a downloader)

---

## 4. How to Run It

### Step 1 — Extract the project
Unzip `youtube_downloader.zip` to a folder of your choice.

### Step 2 — Open a terminal in the project folder
```bash
cd youtube_downloader
```

### Step 3 — Create and activate a virtual environment (recommended)
```bash
python -m venv venv
```
- **Windows (PowerShell):** `venv\Scripts\Activate.ps1`
- **Windows (cmd):** `venv\Scripts\activate.bat`
- **Mac/Linux:** `source venv/bin/activate`

### Step 4 — Install dependencies
```bash
pip install -r requirements.txt
```

### Step 5 — Install FFmpeg
- **Windows:** Download a build from https://ffmpeg.org/download.html, extract it,
  and add the `bin` folder to your system's `PATH` environment variable.
- **macOS:** `brew install ffmpeg`
- **Linux (Debian/Ubuntu):** `sudo apt install ffmpeg`

Confirm it's installed:
```bash
ffmpeg -version
```
If FFmpeg is installed but not on your system `PATH` (or you want to use a
specific build), set its location in the app under **Settings → FFmpeg path
override** instead of editing your PATH.

### Step 6 — Run the app
```bash
python main.py
```
This opens the app window with a sidebar (Home, Downloads, History, Settings,
About). From Home you can paste a YouTube URL, fetch real video info, pick a
resolution (or Best/Lowest/Audio-only), and either download it right away with a
live progress bar, speed, and ETA, or click **➕ Add to Queue** to send it to the
background queue instead — queue up several videos and they'll download
automatically, honoring the concurrency limit set in Settings. The Downloads tab
shows every queued item's live status (Waiting / Downloading / Completed / Failed
/ Cancelled) with its own progress bar, a Cancel/Remove action, and a "Clear
Finished" button. Every completed download (instant or queued) is automatically
saved to History, where you can reopen the file/folder or delete the record.
Settings lets you configure the default folder/resolution/theme, max concurrent
downloads, an optional FFmpeg path override, and whether yt-dlp checks for
updates automatically on startup (or on demand via **Check for Updates**).

### Step 7 (optional) — Sanity-check what's already working
Even before the GUI exists, you can test the core utility functions:
```bash
python -c "import utils; print(utils.is_valid_youtube_url('https://youtu.be/dQw4w9WgXcQ')); print(utils.check_ffmpeg_installed())"
```
Both lines should print `True`.

---

## 5. Build Progress (Module Checklist)

- [x] 1. Project setup (folders, config, requirements, utils, logging)
- [x] 2. GUI (main window, sidebar, theming)
- [x] 3. Video information fetcher
- [x] 4. Resolution detection
- [x] 5. Download engine
- [x] 6. Progress tracking
- [x] 7. Download history
- [x] 8. Settings
- [x] 9. Queue system
- [x] 10. Packaging (PyInstaller `.exe` build)

---

## 6. Building a Windows `.exe`

The app packages into a single standalone `.exe` with PyInstaller, using the
included `build.spec` rather than a plain CLI command. A plain
`pyinstaller --onefile --windowed main.py` looks like it works but the exe
crashes on launch, because CustomTkinter and CTkTable ship their own runtime
data (theme JSON, fonts) that PyInstaller's static analysis doesn't pick up
automatically. `build.spec` bundles that data plus this project's own
`assets/` folder and app icon, so the packaged build matches
`python main.py` exactly.

### Step 1 — Install build dependencies
On top of the normal `requirements.txt`, install the build-only ones:
```bash
pip install -r requirements.txt -r requirements-dev.txt
```

### Step 2 — Build
```bash
pyinstaller build.spec
```

### Step 3 — Find it
The finished executable is written to:
```text
dist/YouTubeDownloaderPro.exe     (Windows)
dist/YouTubeDownloaderPro         (macOS/Linux)
```
`build/` and `dist/` are safe to delete and rebuild from scratch at any time
(`build.spec` is the source of truth, not the leftover folders).

### Where the .exe keeps its data
Settings (`config.json`), history (`history.json`), the default `downloads/`
folder, and `logs/` are all created **next to the `.exe`**, not inside
PyInstaller's temporary extraction folder — so they persist across runs and
you can move the `.exe` to a USB stick or another machine and take your
settings/history with it. (This is handled in `utils.py`, which detects the
frozen/packaged state via `sys.frozen` and points at the executable's own
folder instead of the temp unpack directory PyInstaller uses at runtime.)

### Rebuilding after code changes
Just re-run `pyinstaller build.spec` — it's not a one-time step. Any time you
edit the source, rebuild to get an updated `.exe`.

### Changing the app icon
Replace `assets/icons/app.ico` (Windows titlebar/taskbar/exe icon) and
`assets/icons/app.png` (used for the in-app window icon on macOS/Linux, where
Tk can't load `.ico` files directly) with your own, then rebuild.

### Debugging a build that won't launch
If the packaged `.exe` closes immediately with no error, temporarily flip
`console=False` to `console=True` in `build.spec` and rebuild — that opens a
console window alongside the app showing the real traceback. Set it back to
`False` before shipping a release build.

---

## 7. Troubleshooting

| Issue | Fix |
|---|---|
| `ModuleNotFoundError` | Make sure your virtual environment is activated and `pip install -r requirements.txt` completed without errors |
| FFmpeg not found | Verify `ffmpeg -version` works in your terminal; re-check PATH setup |
| App won't launch | Confirm Module 2 (GUI) has been delivered — `main.py` is a stub until then |
| Downloads fail for a video | Video may be private, deleted, age-restricted, or region-locked — a specific, user-friendly error message shows right in the progress panel on the Home tab |
| FFmpeg installed but app still can't find it | Set the exact path in **Settings → FFmpeg path override** rather than relying on system PATH |
| "Check for Updates" fails with "externally managed environment" | Your system Python blocks `pip install` outside a virtualenv (PEP 668). Run `pip install --upgrade yt-dlp --break-system-packages`, or better, reinstall inside a virtual environment as shown in Step 3 |
| Queued downloads aren't starting | Check the Downloads tab — items past your concurrency limit sit as "Waiting" until a slot frees up. Raise **Max Concurrent Downloads** in Settings if you want more running at once |
| Packaged `.exe` closes instantly with no error | Set `console=True` in `build.spec` and rebuild to see the real traceback (see section 6) |
| Packaged `.exe` can't find its theme / crashes on the CustomTkinter window | Make sure you built with `pyinstaller build.spec`, not a plain `pyinstaller main.py` — the spec bundles CustomTkinter/CTkTable's required data files that a plain build misses |
#   Y T - V i d e o - D o w n l o a d e r  
 #   Y T - V i d e o - D o w n l o a d e r  
 