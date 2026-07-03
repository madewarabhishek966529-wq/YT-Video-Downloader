"""
video_info.py
-------------
Fetches and parses YouTube video/playlist metadata using yt-dlp, without
downloading any media. Used by the Home view to populate the video info
card once a user pastes a link.

Resolution/format parsing for the format-selection table is handled in
video_info.py's `raw_formats` passthrough and consumed by the (upcoming)
Module 4 resolution-detection logic — this module already extracts the
full formats list so Module 4 can build directly on top of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import yt_dlp

from utils import is_playlist_url, is_valid_youtube_url, logger


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class VideoInfoError(Exception):
    """Base exception for all video-info fetch failures."""


class InvalidURLError(VideoInfoError):
    """Raised when the given string isn't a recognizable YouTube URL."""


class PrivateVideoError(VideoInfoError):
    """Raised when the video is private."""


class VideoUnavailableError(VideoInfoError):
    """Raised when the video was deleted, removed, or never existed."""


class GeoRestrictedError(VideoInfoError):
    """Raised when the video is blocked in the user's region."""


class CopyrightRestrictedError(VideoInfoError):
    """Raised when the video is blocked due to a copyright claim."""


class AgeRestrictedError(VideoInfoError):
    """Raised when the video requires age verification / sign-in."""


class NetworkError(VideoInfoError):
    """Raised when the fetch fails due to connectivity issues."""


class LiveStreamNotSupportedError(VideoInfoError):
    """Raised when the URL points to an in-progress live stream."""


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------

@dataclass
class VideoInfo:
    """Metadata for a single YouTube video."""

    video_id: str
    title: str
    channel: str
    duration_seconds: Optional[int]
    view_count: Optional[int]
    upload_date: Optional[str]           # raw YYYYMMDD, format with utils.format_upload_date
    description: str
    thumbnail_url: Optional[str]
    webpage_url: str
    like_count: Optional[int] = None
    is_live: bool = False
    raw_formats: list[dict[str, Any]] = field(default_factory=list)

    def description_preview(self, max_lines: int = 4, max_chars: int = 400) -> str:
        """First few lines of the description, trimmed for display."""
        if not self.description:
            return "No description available."
        lines = self.description.strip().splitlines()
        preview = "\n".join(lines[:max_lines])
        if len(preview) > max_chars:
            preview = preview[:max_chars].rstrip() + "..."
        elif len(lines) > max_lines:
            preview += "..."
        return preview


@dataclass
class PlaylistEntry:
    """Lightweight metadata for one video within a playlist listing."""

    video_id: str
    title: str
    duration_seconds: Optional[int]
    webpage_url: str
    thumbnail_url: Optional[str] = None


@dataclass
class PlaylistInfo:
    """Metadata for a YouTube playlist and its member videos."""

    playlist_id: str
    title: str
    channel: str
    video_count: int
    entries: list[PlaylistEntry] = field(default_factory=list)


# --------------------------------------------------------------------------
# Fetcher
# --------------------------------------------------------------------------

