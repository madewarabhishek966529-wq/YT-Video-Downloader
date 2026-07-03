"""
queue_manager.py
----------------
Multi-video download queue: a pool of worker threads that pull waiting
items and run them through DownloadEngine, honoring a configurable
concurrency cap, plus per-item status tracking (Waiting / Downloading /
Completed / Failed / Cancelled).

This module owns no Tk widgets -- QueueManager runs entirely on
background threads. State changes are reported through an `on_update`
callback that fires on whatever thread produced the change; the GUI is
responsible for marshaling that onto the main thread (e.g. via
`self.after(0, ...)`), exactly like DownloadEngine's progress callback
in downloader.py.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

from downloader import (
    DownloadCancelledError,
    DownloadEngine,
    DownloadError,
    DownloadProgress,
    DownloadRequest,
    DownloadResult,
)
from utils import logger


class QueueStatus(str, Enum):
    WAITING = "Waiting"
    DOWNLOADING = "Downloading"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"

    @property
    def is_finished(self) -> bool:
        return self in (QueueStatus.COMPLETED, QueueStatus.FAILED, QueueStatus.CANCELLED)


@dataclass
class QueueItem:
    """One entry in the download queue."""

    id: int
    title: str
    request: DownloadRequest
    status: QueueStatus = QueueStatus.WAITING
    progress: Optional[DownloadProgress] = None
    result: Optional[DownloadResult] = None
    error: Optional[str] = None
    engine: DownloadEngine = field(default_factory=DownloadEngine)


ItemCallback = Callable[[QueueItem], None]


class QueueManager:
    """
    Owns the ordered list of QueueItems and dispatches them to worker
    threads without exceeding `max_concurrent` simultaneous downloads.

    Thread-safety: all mutation of internal bookkeeping happens under
    `self._lock`. `on_update` is invoked from whichever thread produced
    the change (main thread on add/cancel/remove, a worker thread during
    an active download) -- callers must marshal it onto the GUI thread
    themselves if they touch Tk widgets from it.
    """

    def __init__(self, max_concurrent: int = 2, on_update: Optional[ItemCallback] = None) -> None:
        self.max_concurrent = max(1, max_concurrent)
        self.on_update = on_update
        self._items: Dict[int, QueueItem] = {}
        self._order: List[int] = []
        self._lock = threading.Lock()
        self._id_counter = itertools.count(1)
        self._active_count = 0
        self._shutdown = False

    # -- Public API ------------------------------------------------------

    def set_max_concurrent(self, n: int) -> None:
        """Update the concurrency cap (e.g. when Settings changes) and
        immediately try to fill any newly-freed worker slots."""
        with self._lock:
            self.max_concurrent = max(1, n)
        self._dispatch()

    def add(self, request: DownloadRequest, title: str) -> int:
        """Add a new item to the end of the queue and kick off dispatch.
        Returns the new item's id."""
        item = QueueItem(id=next(self._id_counter), title=title, request=request)
        with self._lock:
            self._items[item.id] = item
            self._order.append(item.id)
        self._notify(item)
        self._dispatch()
        return item.id

    def cancel(self, item_id: int) -> None:
        """Cancel a waiting item outright, or signal an in-flight download
        to stop as soon as possible (cooperative cancellation)."""
        notify_item: Optional[QueueItem] = None
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return
            if item.status == QueueStatus.WAITING:
                item.status = QueueStatus.CANCELLED
                notify_item = item
            elif item.status == QueueStatus.DOWNLOADING:
                item.engine.cancel()  # worker thread finishes the transition
        if notify_item is not None:
            self._notify(notify_item)
        self._dispatch()

    def remove(self, item_id: int) -> None:
        """Remove a finished item from the queue entirely. No-op for
        items that are still waiting or downloading -- cancel() first."""
        with self._lock:
            item = self._items.get(item_id)
            if item is None or not item.status.is_finished:
                return
            del self._items[item_id]
            if item_id in self._order:
                self._order.remove(item_id)

    def clear_finished(self) -> List[int]:
        """Remove every Completed/Failed/Cancelled item. Returns the ids
        that were removed, so the GUI can drop their rows."""
        removed: List[int] = []
        with self._lock:
            for item_id in list(self._order):
                item = self._items[item_id]
                if item.status.is_finished:
                    del self._items[item_id]
                    self._order.remove(item_id)
                    removed.append(item_id)
        return removed

    def items(self) -> List[QueueItem]:
        """Snapshot of all items in queue order."""
        with self._lock:
            return [self._items[i] for i in self._order if i in self._items]

    def counts(self) -> Dict[str, int]:
        with self._lock:
            out = {s.value: 0 for s in QueueStatus}
            for item in self._items.values():
                out[item.status.value] += 1
            return out

    def shutdown(self) -> None:
        """Best-effort stop of everything in-flight/waiting (app close)."""
        with self._lock:
            self._shutdown = True
            for item in self._items.values():
                if item.status == QueueStatus.DOWNLOADING:
                    item.engine.cancel()
                elif item.status == QueueStatus.WAITING:
                    item.status = QueueStatus.CANCELLED

    # -- Dispatch / worker machinery --------------------------------------

    def _dispatch(self) -> None:
        """Promote waiting items to Downloading and spin up worker threads
        for them, up to the concurrency cap. Idempotent/safe to call
        repeatedly (e.g. after every add/cancel/finish)."""
        to_start: List[QueueItem] = []
        with self._lock:
            if self._shutdown:
                return
            free_slots = self.max_concurrent - self._active_count
            if free_slots <= 0:
                return
            for item_id in self._order:
                if free_slots <= 0:
                    break
                item = self._items.get(item_id)
                if item is not None and item.status == QueueStatus.WAITING:
                    item.status = QueueStatus.DOWNLOADING
                    self._active_count += 1
                    to_start.append(item)
                    free_slots -= 1

        for item in to_start:
            self._notify(item)
            threading.Thread(target=self._run_item, args=(item,), daemon=True).start()

    def _run_item(self, item: QueueItem) -> None:
        """Worker thread body: runs exactly one queued download start to
        finish, then frees its slot and tries to dispatch the next one."""

        def on_progress(p: DownloadProgress) -> None:
            item.progress = p
            self._notify(item)

        try:
            result = item.engine.download(item.request, on_progress=on_progress)
        except DownloadCancelledError:
            item.status = QueueStatus.CANCELLED
        except DownloadError as exc:
            item.status = QueueStatus.FAILED
            item.error = str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error in queue worker for item %s: %s", item.id, exc)
            item.status = QueueStatus.FAILED
            item.error = f"Unexpected error: {exc}"
        else:
            item.status = QueueStatus.COMPLETED
            item.result = result

        with self._lock:
            self._active_count -= 1
        self._notify(item)
        self._dispatch()

    def _notify(self, item: QueueItem) -> None:
        if self.on_update is None:
            return
        try:
            self.on_update(item)
        except Exception as exc:  # noqa: BLE001
            logger.error("Queue update callback raised: %s", exc)
