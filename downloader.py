"""
downloader.py
--------------
Core download engine built on yt-dlp. Handles single-video downloads,
quality/format selection (best / worst / custom resolution / video-only /
audio-only), automatic FFmpeg merging of separate video+audio streams,
live progress reporting via callback, and cooperative cancellation.

Playlist iteration and running several downloads at once are handled one
layer up, in queue_manager.py (Module 9) -- this module downloads exactly
one video per DownloadEngine.download() call, and is designed to be safe
to call from a background thread (it never touches the GUI directly).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import yt_dlp

from utils import check_ffmpeg_installed, ensure_dir, logger, resolve_ffmpeg_location


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class DownloadError(Exception):
    """Base exception for all download failures."""


class FFmpegNotFoundError(DownloadError):
    """Raised when a merge/extraction is required but FFmpeg isn't on PATH."""


class DownloadCancelledError(DownloadError):
    """Raised when a download is cancelled mid-transfer by the user."""


class InsufficientDiskSpaceError(DownloadError):
    """Raised when yt-dlp reports it ran out of disk space while writing."""


class _Cancelled(Exception):
    """Internal-only sentinel raised inside a progress hook to unwind
    yt-dlp's download loop as soon as a cancellation is requested."""


# --------------------------------------------------------------------------
# Modes / Requests / Progress / Results
# --------------------------------------------------------------------------

class DownloadMode(str, Enum):
    BEST = "best"
    WORST = "worst"
    CUSTOM_RESOLUTION = "custom_resolution"   # requires `height`
    VIDEO_ONLY = "video_only"                 # requires `format_id`
    AUDIO_ONLY_MP3 = "audio_mp3"
    AUDIO_ONLY_M4A = "audio_m4a"


@dataclass
class DownloadRequest:
    """Everything the engine needs to perform one download."""

    url: str
    output_dir: str
    mode: DownloadMode = DownloadMode.BEST
    height: Optional[int] = None          # used when mode == CUSTOM_RESOLUTION
    format_id: Optional[str] = None       # used when mode == VIDEO_ONLY
    auto_merge_audio: bool = True         # merge separate audio into BEST/WORST/CUSTOM
    filename_template: str = "%(title).150s [%(resolution)s].%(ext)s"
    ffmpeg_path: str = ""                 # optional override; blank = search system PATH


@dataclass
class DownloadProgress:
    """A single progress update, passed to the caller's on_progress callback."""

    status: str                    # "downloading" | "merging" | "finished" | "error"
    percent: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: Optional[int] = None
    speed_bps: Optional[float] = None
    eta_seconds: Optional[int] = None
    filename: str = ""


@dataclass
class DownloadResult:
    """Returned once a download completes successfully."""

    title: str
    filepath: str
    filesize_bytes: Optional[int]
    resolution: str
    ext: str
    elapsed_seconds: float
    webpage_url: str


