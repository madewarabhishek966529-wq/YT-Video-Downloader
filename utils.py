"""
utils.py
--------
Shared utility functions, constants, and logging configuration used across
the YouTube Downloader application.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------------
# Paths & Constants
# --------------------------------------------------------------------------

APP_NAME = "YouTube Downloader Pro"
APP_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# Two different notions of "app directory" matter once this is packaged
# with PyInstaller:
#
#   RESOURCE_DIR -- where bundled, read-only assets (icons/images) live.
#     In a normal `python main.py` run that's just the project folder.
#     In a frozen --onefile .exe, PyInstaller unpacks bundled data into a
#     temporary folder at sys._MEIPASS at startup, which disappears when
#     the app closes -- fine for read-only assets, but NOT for anything
#     that must persist.
#
#   DATA_DIR -- where user data (config.json, history.json, downloads/,
#     logs/) is read from and written to. This must be a stable, writable
#     location next to the executable, not the ephemeral _MEIPASS folder,
#     or every setting/history entry would vanish the moment the app closes.
#     For a frozen build that's the folder containing the .exe; for a
#     source run it's the same project folder RESOURCE_DIR uses.
# --------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    DATA_DIR = Path(sys.executable).resolve().parent
else:
    RESOURCE_DIR = Path(__file__).resolve().parent
    DATA_DIR = RESOURCE_DIR

BASE_DIR = DATA_DIR  # kept for backwards compatibility with earlier modules

ASSETS_DIR = RESOURCE_DIR / "assets"
ICONS_DIR = ASSETS_DIR / "icons"
IMAGES_DIR = ASSETS_DIR / "images"
LOGS_DIR = DATA_DIR / "logs"
DEFAULT_DOWNLOADS_DIR = DATA_DIR / "downloads"

CONFIG_FILE = DATA_DIR / "config.json"
HISTORY_FILE = DATA_DIR / "history.json"

# Regex for validating / extracting YouTube video & playlist IDs
YOUTUBE_URL_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?"
    r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)

YOUTUBE_PLAYLIST_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?youtube\.com/.*[?&]list=([A-Za-z0-9_-]+)"
)

RESOLUTION_ORDER = [
    "2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p"
]

STATUS_WAITING = "Waiting"
STATUS_DOWNLOADING = "Downloading"
STATUS_COMPLETED = "Completed"
STATUS_FAILED = "Failed"
STATUS_CANCELLED = "Cancelled"
STATUS_PAUSED = "Paused"


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def setup_logger(name: str = "ytdownloader") -> logging.Logger:
    """
    Configure and return an application-wide logger that writes to both
    the console and a rotating daily log file under logs/.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"

    logger = logging.getLogger(name)
    if logger.handlers:
        # Logger already configured (avoid duplicate handlers on re-import)
        return logger

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


logger = setup_logger()


# --------------------------------------------------------------------------
# Validation Helpers
# --------------------------------------------------------------------------

def is_valid_youtube_url(url: str) -> bool:
    """Return True if the given string looks like a valid YouTube URL."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    return bool(YOUTUBE_URL_REGEX.search(url)) or bool(
        YOUTUBE_PLAYLIST_REGEX.search(url)
    )


def is_playlist_url(url: str) -> bool:
    """Return True if the URL contains a playlist identifier."""
    return bool(YOUTUBE_PLAYLIST_REGEX.search(url or ""))


def extract_video_id(url: str) -> Optional[str]:
    """Extract the 11-character YouTube video ID from a URL, if present."""
    match = YOUTUBE_URL_REGEX.search(url or "")
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# Formatting Helpers
# --------------------------------------------------------------------------

def format_bytes(size: Optional[float]) -> str:
    """Convert a byte count into a human-readable string (e.g. '12.4 MB')."""
    if size is None or size < 0:
        return "Unknown"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def format_speed(bytes_per_sec: Optional[float]) -> str:
    """Format a download speed in bytes/sec as a readable string."""
    if bytes_per_sec is None or bytes_per_sec <= 0:
        return "-- KB/s"
    return f"{format_bytes(bytes_per_sec)}/s"


def format_eta(seconds: Optional[int]) -> str:
    """Format seconds remaining into mm:ss or hh:mm:ss."""
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def format_duration(seconds: Optional[int]) -> str:
    """Format a video duration (seconds) into hh:mm:ss or mm:ss."""
    if seconds is None:
        return "Unknown"
    seconds = int(seconds)
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def format_views(views: Optional[int]) -> str:
    """Format a view count with thousands separators (e.g. '1,234,567 views')."""
    if views is None:
        return "Unknown views"
    return f"{views:,} views"


def format_upload_date(date_str: Optional[str]) -> str:
    """Convert a yt-dlp upload_date string (YYYYMMDD) into 'Jan 05, 2024'."""
    if not date_str:
        return "Unknown date"
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        return dt.strftime("%b %d, %Y")
    except ValueError:
        return date_str


def sanitize_filename(name: str) -> str:
    """Remove characters that are illegal in Windows/Mac/Linux filenames."""
    if not name:
        return "untitled"
    sanitized = re.sub(r'[<>:"/\\|?*]', "_", name)
    sanitized = sanitized.strip().rstrip(".")
    return sanitized[:180] if len(sanitized) > 180 else sanitized


def open_in_file_explorer(path: str) -> None:
    """Open the given file or folder in the OS's native file explorer."""
    path = os.path.normpath(path)
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}"')
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to open path %s: %s", path, exc)


def ensure_dir(path: os.PathLike | str) -> Path:
    """Create a directory (and parents) if it doesn't already exist."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def check_ffmpeg_installed(custom_path: str = "") -> bool:
    """Check whether FFmpeg is available.

    If `custom_path` is given (a user-configured override, e.g. from
    Settings.ffmpeg_path), it is checked first -- either a direct path to
    the ffmpeg executable itself, or a directory containing it. Falls back
    to the system PATH otherwise.
    """
    import shutil

    if custom_path:
        p = Path(custom_path)
        if p.is_file():
            return True
        exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        if p.is_dir() and (p / exe_name).is_file():
            return True
        # Also allow the user to have pointed at the folder via PATH-style
        # lookup, in case they typed a bare directory that shutil can use.
        found = shutil.which("ffmpeg", path=str(p))
        if found:
            return True
        return False

    return shutil.which("ffmpeg") is not None


def resolve_ffmpeg_location(custom_path: str = "") -> Optional[str]:
    """Return the path yt-dlp's `ffmpeg_location` option should use, or
    None to let yt-dlp fall back to searching the system PATH itself."""
    if not custom_path:
        return None
    return custom_path if Path(custom_path).exists() else None
