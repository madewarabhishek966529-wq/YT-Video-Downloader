"""
settings.py
-----------
Application settings manager. Loads and saves config.json and exposes
typed, attribute-style access to settings used throughout the app.

NOTE: This provides the baseline load/save functionality needed by the GUI
(Module 2). The dedicated Settings *view* (validation, folder browser,
concurrency limits UI, etc.) is fleshed out further in Module 8.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict

from utils import CONFIG_FILE, DEFAULT_DOWNLOADS_DIR, logger


@dataclass
class Settings:
    """Typed representation of the app's config.json."""

    theme: str = "dark"
    color_theme: str = "blue"
    default_resolution: str = "best"
    default_download_folder: str = str(DEFAULT_DOWNLOADS_DIR)
    auto_update_ytdlp: bool = True
    auto_merge_audio: bool = True
    open_folder_after_download: bool = False
    max_concurrent_downloads: int = 2
    last_used_folder: str = str(DEFAULT_DOWNLOADS_DIR)
    window_geometry: str = "1200x750"
    sidebar_expanded: bool = True
    check_clipboard: bool = True
    ffmpeg_path: str = ""

    # --- Persistence ------------------------------------------------------

    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> "Settings":
        """Load settings from config.json, falling back to defaults on error."""
        defaults = cls()
        if not path.exists():
            logger.info("No config.json found, creating defaults at %s", path)
            defaults.save(path)
            return defaults

        try:
            with open(path, "r", encoding="utf-8") as f:
                data: Dict[str, Any] = json.load(f)
            # Only accept known fields; ignore unexpected/legacy keys
            valid_fields = {f_.name for f_ in cls.__dataclass_fields__.values()}
            filtered = {k: v for k, v in data.items() if k in valid_fields}
            return cls(**{**asdict(defaults), **filtered})
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load config.json (%s). Using defaults.", exc)
            return defaults

    def save(self, path: Path = CONFIG_FILE) -> None:
        """Persist current settings to config.json."""
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, indent=4)
            logger.debug("Settings saved to %s", path)
        except OSError as exc:
            logger.error("Failed to save config.json: %s", exc)

    def update(self, **kwargs: Any) -> None:
        """Update one or more fields and immediately persist to disk."""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                logger.warning("Attempted to set unknown setting: %s", key)
        self.save()