class VideoInfoFetcher:
    """Wraps yt-dlp to fetch video/playlist metadata without downloading."""

    def __init__(self) -> None:
        self._base_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "ignoreerrors": False,
            "extract_flat": False,
            "socket_timeout": 15,
        }

    # -- Public API ----------------------------------------------------

    def fetch_video(self, url: str) -> VideoInfo:
        """Fetch full metadata for a single video URL. Raises VideoInfoError subclasses."""
        if not url or not is_valid_youtube_url(url):
            raise InvalidURLError("That doesn't look like a valid YouTube URL.")

        try:
            with yt_dlp.YoutubeDL(self._base_opts) as ydl:
                raw = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise self._translate_error(exc) from exc
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error fetching video info: %s", exc)
            raise NetworkError(
                "Couldn't reach YouTube. Check your internet connection and try again."
            ) from exc

        if raw is None:
            raise VideoUnavailableError("This video is unavailable.")

        return self._parse_video(raw)

    def fetch_playlist(self, url: str, max_entries: Optional[int] = None) -> PlaylistInfo:
        """Fetch a flat listing of all videos in a playlist (fast, no per-video formats)."""
        if not url or not is_playlist_url(url):
            raise InvalidURLError("That doesn't look like a valid YouTube playlist URL.")

        opts = {
            **self._base_opts,
            "noplaylist": False,
            "extract_flat": True,
        }
        if max_entries:
            opts["playlistend"] = max_entries

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                raw = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise self._translate_error(exc) from exc
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error fetching playlist info: %s", exc)
            raise NetworkError(
                "Couldn't reach YouTube. Check your internet connection and try again."
            ) from exc

        if raw is None or "entries" not in raw:
            raise VideoUnavailableError("This playlist is unavailable or empty.")

        return self._parse_playlist(raw)

    # -- Internal helpers ------------------------------------------------

    @staticmethod
    def _translate_error(exc: "yt_dlp.utils.DownloadError") -> VideoInfoError:
        """Map a yt-dlp DownloadError into a specific, user-friendly exception."""
        msg = str(exc).lower()

        if "private video" in msg:
            return PrivateVideoError("This video is private and can't be accessed.")
        if "video unavailable" in msg or "has been removed" in msg:
            return VideoUnavailableError("This video has been deleted or is unavailable.")
        if "not available in your country" in msg or "geo" in msg and "restrict" in msg:
            return GeoRestrictedError("This video is not available in your region.")
        if "copyright" in msg:
            return CopyrightRestrictedError("This video is blocked due to a copyright claim.")
        if "sign in to confirm your age" in msg or "age" in msg and "restrict" in msg:
            return AgeRestrictedError("This video is age-restricted and requires sign-in.")
        if any(term in msg for term in ["urlopen", "timed out", "network", "connection", "resolve host"]):
            return NetworkError("Couldn't reach YouTube. Check your internet connection and try again.")
        if "premieres in" in msg or "live event will begin" in msg:
            return LiveStreamNotSupportedError("This is an upcoming live stream and can't be fetched yet.")

        logger.warning("Unrecognized yt-dlp error, passing through generically: %s", exc)
        return VideoInfoError(f"Couldn't load this video: {exc}")

    @staticmethod
    def _parse_video(raw: dict[str, Any]) -> VideoInfo:
        return VideoInfo(
            video_id=raw.get("id", ""),
            title=raw.get("title", "Untitled"),
            channel=raw.get("uploader") or raw.get("channel") or "Unknown channel",
            duration_seconds=raw.get("duration"),
            view_count=raw.get("view_count"),
            upload_date=raw.get("upload_date"),
            description=raw.get("description") or "",
            thumbnail_url=raw.get("thumbnail"),
            webpage_url=raw.get("webpage_url") or raw.get("original_url", ""),
            like_count=raw.get("like_count"),
            is_live=bool(raw.get("is_live")),
            raw_formats=raw.get("formats") or [],
        )

    @staticmethod
    def _parse_playlist(raw: dict[str, Any]) -> PlaylistInfo:
        entries: list[PlaylistEntry] = []
        for entry in raw.get("entries") or []:
            if entry is None:
                continue  # yt-dlp inserts None for unavailable playlist items
            entries.append(
                PlaylistEntry(
                    video_id=entry.get("id", ""),
                    title=entry.get("title", "Untitled"),
                    duration_seconds=entry.get("duration"),
                    webpage_url=entry.get("url") or entry.get("webpage_url", ""),
                    thumbnail_url=entry.get("thumbnail"),
                )
            )
        return PlaylistInfo(
            playlist_id=raw.get("id", ""),
            title=raw.get("title", "Untitled playlist"),
            channel=raw.get("uploader") or raw.get("channel") or "Unknown channel",
            video_count=len(entries),
            entries=entries,
        )


# --------------------------------------------------------------------------
# Resolution / Format Detection (Module 4)
# --------------------------------------------------------------------------
#
# yt-dlp exposes every muxed/video-only/audio-only stream YouTube offers for
# a video via `raw_formats` on VideoInfo. This section turns that raw list
# into a clean, deduplicated table of resolution choices for the UI:
# one row per resolution, showing FPS, codec, whether audio is already
# included (progressive) or needs merging (DASH video-only), and an
# estimated total download size.

STANDARD_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144]


@dataclass
class FormatOption:
    """A single selectable resolution row for the UI table."""

    format_id: str
    resolution_label: str      # e.g. "1080p", "1080p (4K source)" not used, just "2160p (4K)"
    height: int
    fps: Optional[int]
    ext: str
    vcodec: str
    acodec: Optional[str]
    has_audio: bool
    filesize_bytes: Optional[int]        # size of the video stream alone
    estimated_total_bytes: Optional[int]  # video + (merged audio, if applicable)
    is_estimated_size: bool               # True if filesize was approximated

    @property
    def audio_status(self) -> str:
        return "Yes" if self.has_audio else "No (auto-merged)"


