"""
history.py
----------
Download history manager: persists completed downloads to history.json
and provides the CRUD operations the History view needs (list, add,
delete, clear).

Kept independent of downloader.py at import time (only used for type
hints under TYPE_CHECKING) so this module never needs yt-dlp/GUI state
to be constructed or unit-tested.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from utils import HISTORY_FILE, logger

if TYPE_CHECKING:  # pragma: no cover
    from downloader import DownloadResult


@dataclass
class HistoryEntry:
    """One completed download, as stored in history.json."""

    id: str
    title: str
    resolution: str
    ext: str
    filesize_bytes: Optional[int]
    filepath: str
    webpage_url: str
    elapsed_seconds: float
    downloaded_at: str  # ISO 8601 timestamp, local time

    # -- Construction ------------------------------------------------------

    @classmethod
    def from_result(cls, result: "DownloadResult") -> "HistoryEntry":
        """Build a new entry from a freshly-completed DownloadResult."""
        return cls(
            id=uuid.uuid4().hex,
            title=result.title or "Untitled",
            resolution=result.resolution or "Unknown",
            ext=result.ext or "",
            filesize_bytes=result.filesize_bytes,
            filepath=result.filepath or "",
            webpage_url=result.webpage_url or "",
            elapsed_seconds=result.elapsed_seconds or 0.0,
            downloaded_at=datetime.now().isoformat(timespec="seconds"),
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["HistoryEntry"]:
        """Rebuild an entry from a history.json record. Returns None (and
        logs) for malformed/legacy records instead of raising, so one bad
        row can't corrupt the whole history load."""
        try:
            return cls(
                id=str(data["id"]),
                title=str(data.get("title") or "Untitled"),
                resolution=str(data.get("resolution") or "Unknown"),
                ext=str(data.get("ext") or ""),
                filesize_bytes=data.get("filesize_bytes"),
                filepath=str(data.get("filepath") or ""),
                webpage_url=str(data.get("webpage_url") or ""),
                elapsed_seconds=float(data.get("elapsed_seconds") or 0.0),
                downloaded_at=str(data.get("downloaded_at") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.error("Skipping malformed history entry: %s", exc)
            return None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    # -- Convenience for the GUI ---------------------------------------

    @property
    def folder(self) -> str:
        return str(Path(self.filepath).parent) if self.filepath else ""

    @property
    def file_exists(self) -> bool:
        return bool(self.filepath) and Path(self.filepath).is_file()

    @property
    def size_label(self) -> str:
        from utils import format_bytes
        return format_bytes(self.filesize_bytes) if self.filesize_bytes else "Unknown size"

    @property
    def date_label(self) -> str:
        try:
            dt = datetime.fromisoformat(self.downloaded_at)
            return dt.strftime("%b %d, %Y \u00b7 %I:%M %p")
        except ValueError:
            return self.downloaded_at or "Unknown date"


class HistoryManager:
    """Loads/saves history.json and exposes CRUD used by the History view.

    Entries are kept newest-first in memory and on disk. Safe to call from
    the GUI thread only (like settings.py, it does simple synchronous file
    I/O -- entries are added right after a download finishes, on the main
    thread via `after()`, never from the download worker thread itself).
    """

    def __init__(self, path: Path = HISTORY_FILE) -> None:
        self.path = path
        self._entries: List[HistoryEntry] = []
        self.load()

    def load(self) -> None:
        """(Re)load entries from disk. Falls back to an empty history on
        any corruption rather than crashing the app."""
        if not self.path.exists():
            self._entries = []
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, list):
                raise ValueError("history.json root must be a list")
            parsed = [HistoryEntry.from_dict(item) for item in raw if isinstance(item, dict)]
            self._entries = [e for e in parsed if e is not None]
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.error("Failed to load history.json (%s). Starting with empty history.", exc)
            self._entries = []
        self._entries.sort(key=lambda e: e.downloaded_at, reverse=True)

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump([e.to_dict() for e in self._entries], f, indent=4)
            logger.debug("History saved to %s (%d entries)", self.path, len(self._entries))
        except OSError as exc:
            logger.error("Failed to save history.json: %s", exc)

    def all(self) -> List[HistoryEntry]:
        """Newest-first list of every entry (a defensive copy)."""
        return list(self._entries)

    def add(self, result: "DownloadResult") -> HistoryEntry:
        """Record a completed download and persist immediately."""
        entry = HistoryEntry.from_result(result)
        self._entries.insert(0, entry)
        self.save()
        return entry

    def delete(self, entry_id: str) -> bool:
        """Remove one entry by id. Returns True if something was removed.
        Does not touch the file on disk -- only the history record."""
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.id != entry_id]
        removed = len(self._entries) != before
        if removed:
            self.save()
        return removed

    def clear(self) -> None:
        """Wipe the entire history (does not delete downloaded files)."""
        self._entries = []
        self.save()

    def __len__(self) -> int:
        return len(self._entries)