ProgressCallback = Callable[[DownloadProgress], None]


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class DownloadEngine:
    """
    Downloads a single video/audio stream via yt-dlp, optionally merging
    separate video+audio with FFmpeg, reporting live progress through a
    callback, and supporting cooperative cancellation.

    One DownloadEngine instance is meant to handle one in-flight download
    at a time -- queue_manager.py creates one per active worker slot.
    """

    def __init__(self) -> None:
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Signal the in-progress download to stop as soon as possible.
        Safe to call from any thread (e.g. the GUI thread on a Cancel click)."""
        self._cancel_event.set()

    def reset(self) -> None:
        """Clear a previous cancellation so this engine instance can be reused."""
        self._cancel_event.clear()

    def download(
        self,
        request: DownloadRequest,
        on_progress: Optional[ProgressCallback] = None,
    ) -> DownloadResult:
        """
        Blocking call -- run this on a background thread, never on the GUI
        thread. Raises a DownloadError subclass on failure.
        """
        if self._requires_ffmpeg(request) and not check_ffmpeg_installed(request.ffmpeg_path):
            raise FFmpegNotFoundError(
                "FFmpeg is required to merge or convert audio/video but wasn't "
                "found on your system PATH (or the configured FFmpeg path). "
                "Install it, or set the correct path in Settings, and try again."
            )

        ensure_dir(request.output_dir)
        start_time = time.time()
        result_holder: dict[str, Any] = {}
        opts = self._build_ydl_opts(request, on_progress, result_holder)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(request.url, download=True)
                filepath = result_holder.get("filepath")
                if not filepath:
                    filepath = self._resolve_output_path(ydl, info)
        except _Cancelled:
            raise DownloadCancelledError("Download was cancelled.")
        except yt_dlp.utils.DownloadError as exc:
            raise self._translate_error(exc) from exc
        except DownloadError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error during download: %s", exc)
            raise DownloadError(f"Download failed: {exc}") from exc

        if info is None:
            raise DownloadError("Download failed: no information was returned.")

        elapsed = time.time() - start_time
        size = None
        if filepath and Path(filepath).exists():
            size = Path(filepath).stat().st_size

        return DownloadResult(
            title=info.get("title", "Untitled"),
            filepath=str(filepath) if filepath else "",
            filesize_bytes=size,
            resolution=self._describe_resolution(info, request),
            ext=Path(filepath).suffix.lstrip(".") if filepath else info.get("ext", ""),
            elapsed_seconds=elapsed,
            webpage_url=info.get("webpage_url", request.url),
        )

    # -- yt-dlp option building -----------------------------------------

    @staticmethod
    def _requires_ffmpeg(request: DownloadRequest) -> bool:
        if request.mode in (DownloadMode.AUDIO_ONLY_MP3, DownloadMode.AUDIO_ONLY_M4A):
            return True
        if request.mode in (DownloadMode.BEST, DownloadMode.WORST, DownloadMode.CUSTOM_RESOLUTION):
            return request.auto_merge_audio
        return False

    def _build_ydl_opts(
        self,
        request: DownloadRequest,
        on_progress: Optional[ProgressCallback],
        result_holder: dict[str, Any],
    ) -> dict[str, Any]:
        outtmpl = str(Path(request.output_dir) / request.filename_template)

        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "outtmpl": outtmpl,
            "format": self._format_selector(request),
            "progress_hooks": [self._make_progress_hook(on_progress, result_holder)],
            "postprocessor_hooks": [self._make_postprocessor_hook(on_progress)],
        }

        if request.mode in (DownloadMode.BEST, DownloadMode.WORST, DownloadMode.CUSTOM_RESOLUTION):
            if request.auto_merge_audio:
                opts["merge_output_format"] = "mp4"
        elif request.mode == DownloadMode.AUDIO_ONLY_MP3:
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        elif request.mode == DownloadMode.AUDIO_ONLY_M4A:
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "0",
            }]

        ffmpeg_location = resolve_ffmpeg_location(request.ffmpeg_path)
        if ffmpeg_location:
            opts["ffmpeg_location"] = ffmpeg_location

        return opts

    @staticmethod
    def _format_selector(request: DownloadRequest) -> str:
        """Build a yt-dlp format-selector string for the requested mode."""
        mode = request.mode

        if mode == DownloadMode.BEST:
            if request.auto_merge_audio:
                return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
            return "best"

        if mode == DownloadMode.WORST:
            if request.auto_merge_audio:
                return "worstvideo+worstaudio/worst"
            return "worst"

        if mode == DownloadMode.CUSTOM_RESOLUTION:
            h = request.height or 1080
            if request.auto_merge_audio:
                return (
                    f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
                    f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
                )
            return f"best[height<={h}]"

        if mode == DownloadMode.VIDEO_ONLY:
            # A specific format_id chosen from the Module 4 resolution table,
            # falling back to the best video-only stream if none was supplied.
            return request.format_id or "bestvideo"

        if mode in (DownloadMode.AUDIO_ONLY_MP3, DownloadMode.AUDIO_ONLY_M4A):
            return "bestaudio/best"

        return "best"

    # -- Progress plumbing -------------------------------------------------

    def _make_progress_hook(
        self,
        on_progress: Optional[ProgressCallback],
        result_holder: dict[str, Any],
    ) -> Callable[[dict[str, Any]], None]:
        def hook(d: dict[str, Any]) -> None:
            if self._cancel_event.is_set():
                raise _Cancelled()

            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                percent = (downloaded / total * 100) if total else 0.0
                if on_progress:
                    on_progress(DownloadProgress(
                        status="downloading",
                        percent=min(percent, 100.0),
                        downloaded_bytes=downloaded,
                        total_bytes=total,
                        speed_bps=d.get("speed"),
                        eta_seconds=d.get("eta"),
                        filename=Path(d.get("filename", "")).name,
                    ))
            elif status == "finished":
                # A single stream (video or audio) finished downloading.
                # If a merge/extraction follows, postprocessor_hooks reports it.
                result_holder["filepath"] = d.get("filename")
                if on_progress:
                    on_progress(DownloadProgress(
                        status="merging",
                        percent=100.0,
                        filename=Path(d.get("filename", "")).name,
                    ))
            elif status == "error":
                if on_progress:
                    on_progress(DownloadProgress(status="error", filename=d.get("filename", "")))

        return hook

    @staticmethod
    def _make_postprocessor_hook(
        on_progress: Optional[ProgressCallback],
    ) -> Callable[[dict[str, Any]], None]:
        def hook(d: dict[str, Any]) -> None:
            if d.get("status") == "finished" and on_progress:
                info = d.get("info_dict", {}) or {}
                filepath = info.get("filepath") or info.get("_filename", "")
                on_progress(DownloadProgress(
                    status="finished",
                    percent=100.0,
                    filename=Path(filepath).name if filepath else "",
                ))

        return hook

    # -- Helpers -------------------------------------------------------

    @staticmethod
    def _resolve_output_path(ydl: "yt_dlp.YoutubeDL", info: dict[str, Any]) -> Optional[str]:
        try:
            path = ydl.prepare_filename(info)
            # After postprocessing (merge/extract-audio), the real extension
            # may differ from the pre-download template's guess.
            return info.get("filepath") or path
        except Exception:  # noqa: BLE001
            return info.get("_filename") or info.get("filepath")

    @staticmethod
    def _describe_resolution(info: dict[str, Any], request: DownloadRequest) -> str:
        if request.mode in (DownloadMode.AUDIO_ONLY_MP3, DownloadMode.AUDIO_ONLY_M4A):
            return "Audio only"
        height = info.get("height") or request.height
        return f"{height}p" if height else "Unknown"

    @staticmethod
    def _translate_error(exc: "yt_dlp.utils.DownloadError") -> DownloadError:
        """Map a yt-dlp DownloadError into a specific, user-friendly exception,
        mirroring the pattern used in video_info.py's error translation."""
        msg = str(exc).lower()

        if "no space left" in msg or "disk quota" in msg:
            return InsufficientDiskSpaceError("Not enough disk space to complete this download.")
        if "ffmpeg" in msg and ("not found" in msg or "not installed" in msg):
            return FFmpegNotFoundError(
                "FFmpeg is required to merge or convert audio/video but wasn't "
                "found on your system PATH."
            )
        if any(term in msg for term in ["urlopen", "timed out", "network", "connection", "resolve host"]):
            return DownloadError("Couldn't reach YouTube. Check your internet connection and try again.")
        if "private video" in msg:
            return DownloadError("This video is private and can't be downloaded.")
        if "video unavailable" in msg or "has been removed" in msg:
            return DownloadError("This video has been deleted or is unavailable.")
        if "requested format is not available" in msg:
            return DownloadError("That resolution is no longer available for this video. Try another one.")

        logger.warning("Unrecognized yt-dlp download error: %s", exc)
        return DownloadError(f"Download failed: {exc}")