@dataclass
class AudioOption:
    """A single selectable audio-only stream (for Audio Only downloads)."""

    format_id: str
    ext: str
    acodec: str
    abr_kbps: Optional[float]
    filesize_bytes: Optional[int]


def _best_filesize(fmt: dict[str, Any]) -> tuple[Optional[int], bool]:
    """Return (size_in_bytes, is_estimated). yt-dlp sometimes only has filesize_approx."""
    if fmt.get("filesize"):
        return int(fmt["filesize"]), False
    if fmt.get("filesize_approx"):
        return int(fmt["filesize_approx"]), True
    return None, False


def _resolution_label(height: int) -> str:
    if height >= 2160:
        return "2160p (4K)"
    return f"{height}p"


def get_best_audio_format(raw_formats: list[dict[str, Any]]) -> Optional[AudioOption]:
    """Pick the highest-bitrate audio-only stream, used both for the Audio
    Only download option and to estimate merged file sizes for video-only
    resolutions."""
    audio_formats = [
        f for f in raw_formats
        if (f.get("acodec") not in (None, "none")) and (f.get("vcodec") in (None, "none"))
    ]
    if not audio_formats:
        return None

    def sort_key(f: dict[str, Any]) -> float:
        return f.get("abr") or f.get("tbr") or 0.0

    best = max(audio_formats, key=sort_key)
    size, _ = _best_filesize(best)
    return AudioOption(
        format_id=best.get("format_id", ""),
        ext=best.get("ext", "m4a"),
        acodec=best.get("acodec", "unknown"),
        abr_kbps=best.get("abr"),
        filesize_bytes=size,
    )


def get_resolution_options(raw_formats: list[dict[str, Any]]) -> list[FormatOption]:
    """
    Parse yt-dlp's raw formats list into one FormatOption per standard
    resolution (144p-2160p), picking the best available stream at each
    height and estimating total download size (merging in best audio for
    video-only/DASH formats, since YouTube rarely offers progressive
    muxed streams above 720p).
    """
    video_formats = [
        f for f in raw_formats
        if f.get("vcodec") not in (None, "none") and f.get("height")
    ]
    if not video_formats:
        return []

    best_audio = get_best_audio_format(raw_formats)

    # Group candidate formats by their nearest standard height
    by_height: dict[int, list[dict[str, Any]]] = {}
    for f in video_formats:
        height = f["height"]
        # Snap to nearest standard resolution bucket (handles odd heights
        # like 1082 from some re-encodes)
        nearest = min(STANDARD_HEIGHTS, key=lambda h: abs(h - height))
        by_height.setdefault(nearest, []).append(f)

    options: list[FormatOption] = []
    for height in STANDARD_HEIGHTS:
        candidates = by_height.get(height)
        if not candidates:
            continue

        def rank(f: dict[str, Any]) -> tuple:
            has_audio = f.get("acodec") not in (None, "none")
            fps = f.get("fps") or 0
            tbr = f.get("tbr") or 0
            # Prefer mp4 (broadest compatibility), then progressive (has
            # audio already), then higher fps/bitrate
            is_mp4 = f.get("ext") == "mp4"
            return (is_mp4, has_audio, fps, tbr)

        best = max(candidates, key=rank)
        has_audio = best.get("acodec") not in (None, "none")
        video_size, is_est = _best_filesize(best)

        total_size: Optional[int] = video_size
        total_is_est = is_est
        if not has_audio and video_size is not None and best_audio and best_audio.filesize_bytes:
            total_size = video_size + best_audio.filesize_bytes
            total_is_est = True

        options.append(
            FormatOption(
                format_id=best.get("format_id", ""),
                resolution_label=_resolution_label(height),
                height=height,
                fps=best.get("fps"),
                ext=best.get("ext", "mp4"),
                vcodec=best.get("vcodec", "unknown"),
                acodec=best.get("acodec") if has_audio else None,
                has_audio=has_audio,
                filesize_bytes=video_size,
                estimated_total_bytes=total_size,
                is_estimated_size=total_is_est,
            )
        )

    # Sort highest resolution first
    options.sort(key=lambda o: o.height, reverse=True)
    return options
