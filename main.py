"""
main.py
-------
Application entry point for YouTube Downloader Pro.

Initializes logging and required folders, checks for FFmpeg, and launches
the CustomTkinter GUI.
"""

from __future__ import annotations

import sys

from utils import (
    DEFAULT_DOWNLOADS_DIR,
    LOGS_DIR,
    check_ffmpeg_installed,
    ensure_dir,
    logger,
)


def bootstrap() -> None:
    """Ensure required folders exist and log environment info before launch."""
    ensure_dir(DEFAULT_DOWNLOADS_DIR)
    ensure_dir(LOGS_DIR)

    logger.info("=" * 60)
    logger.info("Starting YouTube Downloader Pro")
    logger.info("Python: %s", sys.version.split()[0])
    logger.info("Platform: %s", sys.platform)

    if not check_ffmpeg_installed():
        logger.warning(
            "FFmpeg was not found on PATH. Merging separate video/audio "
            "streams will fail until it is installed. See README.md for "
            "installation instructions."
        )
    else:
        logger.info("FFmpeg detected on PATH.")


def main() -> None:
    bootstrap()
    try:
        from gui import run_app
        run_app()
    except ImportError as exc:
        logger.critical("Failed to import GUI module: %s", exc)
        print(
            "Error: required GUI dependencies are missing.\n"
            "Run: pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        logger.critical("Unexpected error while running the app: %s", exc, exc_info=True)
        raise


if __name__ == "__main__":
    main()