# --------------------------------------------------------------------------
# yt-dlp self-update (Module 8: Settings)
# --------------------------------------------------------------------------
#
# yt-dlp's format/extraction support changes constantly as YouTube tweaks
# its site, so keeping it current matters more than most dependencies.
# These helpers wrap `pip install --upgrade yt-dlp` in a subprocess -- this
# is deliberately blocking, so callers (the Settings view / app startup)
# must run it on a background thread and never on the GUI thread.

def get_ytdlp_version() -> str:
    """Return the currently-imported yt-dlp version string."""
    return getattr(yt_dlp.version, "__version__", "unknown")


def update_ytdlp(timeout: int = 90) -> tuple[bool, str]:
    """
    Attempt to upgrade yt-dlp to the latest release via pip.

    Returns (success, message). `success` is True both when an update was
    installed and when yt-dlp was already up to date -- it's only False if
    the update attempt itself failed (no internet, pip error, timeout).

    Note: because yt_dlp is already imported in this process, a successful
    upgrade only takes effect after the app is restarted -- the message
    reflects that.
    """
    import subprocess
    import sys

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "Update check timed out. Check your internet connection."
    except Exception as exc:  # noqa: BLE001
        logger.error("yt-dlp update check failed to launch pip: %s", exc)
        return False, f"Couldn't check for updates: {exc}"

    output = f"{result.stdout}\n{result.stderr}"

    if result.returncode != 0:
        logger.error("yt-dlp update via pip failed: %s", output.strip()[:500])
        if "externally-managed-environment" in output:
            return False, (
                "Couldn't update: this Python is externally managed. Update yt-dlp "
                "manually with 'pip install --upgrade yt-dlp --break-system-packages' "
                "or from within a virtual environment."
            )
        return False, "Update check failed. See logs for details."

    if "Successfully installed" in output:
        logger.info("yt-dlp was upgraded via pip.")
        return True, "yt-dlp was updated. Restart the app to use the new version."

    logger.info("yt-dlp is already up to date.")
    return True, "yt-dlp is already up to date."
