"""
gui.py
------
CustomTkinter GUI for YouTube Downloader Pro.

Builds the main application window: a sidebar for navigation between
Home, Downloads, History, Settings, and About views, plus a themeable,
responsive layout.

Download/queue/history logic is wired up in later modules (3-9). For now,
each view is fully laid out and interactive where it doesn't depend on
those modules yet, with clear placeholders where functionality is pending.
"""

from __future__ import annotations

import io
import os
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
import requests
from CTkTable import CTkTable
from PIL import Image

from downloader import (
    DownloadCancelledError,
    DownloadEngine,
    DownloadError,
    DownloadMode,
    DownloadProgress,
    DownloadRequest,
    DownloadResult,
    FFmpegNotFoundError,
    get_ytdlp_version,
    update_ytdlp,
)
from history import HistoryEntry, HistoryManager
from queue_manager import QueueItem, QueueManager, QueueStatus
from settings import Settings
from utils import (
    APP_NAME,
    APP_VERSION,
    ICONS_DIR,
    check_ffmpeg_installed,
    format_bytes,
    format_duration,
    format_eta,
    format_speed,
    format_upload_date,
    format_views,
    is_playlist_url,
    is_valid_youtube_url,
    logger,
    open_in_file_explorer,
)
from video_info import (
    FormatOption,
    PlaylistInfo,
    VideoInfo,
    VideoInfoError,
    VideoInfoFetcher,
    get_resolution_options,
)

try:
    import pyperclip
    _CLIPBOARD_AVAILABLE = True
except ImportError:  # pragma: no cover
    _CLIPBOARD_AVAILABLE = False


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------

class Sidebar(ctk.CTkFrame):
    """Left-hand navigation sidebar with app branding and nav buttons."""

    NAV_ITEMS = ["Home", "Downloads", "History", "Settings", "About"]

    def __init__(self, master: ctk.CTk, on_navigate: Callable[[str], None]) -> None:
        super().__init__(master, width=200, corner_radius=0)
        self.on_navigate = on_navigate
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        self.grid_rowconfigure(len(self.NAV_ITEMS) + 2, weight=1)

        self._build_branding()
        self._build_nav_buttons()
        self._build_theme_switch()

    def _build_branding(self) -> None:
        logo_label = ctk.CTkLabel(
            self,
            text="▶ YT Downloader",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        logo_label.grid(row=0, column=0, padx=20, pady=(24, 4), sticky="w")

        version_label = ctk.CTkLabel(
            self,
            text=f"v{APP_VERSION} Pro",
            font=ctk.CTkFont(size=11),
            text_color="gray60",
        )
        version_label.grid(row=1, column=0, padx=20, pady=(0, 24), sticky="w")

    def _build_nav_buttons(self) -> None:
        icons = {
            "Home": "🏠",
            "Downloads": "⬇",
            "History": "🕑",
            "Settings": "⚙",
            "About": "ℹ",
        }
        for i, name in enumerate(self.NAV_ITEMS, start=2):
            btn = ctk.CTkButton(
                self,
                text=f"  {icons.get(name, '')}   {name}",
                anchor="w",
                fg_color="transparent",
                text_color=("gray10", "gray90"),
                hover_color=("gray80", "gray28"),
                font=ctk.CTkFont(size=14),
                height=40,
                command=lambda n=name: self.on_navigate(n),
            )
            btn.grid(row=i, column=0, padx=12, pady=4, sticky="ew")
            self.nav_buttons[name] = btn

    def _build_theme_switch(self) -> None:
        self.theme_switch = ctk.CTkSwitch(
            self,
            text="Dark Mode",
            command=self._on_theme_toggle,
            onvalue="dark",
            offvalue="light",
        )
        self.theme_switch.grid(
            row=len(self.NAV_ITEMS) + 3, column=0, padx=20, pady=24, sticky="w"
        )

    def set_theme_switch(self, mode: str) -> None:
        if mode == "dark":
            self.theme_switch.select()
        else:
            self.theme_switch.deselect()

    def _on_theme_toggle(self) -> None:
        mode = self.theme_switch.get()
        ctk.set_appearance_mode(mode)
        if hasattr(self.master, "on_theme_changed"):
            self.master.on_theme_changed(mode)  # type: ignore[attr-defined]

    def set_active(self, name: str) -> None:
        """Visually highlight the currently active nav button."""
        for btn_name, btn in self.nav_buttons.items():
            if btn_name == name:
                btn.configure(fg_color=("gray75", "gray25"))
            else:
                btn.configure(fg_color="transparent")


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------

class BaseView(ctk.CTkFrame):
    """Common base class for all page views shown in the content area."""

    def __init__(self, master: ctk.CTk) -> None:
        super().__init__(master, fg_color="transparent")

    def on_show(self) -> None:
        """Hook called each time this view is navigated to. Override as needed."""
        pass


class DownloadProgressPanel(ctk.CTkFrame):
    """
    Self-contained progress UI for one in-flight download: title, a
    determinate progress bar, percent/speed/ETA/size stats, a Cancel
    button, and a final success/error message with an Open Folder action.

    Reusable — HomeView uses it for single downloads now; the Module 9
    queue system will reuse the same widget per queued item.
    """

    def __init__(self, master: ctk.CTk, on_cancel: Callable[[], None]) -> None:
        super().__init__(master, fg_color=("gray92", "gray17"), corner_radius=12)
        self.on_cancel = on_cancel
        self.grid_columnconfigure(0, weight=1)
        self._result_folder: Optional[str] = None

        self.name_label = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w", justify="left",
        )
        self.name_label.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 6))

        self.progress_bar = ctk.CTkProgressBar(self, height=14)
        self.progress_bar.grid(row=1, column=0, sticky="ew", padx=20)
        self.progress_bar.set(0)

        stats_row = ctk.CTkFrame(self, fg_color="transparent")
        stats_row.grid(row=2, column=0, sticky="ew", padx=20, pady=(6, 4))
        stats_row.grid_columnconfigure(4, weight=1)

        self.percent_label = ctk.CTkLabel(stats_row, text="0%", font=ctk.CTkFont(size=12))
        self.percent_label.grid(row=0, column=0, sticky="w", padx=(0, 16))
        self.speed_label = ctk.CTkLabel(stats_row, text="-- KB/s", font=ctk.CTkFont(size=12), text_color="gray60")
        self.speed_label.grid(row=0, column=1, sticky="w", padx=(0, 16))
        self.eta_label = ctk.CTkLabel(stats_row, text="ETA --:--", font=ctk.CTkFont(size=12), text_color="gray60")
        self.eta_label.grid(row=0, column=2, sticky="w", padx=(0, 16))
        self.size_label = ctk.CTkLabel(stats_row, text="", font=ctk.CTkFont(size=12), text_color="gray60")
        self.size_label.grid(row=0, column=3, sticky="w")

        self.cancel_btn = ctk.CTkButton(
            stats_row, text="Cancel", width=80, height=28,
            fg_color="#e5484d", hover_color="#c53030",
            command=self._on_cancel_clicked,
        )
        self.cancel_btn.grid(row=0, column=5, sticky="e")

        self.result_row = ctk.CTkFrame(self, fg_color="transparent")
        self.result_row.grid(row=3, column=0, sticky="ew", padx=20, pady=(4, 16))
        self.result_label = ctk.CTkLabel(
            self.result_row, text="", font=ctk.CTkFont(size=12), anchor="w", justify="left",
        )
        self.result_label.pack(side="left")
        self.open_folder_btn = ctk.CTkButton(
            self.result_row, text="Open Folder", width=110, height=28,
            command=self._open_folder,
        )
        # packed on demand once a download finishes

    def start(self, title: str) -> None:
        self._result_folder = None
        self.name_label.configure(text=title)
        self.progress_bar.set(0)
        self.progress_bar.configure(mode="determinate")
        self.percent_label.configure(text="0%")
        self.speed_label.configure(text="-- KB/s")
        self.eta_label.configure(text="ETA --:--")
        self.size_label.configure(text="")
        self.result_label.configure(text="", text_color="gray60")
        self.open_folder_btn.pack_forget()
        self.cancel_btn.configure(state="normal", text="Cancel")
        self.grid()

    def update_progress(self, p: DownloadProgress) -> None:
        if p.status == "downloading":
            self.progress_bar.set(min(p.percent / 100, 1.0))
            self.percent_label.configure(text=f"{p.percent:.0f}%")
            self.speed_label.configure(text=format_speed(p.speed_bps))
            self.eta_label.configure(text=f"ETA {format_eta(p.eta_seconds)}")
            size_text = format_bytes(p.downloaded_bytes)
            if p.total_bytes:
                size_text += f" / {format_bytes(p.total_bytes)}"
            self.size_label.configure(text=size_text)
        elif p.status == "merging":
            self.progress_bar.configure(mode="indeterminate")
            self.progress_bar.start()
            self.result_label.configure(text="Merging audio + video with FFmpeg...", text_color="gray60")
        elif p.status == "finished":
            self.progress_bar.configure(mode="determinate")
            self.progress_bar.stop()
            self.progress_bar.set(1.0)
            self.percent_label.configure(text="100%")

    def show_success(self, result: DownloadResult) -> None:
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.stop()
        self.progress_bar.set(1.0)
        self.percent_label.configure(text="100%")
        self.cancel_btn.configure(state="disabled", text="Done")
        size_text = format_bytes(result.filesize_bytes) if result.filesize_bytes else "unknown size"
        self.result_label.configure(
            text=f"✓ Downloaded \"{result.title}\" ({result.resolution}, {size_text}) "
                 f"in {result.elapsed_seconds:.0f}s.",
            text_color="#30a46c",
        )
        self._result_folder = str(Path(result.filepath).parent) if result.filepath else None
        if self._result_folder:
            self.open_folder_btn.pack(side="right")

    def show_error(self, exc: Exception) -> None:
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.stop()
        self.cancel_btn.configure(state="disabled", text="Failed")
        icon = "✕ Cancelled:" if isinstance(exc, DownloadCancelledError) else "⚠ Download failed:"
        self.result_label.configure(text=f"{icon} {exc}", text_color="#e5484d")

    def _on_cancel_clicked(self) -> None:
        self.cancel_btn.configure(state="disabled", text="Cancelling...")
        self.on_cancel()

    def _open_folder(self) -> None:
        if self._result_folder:
            open_in_file_explorer(self._result_folder)


class HomeView(BaseView):
    """Landing view: paste a URL, fetch info, choose quality, download."""

    THUMB_SIZE = (240, 135)  # 16:9

    def __init__(
        self,
        master: ctk.CTk,
        settings: Settings,
        history_manager: HistoryManager,
        queue_manager: QueueManager,
    ) -> None:
        super().__init__(master)
        self.settings = settings
        self.history_manager = history_manager
        self.queue_manager = queue_manager
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        self._fetcher = VideoInfoFetcher()
        self._current_info: Optional[VideoInfo] = None
        self._current_playlist: Optional[PlaylistInfo] = None
        self._thumb_ctk_image: Optional[ctk.CTkImage] = None
        self._fetch_seq = 0  # guards against stale background results

        self._engine = DownloadEngine()
        self._downloading = False

        title = ctk.CTkLabel(
            self, text="Paste a YouTube link to get started",
            font=ctk.CTkFont(size=22, weight="bold"),
        )
        title.grid(row=0, column=0, pady=(30, 16), sticky="w", padx=30)

        url_frame = ctk.CTkFrame(self, fg_color=("gray92", "gray17"), corner_radius=12)
        url_frame.grid(row=1, column=0, sticky="ew", padx=30, pady=(0, 12))
        url_frame.grid_columnconfigure(0, weight=1)

        self.url_entry = ctk.CTkEntry(
            url_frame,
            placeholder_text="https://www.youtube.com/watch?v=...",
            height=42,
            font=ctk.CTkFont(size=14),
        )
        self.url_entry.grid(row=0, column=0, padx=(16, 8), pady=16, sticky="ew")
        self.url_entry.bind("<Return>", lambda _e: self._fetch_info())

        self.paste_btn = ctk.CTkButton(
            url_frame, text="Paste", width=80, height=42,
            fg_color="transparent", border_width=1,
            command=self._paste_from_clipboard,
        )
        self.paste_btn.grid(row=0, column=1, padx=(0, 8), pady=16)

        self.fetch_btn = ctk.CTkButton(
            url_frame, text="Fetch Info", width=120, height=42,
            command=self._fetch_info,
        )
        self.fetch_btn.grid(row=0, column=2, padx=(0, 16), pady=16)

        self.status_label = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=13), text_color="gray60"
        )
        self.status_label.grid(row=2, column=0, sticky="w", padx=32)

        # Container that swaps between the empty-state placeholder and the
        # populated results (video info card + resolution table).
        self.info_container = ctk.CTkFrame(self, fg_color="transparent")
        self.info_container.grid(row=3, column=0, sticky="nsew", padx=30, pady=(8, 20))
        self.info_container.grid_columnconfigure(0, weight=1)
        self.info_container.grid_rowconfigure(0, weight=1)

        self._build_placeholder()
        self._build_results_scroll()
        self.empty_state.grid(row=0, column=0, sticky="nsew")

    # -- UI construction -------------------------------------------------

    def _build_placeholder(self) -> None:
        self.empty_state = ctk.CTkFrame(
            self.info_container, fg_color=("gray92", "gray17"), corner_radius=12
        )
        ctk.CTkLabel(
            self.empty_state,
            text=(
                "📺  Video details, thumbnail, and available resolutions\n"
                "will appear here once you fetch a link."
            ),
            font=ctk.CTkFont(size=13),
            text_color="gray60",
            justify="center",
        ).pack(expand=True, pady=60)

    def _build_results_scroll(self) -> None:
        """Scrollable container holding the info card + resolution table,
        since the table can be tall (up to 8 rows)."""
        self.results_scroll = ctk.CTkScrollableFrame(
            self.info_container, fg_color="transparent"
        )
        self.results_scroll.grid_columnconfigure(0, weight=1)
        self._build_info_card()
        self._build_resolution_card()

    def _build_info_card(self) -> None:
        self.info_card = ctk.CTkFrame(
            self.results_scroll, fg_color=("gray92", "gray17"), corner_radius=12
        )
        self.info_card.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        self.info_card.grid_columnconfigure(1, weight=1)

        self.thumb_label = ctk.CTkLabel(self.info_card, text="", width=self.THUMB_SIZE[0])
        self.thumb_label.grid(row=0, column=0, rowspan=5, padx=20, pady=20, sticky="n")

        self.title_label = ctk.CTkLabel(
            self.info_card, text="", font=ctk.CTkFont(size=17, weight="bold"),
            anchor="w", justify="left", wraplength=520,
        )
        self.title_label.grid(row=0, column=1, sticky="ew", padx=(0, 20), pady=(20, 4))

        self.channel_label = ctk.CTkLabel(
            self.info_card, text="", font=ctk.CTkFont(size=13), text_color="gray60", anchor="w"
        )
        self.channel_label.grid(row=1, column=1, sticky="ew", padx=(0, 20))

        self.meta_label = ctk.CTkLabel(
            self.info_card, text="", font=ctk.CTkFont(size=12), text_color="gray60", anchor="w"
        )
        self.meta_label.grid(row=2, column=1, sticky="ew", padx=(0, 20), pady=(4, 8))

        self.description_box = ctk.CTkTextbox(
            self.info_card, height=90, font=ctk.CTkFont(size=12), wrap="word",
            fg_color=("gray86", "gray20"), activate_scrollbars=True,
        )
        self.description_box.grid(row=3, column=1, sticky="ew", padx=(0, 20), pady=(0, 16))
        self.description_box.configure(state="disabled")

        self.playlist_notice = ctk.CTkLabel(
            self.info_card, text="", font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#e5a50a", anchor="w",
        )
        self.playlist_notice.grid(row=4, column=1, sticky="ew", padx=(0, 20), pady=(0, 16))

    def _build_resolution_card(self) -> None:
        self.resolution_card = ctk.CTkFrame(
            self.results_scroll, fg_color=("gray92", "gray17"), corner_radius=12
        )
        self.resolution_card.grid(row=1, column=0, sticky="ew")
        self.resolution_card.grid_columnconfigure(0, weight=1)

        header = ctk.CTkLabel(
            self.resolution_card, text="Available Resolutions",
            font=ctk.CTkFont(size=15, weight="bold"), anchor="w",
        )
        header.grid(row=0, column=0, sticky="w", padx=20, pady=(16, 4))

        self.selection_label = ctk.CTkLabel(
            self.resolution_card, text="Select a resolution below to download.",
            font=ctk.CTkFont(size=12), text_color="gray60", anchor="w",
        )
        self.selection_label.grid(row=1, column=0, sticky="w", padx=20, pady=(0, 12))

        # Table body is (re)built each time new formats are loaded, since
        # CTkTable's row count is fixed at construction.
        self.table_container = ctk.CTkFrame(self.resolution_card, fg_color="transparent")
        self.table_container.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 20))

        self.resolution_table: Optional[CTkTable] = None
        self._format_options: list[FormatOption] = []
        self._selected_format: Optional[FormatOption] = None

        self.no_formats_label = ctk.CTkLabel(
            self.table_container,
            text="No resolution data yet.",
            font=ctk.CTkFont(size=12),
            text_color="gray60",
        )
        # Not shown by default; shown only if a fetch yields zero formats.

        # Download action buttons
        actions_row = ctk.CTkFrame(self.resolution_card, fg_color="transparent")
        actions_row.grid(row=3, column=0, sticky="w", padx=20, pady=(0, 16))

        self.download_selected_btn = ctk.CTkButton(
            actions_row, text="⬇ Download Selected", width=170, height=34,
            state="disabled", command=lambda: self._start_download("selected"),
        )
        self.download_selected_btn.grid(row=0, column=0, padx=(0, 8))

        self.download_best_btn = ctk.CTkButton(
            actions_row, text="Best Quality", width=110, height=34,
            fg_color="transparent", border_width=1,
            command=lambda: self._start_download("best"),
        )
        self.download_best_btn.grid(row=0, column=1, padx=(0, 8))

        self.download_worst_btn = ctk.CTkButton(
            actions_row, text="Lowest Quality", width=120, height=34,
            fg_color="transparent", border_width=1,
            command=lambda: self._start_download("worst"),
        )
        self.download_worst_btn.grid(row=0, column=2, padx=(0, 8))

        self.download_mp3_btn = ctk.CTkButton(
            actions_row, text="Audio Only (MP3)", width=140, height=34,
            fg_color="transparent", border_width=1,
            command=lambda: self._start_download("mp3"),
        )
        self.download_mp3_btn.grid(row=0, column=3, padx=(0, 8))

        self.download_m4a_btn = ctk.CTkButton(
            actions_row, text="Audio Only (M4A)", width=140, height=34,
            fg_color="transparent", border_width=1,
            command=lambda: self._start_download("m4a"),
        )
        self.download_m4a_btn.grid(row=0, column=4)

        self.add_queue_btn = ctk.CTkButton(
            actions_row, text="➕ Add to Queue", width=140, height=34,
            fg_color="transparent", border_width=1,
            command=self._add_to_queue,
        )
        self.add_queue_btn.grid(row=0, column=5, padx=(8, 0))

        self._download_buttons = [
            self.download_selected_btn, self.download_best_btn,
            self.download_worst_btn, self.download_mp3_btn, self.download_m4a_btn,
        ]

        # Progress panel (hidden until a download starts)
        self.progress_panel = DownloadProgressPanel(self.results_scroll, on_cancel=self._cancel_download)
        self.progress_panel.grid(row=2, column=0, sticky="ew", pady=(16, 0))
        self.progress_panel.grid_remove()

    # -- Resolution table ---------------------------------------------

    TABLE_HEADERS = ["Resolution", "FPS", "Video Codec", "Audio", "Est. Size"]

    def _populate_resolution_table(self, options: list[FormatOption]) -> None:
        self._format_options = options
        self._selected_format = None
        self.selection_label.configure(
            text="Select a resolution below to download.", text_color="gray60"
        )
        self.download_selected_btn.configure(state="disabled")

        # Clear any previous table
        if self.resolution_table is not None:
            self.resolution_table.destroy()
            self.resolution_table = None
        self.no_formats_label.pack_forget()

        if not options:
            self.no_formats_label.configure(
                text="⚠ No downloadable video resolutions were found for this video."
            )
            self.no_formats_label.pack(pady=20)
            return

        rows = [self.TABLE_HEADERS]
        for opt in options:
            size_text = format_bytes(opt.estimated_total_bytes) if opt.estimated_total_bytes else "Unknown"
            if opt.estimated_total_bytes and opt.is_estimated_size:
                size_text = f"~{size_text}"
            fps_text = f"{opt.fps}" if opt.fps else "—"
            rows.append([
                opt.resolution_label,
                fps_text,
                opt.vcodec.split(".")[0] if opt.vcodec else "—",
                opt.audio_status,
                size_text,
            ])

        self.resolution_table = CTkTable(
            self.table_container,
            row=len(rows),
            column=len(self.TABLE_HEADERS),
            values=rows,
            header_color=("gray80", "gray25"),
            hover_color=("gray85", "gray30"),
            hover=True,
            corner_radius=6,
            font=ctk.CTkFont(size=12),
            command=self._on_resolution_row_clicked,
        )
        self.resolution_table.pack(fill="x")

    def _on_resolution_row_clicked(self, cell_value: str) -> None:
        """CTkTable's command callback passes the clicked cell's value, so we
        match it against the Resolution column (first column values are
        unique per row)."""
        match = next(
            (o for o in self._format_options if o.resolution_label == cell_value), None
        )
        if match is None:
            return  # click was on a non-resolution column; ignore
        self._selected_format = match
        size_text = format_bytes(match.estimated_total_bytes) if match.estimated_total_bytes else "unknown size"
        audio_note = "" if match.has_audio else " — audio will be merged automatically"
        self.selection_label.configure(
            text=f"✓ Selected {match.resolution_label} ({size_text}){audio_note}.",
            text_color="#30a46c",
        )
        if not self._downloading:
            self.download_selected_btn.configure(state="normal")

    # -- Download flow (Module 6: progress tracking) -------------------

    def _build_request(self, kind: str) -> Optional[tuple[DownloadRequest, str]]:
        """Build a (DownloadRequest, display title) pair for the current
        video and the requested kind ("selected" | "best" | "worst" |
        "mp3" | "m4a"), shared by both the instant-download buttons and
        Add to Queue. Returns None if the request can't be built (e.g.
        "selected" with nothing selected, or no video loaded)."""
        if self._current_info is None:
            return None

        url = self._current_info.webpage_url
        output_dir = self.settings.default_download_folder
        auto_merge = self.settings.auto_merge_audio

        if kind == "selected":
            match = self._selected_format
            if match is None:
                return None
            if match.has_audio:
                request = DownloadRequest(
                    url=url, output_dir=output_dir,
                    mode=DownloadMode.VIDEO_ONLY, format_id=match.format_id,
                )
            else:
                request = DownloadRequest(
                    url=url, output_dir=output_dir,
                    mode=DownloadMode.CUSTOM_RESOLUTION, height=match.height,
                    auto_merge_audio=True,
                )
            title = f"{self._current_info.title} — {match.resolution_label}"
        elif kind == "best":
            request = DownloadRequest(
                url=url, output_dir=output_dir, mode=DownloadMode.BEST, auto_merge_audio=auto_merge,
            )
            title = f"{self._current_info.title} — Best Quality"
        elif kind == "worst":
            request = DownloadRequest(
                url=url, output_dir=output_dir, mode=DownloadMode.WORST, auto_merge_audio=auto_merge,
            )
            title = f"{self._current_info.title} — Lowest Quality"
        elif kind == "mp3":
            request = DownloadRequest(url=url, output_dir=output_dir, mode=DownloadMode.AUDIO_ONLY_MP3)
            title = f"{self._current_info.title} — Audio (MP3)"
        elif kind == "m4a":
            request = DownloadRequest(url=url, output_dir=output_dir, mode=DownloadMode.AUDIO_ONLY_M4A)
            title = f"{self._current_info.title} — Audio (M4A)"
        else:
            return None

        request.ffmpeg_path = self.settings.ffmpeg_path
        return request, title

    def _add_to_queue(self) -> None:
        """Queue the current video for background download (Module 9) so
        the user can keep browsing/fetching other links while it downloads.
        Uses the selected resolution if one is picked, otherwise Best."""
        built = self._build_request("selected") or self._build_request("best")
        if built is None:
            self._set_status("Fetch a video first before adding it to the queue.", "#e5484d")
            return
        request, title = built
        self.queue_manager.add(request, title)
        self._set_status(f"Added \"{title}\" to the download queue.", "#30a46c")

    def _start_download(self, kind: str) -> None:
        if self._current_info is None or self._downloading:
            return

        built = self._build_request(kind)
        if built is None:
            return
        request, title = built

        self._downloading = True
        self._engine.reset()
        for btn in self._download_buttons:
            btn.configure(state="disabled")
        self.progress_panel.start(title)

        thread = threading.Thread(target=self._download_worker, args=(request,), daemon=True)
        thread.start()

    def _download_worker(self, request: DownloadRequest) -> None:
        """Runs on a background thread. Never touches Tk widgets directly."""
        try:
            result = self._engine.download(
                request, on_progress=lambda p: self.after(0, lambda: self._on_download_progress(p))
            )
        except DownloadError as exc:
            # `except ... as exc` implicitly deletes `exc` once this block
            # exits, so it must be captured as a default arg, not just
            # closed over, or the scheduled callback loses the reference.
            self.after(0, lambda exc=exc: self._on_download_error(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error in download worker: %s", exc)
            self.after(0, lambda exc=exc: self._on_download_error(exc))
            return

        self.after(0, lambda: self._on_download_success(result))

    def _on_download_progress(self, p: DownloadProgress) -> None:
        self.progress_panel.update_progress(p)

    def _on_download_success(self, result: DownloadResult) -> None:
        self._downloading = False
        for btn in self._download_buttons:
            btn.configure(state="normal")
        if self._selected_format is None:
            self.download_selected_btn.configure(state="disabled")
        self.progress_panel.show_success(result)
        try:
            self.history_manager.add(result)
        except Exception as exc:  # noqa: BLE001
            # History is a nice-to-have record of the download; a failure
            # persisting it should never make a successful download look
            # like it failed to the user.
            logger.error("Failed to record download in history: %s", exc)
        if self.settings.open_folder_after_download and result.filepath:
            open_in_file_explorer(str(Path(result.filepath).parent))

    def _on_download_error(self, exc: Exception) -> None:
        self._downloading = False
        for btn in self._download_buttons:
            btn.configure(state="normal")
        if self._selected_format is None:
            self.download_selected_btn.configure(state="disabled")
        self.progress_panel.show_error(exc)

    def _cancel_download(self) -> None:
        self._engine.cancel()

    def _paste_from_clipboard(self) -> None:
        if not _CLIPBOARD_AVAILABLE:
            self._set_status("Clipboard support not available (pyperclip missing).", "gray60")
            return
        try:
            text = pyperclip.paste()
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, text)
            self._set_status("", "gray60")
        except Exception as exc:  # noqa: BLE001
            logger.error("Clipboard paste failed: %s", exc)
            self._set_status("Could not read clipboard.", "#e5484d")

    # -- Fetch flow ----------------------------------------------------

    def _set_status(self, text: str, color: str) -> None:
        self.status_label.configure(text=text, text_color=color)

    def _fetch_info(self) -> None:
        url = self.url_entry.get().strip()
        if not url:
            self._set_status("⚠ Please paste a YouTube URL first.", "#e5a50a")
            return
        if not is_valid_youtube_url(url):
            self._set_status("⚠ That doesn't look like a valid YouTube URL.", "#e5484d")
            return

        self._fetch_seq += 1
        seq = self._fetch_seq

        self.fetch_btn.configure(state="disabled", text="Fetching...")
        self._set_status("⏳ Fetching video information...", "gray60")
        self.playlist_notice.configure(text="")

        thread = threading.Thread(target=self._fetch_worker, args=(url, seq), daemon=True)
        thread.start()

    def _fetch_worker(self, url: str, seq: int) -> None:
        """Runs on a background thread. Never touches Tk widgets directly."""
        playlist_info: Optional[PlaylistInfo] = None
        try:
            if is_playlist_url(url):
                playlist_info = self._fetcher.fetch_playlist(url, max_entries=50)
            video_info = self._fetcher.fetch_video(url)
            thumb_bytes = self._download_thumbnail(video_info.thumbnail_url)
        except VideoInfoError as exc:
            # `except ... as exc` implicitly deletes `exc` once this block
            # exits, so it must be captured as a default arg, not just
            # closed over, or the scheduled callback loses the reference.
            self.after(0, lambda exc=exc: self._on_fetch_error(exc, seq))
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error in fetch worker: %s", exc)
            self.after(0, lambda exc=exc: self._on_fetch_error(exc, seq))
            return

        self.after(0, lambda: self._on_fetch_success(video_info, playlist_info, thumb_bytes, seq))

    @staticmethod
    def _download_thumbnail(url: Optional[str]) -> Optional[bytes]:
        if not url:
            return None
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to download thumbnail: %s", exc)
            return None

    def _on_fetch_error(self, exc: Exception, seq: int) -> None:
        if seq != self._fetch_seq:
            return  # a newer fetch superseded this one
        self.fetch_btn.configure(state="normal", text="Fetch Info")
        self._set_status(f"⚠ {exc}", "#e5484d")

    def _on_fetch_success(
        self,
        info: VideoInfo,
        playlist_info: Optional[PlaylistInfo],
        thumb_bytes: Optional[bytes],
        seq: int,
    ) -> None:
        if seq != self._fetch_seq:
            return  # a newer fetch superseded this one

        self._current_info = info
        self._current_playlist = playlist_info
        self.fetch_btn.configure(state="normal", text="Fetch Info")
        self._set_status("✓ Video information loaded.", "#30a46c")

        # Thumbnail
        if thumb_bytes:
            try:
                image = Image.open(io.BytesIO(thumb_bytes))
                image = image.resize(self.THUMB_SIZE)
                self._thumb_ctk_image = ctk.CTkImage(
                    light_image=image, dark_image=image, size=self.THUMB_SIZE
                )
                self.thumb_label.configure(image=self._thumb_ctk_image, text="")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to render thumbnail: %s", exc)
                self.thumb_label.configure(image=None, text="🎬")
        else:
            self.thumb_label.configure(image=None, text="🎬")

        # Text fields
        self.title_label.configure(text=info.title)
        self.channel_label.configure(text=f"📺 {info.channel}")
        meta_parts = [
            f"⏱ {format_duration(info.duration_seconds)}",
            format_views(info.view_count),
            format_upload_date(info.upload_date),
        ]
        self.meta_label.configure(text="   •   ".join(meta_parts))

        self.description_box.configure(state="normal")
        self.description_box.delete("1.0", "end")
        self.description_box.insert("1.0", info.description_preview())
        self.description_box.configure(state="disabled")

        if playlist_info:
            self.playlist_notice.configure(
                text=(
                    f"📃 This is part of a playlist: \"{playlist_info.title}\" "
                    f"({playlist_info.video_count} videos). Full playlist selection "
                    f"arrives in Module 10 — showing info for this single video for now."
                )
            )
        else:
            self.playlist_notice.configure(text="")

        # Resolution table
        options = get_resolution_options(info.raw_formats)
        self._populate_resolution_table(options)

        # Swap placeholder -> populated results
        self.results_scroll.grid(row=0, column=0, sticky="nsew")
        self.empty_state.grid_forget()


class QueueRow(ctk.CTkFrame):
    """One row in the Downloads (queue) view: title + status badge, a
    progress bar that's live while downloading, and a context-sensitive
    action button (Cancel while waiting/downloading, Remove once the
    item is finished)."""

    STATUS_COLORS = {
        QueueStatus.WAITING: "gray60",
        QueueStatus.DOWNLOADING: "#3b82f6",
        QueueStatus.COMPLETED: "#30a46c",
        QueueStatus.FAILED: "#e5484d",
        QueueStatus.CANCELLED: "gray50",
    }

    def __init__(
        self,
        master: ctk.CTk,
        item_id: int,
        on_cancel: Callable[[int], None],
        on_remove: Callable[[int], None],
    ) -> None:
        super().__init__(master, fg_color=("gray92", "gray17"), corner_radius=10)
        self.item_id = item_id
        self.on_cancel = on_cancel
        self.on_remove = on_remove
        self._result_folder: Optional[str] = None
        self.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))
        top.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            top, text="", font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w", justify="left",
        )
        self.title_label.grid(row=0, column=0, sticky="ew")

        self.status_label = ctk.CTkLabel(
            top, text="", font=ctk.CTkFont(size=11, weight="bold"), anchor="e",
        )
        self.status_label.grid(row=0, column=1, sticky="e", padx=(8, 0))

        self.progress_bar = ctk.CTkProgressBar(self, height=8)
        self.progress_bar.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 4))
        self.progress_bar.set(0)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 12))
        bottom.grid_columnconfigure(0, weight=1)

        self.detail_label = ctk.CTkLabel(
            bottom, text="", font=ctk.CTkFont(size=11), text_color="gray60",
            anchor="w", justify="left",
        )
        self.detail_label.grid(row=0, column=0, sticky="ew")

        self.folder_btn = ctk.CTkButton(
            bottom, text="Open Folder", width=100, height=26,
            fg_color="transparent", border_width=1,
            command=self._open_folder,
        )
        # gridded on demand once a download completes successfully

        self.action_btn = ctk.CTkButton(
            bottom, text="Cancel", width=80, height=26,
            command=self._on_action_clicked,
        )
        self.action_btn.grid(row=0, column=2, sticky="e")

    def update_item(self, item: QueueItem) -> None:
        self.title_label.configure(text=item.title)
        color = self.STATUS_COLORS.get(item.status, "gray60")
        self.status_label.configure(text=item.status.value, text_color=color)

        if item.status == QueueStatus.WAITING:
            self.progress_bar.configure(mode="determinate")
            self.progress_bar.stop()
            self.progress_bar.set(0)
            self.detail_label.configure(text="Waiting in queue…", text_color="gray60")
            self._set_action("Cancel", enabled=True, danger=True)
            self.folder_btn.grid_remove()

        elif item.status == QueueStatus.DOWNLOADING:
            p = item.progress
            if p is not None and p.status == "downloading":
                self.progress_bar.configure(mode="determinate")
                self.progress_bar.stop()
                self.progress_bar.set(min(p.percent / 100, 1.0))
                size_text = format_bytes(p.downloaded_bytes)
                if p.total_bytes:
                    size_text += f" / {format_bytes(p.total_bytes)}"
                self.detail_label.configure(
                    text=f"{p.percent:.0f}% • {format_speed(p.speed_bps)} • "
                         f"ETA {format_eta(p.eta_seconds)} • {size_text}",
                    text_color="gray60",
                )
            elif p is not None and p.status == "merging":
                self.progress_bar.configure(mode="indeterminate")
                self.progress_bar.start()
                self.detail_label.configure(text="Merging audio + video with FFmpeg…", text_color="gray60")
            else:
                self.progress_bar.configure(mode="indeterminate")
                self.progress_bar.start()
                self.detail_label.configure(text="Starting…", text_color="gray60")
            self._set_action("Cancel", enabled=True, danger=True)
            self.folder_btn.grid_remove()

        elif item.status == QueueStatus.COMPLETED:
            self.progress_bar.configure(mode="determinate")
            self.progress_bar.stop()
            self.progress_bar.set(1.0)
            result = item.result
            size_text = format_bytes(result.filesize_bytes) if result and result.filesize_bytes else "unknown size"
            self.detail_label.configure(
                text=f"✓ Completed ({result.resolution if result else ''}, {size_text})",
                text_color="#30a46c",
            )
            self._set_action("Remove", enabled=True, danger=False)
            self._result_folder = str(Path(result.filepath).parent) if result and result.filepath else None
            if self._result_folder:
                self.folder_btn.grid(row=0, column=1, sticky="e", padx=(0, 8))
            else:
                self.folder_btn.grid_remove()

        elif item.status == QueueStatus.FAILED:
            self.progress_bar.configure(mode="determinate")
            self.progress_bar.stop()
            self.detail_label.configure(text=f"⚠ {item.error or 'Download failed.'}", text_color="#e5484d")
            self._set_action("Remove", enabled=True, danger=False)
            self.folder_btn.grid_remove()

        elif item.status == QueueStatus.CANCELLED:
            self.progress_bar.configure(mode="determinate")
            self.progress_bar.stop()
            self.detail_label.configure(text="✕ Cancelled", text_color="gray50")
            self._set_action("Remove", enabled=True, danger=False)
            self.folder_btn.grid_remove()

    def _set_action(self, text: str, enabled: bool, danger: bool) -> None:
        self.action_btn.configure(
            text=text,
            state="normal" if enabled else "disabled",
            fg_color="#e5484d" if danger else "transparent",
            hover_color="#c53030" if danger else ("gray85", "gray30"),
            border_width=0 if danger else 1,
        )

    def _on_action_clicked(self) -> None:
        if self.action_btn.cget("text") == "Cancel":
            self.action_btn.configure(state="disabled", text="Cancelling...")
            self.on_cancel(self.item_id)
        else:
            self.on_remove(self.item_id)

    def _open_folder(self) -> None:
        if self._result_folder:
            open_in_file_explorer(self._result_folder)


class DownloadsView(BaseView):
    """Live multi-item download queue view (Module 9)."""

    def __init__(self, master: ctk.CTk, queue_manager: QueueManager) -> None:
        super().__init__(master)
        self.queue_manager = queue_manager
        self._rows: dict[int, QueueRow] = {}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header_row = ctk.CTkFrame(self, fg_color="transparent")
        header_row.grid(row=0, column=0, sticky="ew", padx=30, pady=(30, 4))
        header_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            header_row, text="Downloads", font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        self.clear_btn = ctk.CTkButton(
            header_row, text="Clear Finished", width=130, height=30,
            fg_color="transparent", border_width=1,
            command=self._on_clear_finished,
        )
        self.clear_btn.grid(row=0, column=2, sticky="e")

        self.summary_label = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=12), text_color="gray60", anchor="w",
        )
        self.summary_label.grid(row=1, column=0, sticky="w", padx=32, pady=(0, 8))

        self.list_container = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list_container.grid(row=2, column=0, sticky="nsew", padx=30, pady=(0, 30))
        self.list_container.grid_columnconfigure(0, weight=1)

        self.empty_state = ctk.CTkFrame(self.list_container, fg_color=("gray92", "gray17"), corner_radius=12)
        ctk.CTkLabel(
            self.empty_state,
            text=(
                "⬇  No downloads queued yet.\n\n"
                "Fetch a video on the Home tab and click \"➕ Add to Queue\" to\n"
                "download it in the background — you can queue several videos at\n"
                "once and they'll run automatically, up to your concurrency limit\n"
                "from Settings."
            ),
            font=ctk.CTkFont(size=13),
            text_color="gray60",
            justify="center",
        ).pack(expand=True, pady=60)

        self._refresh_all()

    def on_show(self) -> None:
        self._refresh_all()

    # -- Sync with QueueManager -------------------------------------------

    def _refresh_all(self) -> None:
        """Full resync: (re)build rows for every current item in order.
        Used on first show / navigation, since items may have been added
        or removed while this view wasn't visible."""
        items = self.queue_manager.items()
        current_ids = {item.id for item in items}

        for stale_id in list(self._rows.keys()):
            if stale_id not in current_ids:
                self._rows.pop(stale_id).destroy()

        for row in self._rows.values():
            row.grid_forget()

        for idx, item in enumerate(items):
            row = self._rows.get(item.id)
            if row is None:
                row = QueueRow(self.list_container, item.id, self._on_row_cancel, self._on_row_remove)
                self._rows[item.id] = row
            row.update_item(item)
            row.grid(row=idx, column=0, sticky="ew", pady=(0, 10))

        self._update_visibility(bool(items))
        self._update_summary()

    def update_item(self, item: QueueItem) -> None:
        """Incremental update for one item, called (via App) every time
        the QueueManager reports a change -- including frequent progress
        ticks -- so this must stay cheap and avoid rebuilding the list."""
        row = self._rows.get(item.id)
        if row is None:
            # New item added while this view was visible.
            row = QueueRow(self.list_container, item.id, self._on_row_cancel, self._on_row_remove)
            self._rows[item.id] = row
            row.grid(row=len(self._rows) - 1, column=0, sticky="ew", pady=(0, 10))
            self._update_visibility(True)
        row.update_item(item)
        self._update_summary()

    def _update_visibility(self, has_items: bool) -> None:
        if has_items:
            self.empty_state.grid_forget()
        else:
            self.empty_state.grid(row=0, column=0, sticky="nsew")

    def _update_summary(self) -> None:
        counts = self.queue_manager.counts()
        active = counts.get(QueueStatus.DOWNLOADING.value, 0)
        waiting = counts.get(QueueStatus.WAITING.value, 0)
        done = counts.get(QueueStatus.COMPLETED.value, 0)
        failed = counts.get(QueueStatus.FAILED.value, 0)
        cancelled = counts.get(QueueStatus.CANCELLED.value, 0)
        total = active + waiting + done + failed + cancelled
        if total == 0:
            self.summary_label.configure(text="")
            return
        parts = []
        if active:
            parts.append(f"{active} downloading")
        if waiting:
            parts.append(f"{waiting} waiting")
        if done:
            parts.append(f"{done} completed")
        if failed:
            parts.append(f"{failed} failed")
        if cancelled:
            parts.append(f"{cancelled} cancelled")
        self.summary_label.configure(text=" • ".join(parts))

    # -- Row actions -------------------------------------------------------

    def _on_row_cancel(self, item_id: int) -> None:
        self.queue_manager.cancel(item_id)

    def _on_row_remove(self, item_id: int) -> None:
        self.queue_manager.remove(item_id)
        row = self._rows.pop(item_id, None)
        if row is not None:
            row.destroy()
        self._relayout()
        self._update_visibility(bool(self._rows))
        self._update_summary()

    def _on_clear_finished(self) -> None:
        removed_ids = self.queue_manager.clear_finished()
        for item_id in removed_ids:
            row = self._rows.pop(item_id, None)
            if row is not None:
                row.destroy()
        self._relayout()
        self._update_visibility(bool(self._rows))
        self._update_summary()

    def _relayout(self) -> None:
        """Re-grid remaining rows so removing one from the middle doesn't
        leave a gap, preserving current insertion order."""
        for idx, row in enumerate(self._rows.values()):
            row.grid(row=idx, column=0, sticky="ew", pady=(0, 10))


class HistoryRow(ctk.CTkFrame):
    """One row in the History list: title/meta on the left, quick actions
    (Open File, Open Folder, Delete) on the right.

    Delete uses a lightweight "click again to confirm" pattern (no modal
    dialog) that reverts itself after a few seconds if not confirmed.
    """

    def __init__(
        self,
        master: ctk.CTk,
        entry: HistoryEntry,
        on_delete: Callable[["HistoryRow"], None],
    ) -> None:
        super().__init__(master, fg_color=("gray92", "gray17"), corner_radius=10)
        self.entry = entry
        self.on_delete = on_delete
        self.grid_columnconfigure(0, weight=1)
        self._confirming_delete = False
        self._confirm_after_id: Optional[str] = None

        text_col = ctk.CTkFrame(self, fg_color="transparent")
        text_col.grid(row=0, column=0, sticky="ew", padx=(16, 8), pady=12)
        text_col.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            text_col, text=entry.title, font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w", justify="left",
        ).grid(row=0, column=0, sticky="ew")

        missing_note = "  \u2022  \u26a0 file no longer found" if not entry.file_exists else ""
        meta_text = f"{entry.resolution} \u2022 {entry.size_label} \u2022 {entry.date_label}{missing_note}"
        meta_color = "#e5a50a" if not entry.file_exists else "gray60"
        ctk.CTkLabel(
            text_col, text=meta_text, font=ctk.CTkFont(size=11),
            text_color=meta_color, anchor="w", justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(2, 0))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=0, column=1, sticky="e", padx=(0, 12), pady=12)

        self.open_file_btn = ctk.CTkButton(
            btn_row, text="Open File", width=90, height=30,
            fg_color="transparent", border_width=1,
            state="normal" if entry.file_exists else "disabled",
            command=self._open_file,
        )
        self.open_file_btn.pack(side="left", padx=(0, 6))

        self.open_folder_btn = ctk.CTkButton(
            btn_row, text="Open Folder", width=100, height=30,
            fg_color="transparent", border_width=1,
            state="normal" if entry.folder and Path(entry.folder).exists() else "disabled",
            command=self._open_folder,
        )
        self.open_folder_btn.pack(side="left", padx=(0, 6))

        self.delete_btn = ctk.CTkButton(
            btn_row, text="Delete", width=80, height=30,
            fg_color="#e5484d", hover_color="#c53030",
            command=self._on_delete_clicked,
        )
        self.delete_btn.pack(side="left")

    def _open_file(self) -> None:
        if self.entry.filepath:
            open_in_file_explorer(self.entry.filepath)

    def _open_folder(self) -> None:
        if self.entry.folder:
            open_in_file_explorer(self.entry.folder)

    def _on_delete_clicked(self) -> None:
        if not self._confirming_delete:
            self._confirming_delete = True
            self.delete_btn.configure(text="Confirm?")
            self._confirm_after_id = self.after(3000, self._revert_confirm)
            return
        if self._confirm_after_id is not None:
            self.after_cancel(self._confirm_after_id)
        self.on_delete(self)

    def _revert_confirm(self) -> None:
        self._confirming_delete = False
        self._confirm_after_id = None
        self.delete_btn.configure(text="Delete")


class HistoryView(BaseView):
    """Download history view: lists every completed download with quick
    Open File / Open Folder / Delete actions, newest first."""

    def __init__(self, master: ctk.CTk, history_manager: HistoryManager) -> None:
        super().__init__(master)
        self.history_manager = history_manager
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=30, pady=(30, 16))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header, text="History", font=ctk.CTkFont(size=22, weight="bold")
        ).grid(row=0, column=0, sticky="w")

        self.clear_btn = ctk.CTkButton(
            header, text="Clear All", width=100, height=30,
            fg_color="transparent", border_width=1,
            command=self._on_clear_clicked,
        )
        self.clear_btn.grid(row=0, column=1, sticky="e")

        # Swaps between the empty-state placeholder and the scrollable list.
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", padx=30, pady=(0, 30))
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_rowconfigure(0, weight=1)

        self.empty_state = ctk.CTkFrame(self.body, fg_color=("gray92", "gray17"), corner_radius=12)
        ctk.CTkLabel(
            self.empty_state,
            text=(
                "\U0001f551  Your download history is empty.\n\n"
                "Completed downloads show up here with title, resolution,\n"
                "file size, and date, plus quick actions to open the file\n"
                "or folder."
            ),
            font=ctk.CTkFont(size=13),
            text_color="gray60",
            justify="center",
        ).pack(expand=True, pady=60)

        self.list_frame = ctk.CTkScrollableFrame(self.body, fg_color="transparent")
        self.list_frame.grid_columnconfigure(0, weight=1)

        self._rows: list[HistoryRow] = []
        self.refresh()

    def on_show(self) -> None:
        # History can change from the Home tab between visits, so always
        # reload from the HistoryManager (which itself owns the in-memory
        # cache — this is a cheap list copy, not a disk read).
        self.refresh()

    def refresh(self) -> None:
        for row in self._rows:
            row.destroy()
        self._rows = []

        entries = self.history_manager.all()
        self.clear_btn.configure(state="normal" if entries else "disabled")

        if not entries:
            self.list_frame.grid_forget()
            self.empty_state.grid(row=0, column=0, sticky="nsew")
            return

        self.empty_state.grid_forget()
        self.list_frame.grid(row=0, column=0, sticky="nsew")

        for i, entry in enumerate(entries):
            row = HistoryRow(self.list_frame, entry, on_delete=self._on_delete_row)
            row.grid(row=i, column=0, sticky="ew", pady=(0, 8))
            self._rows.append(row)

    def _on_delete_row(self, row: HistoryRow) -> None:
        self.history_manager.delete(row.entry.id)
        self.refresh()

    def _on_clear_clicked(self) -> None:
        if not self._rows:
            return
        if self.clear_btn.cget("text") != "Confirm?":
            self.clear_btn.configure(text="Confirm?")
            self.after(3000, lambda: self.clear_btn.configure(text="Clear All"))
            return
        self.history_manager.clear()
        self.clear_btn.configure(text="Clear All")
        self.refresh()


class SettingsView(BaseView):
    """Settings view: theme, folders, concurrency, and behavior toggles."""

    def __init__(self, master: ctk.CTk, settings: Settings, on_settings_changed: Callable[[], None]) -> None:
        super().__init__(master)
        self.settings = settings
        self.on_settings_changed = on_settings_changed
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkLabel(
            self, text="Settings", font=ctk.CTkFont(size=22, weight="bold")
        )
        header.grid(row=0, column=0, sticky="w", padx=30, pady=(30, 16))

        card = ctk.CTkFrame(self, fg_color=("gray92", "gray17"), corner_radius=12)
        card.grid(row=1, column=0, sticky="ew", padx=30, pady=(0, 30))
        card.grid_columnconfigure(1, weight=1)

        # Default download folder
        ctk.CTkLabel(card, text="Default download folder:", font=ctk.CTkFont(size=13)).grid(
            row=0, column=0, padx=(20, 8), pady=(20, 8), sticky="w"
        )
        self.folder_var = ctk.StringVar(value=self.settings.default_download_folder)
        folder_entry = ctk.CTkEntry(card, textvariable=self.folder_var, height=34)
        folder_entry.grid(row=0, column=1, padx=(0, 8), pady=(20, 8), sticky="ew")
        browse_btn = ctk.CTkButton(card, text="Browse", width=90, height=34, command=self._browse_folder)
        browse_btn.grid(row=0, column=2, padx=(0, 20), pady=(20, 8))

        # Default resolution
        ctk.CTkLabel(card, text="Default resolution:", font=ctk.CTkFont(size=13)).grid(
            row=1, column=0, padx=(20, 8), pady=8, sticky="w"
        )
        self.resolution_var = ctk.StringVar(value=self.settings.default_resolution)
        resolution_menu = ctk.CTkOptionMenu(
            card,
            variable=self.resolution_var,
            values=["best", "1080p", "720p", "480p", "360p", "worst"],
        )
        resolution_menu.grid(row=1, column=1, padx=(0, 8), pady=8, sticky="w")

        # Max concurrent downloads
        ctk.CTkLabel(card, text="Max concurrent downloads:", font=ctk.CTkFont(size=13)).grid(
            row=2, column=0, padx=(20, 8), pady=8, sticky="w"
        )
        self.concurrency_var = ctk.StringVar(value=str(self.settings.max_concurrent_downloads))
        concurrency_menu = ctk.CTkOptionMenu(
            card, variable=self.concurrency_var, values=["1", "2", "3", "4", "5"]
        )
        concurrency_menu.grid(row=2, column=1, padx=(0, 8), pady=8, sticky="w")

        # Toggles
        self.auto_merge_var = ctk.BooleanVar(value=self.settings.auto_merge_audio)
        ctk.CTkCheckBox(
            card, text="Auto-merge audio + video with FFmpeg", variable=self.auto_merge_var
        ).grid(row=3, column=0, columnspan=2, padx=20, pady=8, sticky="w")

        self.open_after_var = ctk.BooleanVar(value=self.settings.open_folder_after_download)
        ctk.CTkCheckBox(
            card, text="Open folder after download completes", variable=self.open_after_var
        ).grid(row=4, column=0, columnspan=2, padx=20, pady=8, sticky="w")

        self.auto_update_var = ctk.BooleanVar(value=self.settings.auto_update_ytdlp)
        ctk.CTkCheckBox(
            card, text="Auto-update yt-dlp on startup", variable=self.auto_update_var
        ).grid(row=5, column=0, columnspan=2, padx=20, pady=8, sticky="w")

        self.clipboard_var = ctk.BooleanVar(value=self.settings.check_clipboard)
        ctk.CTkCheckBox(
            card, text="Detect YouTube links from clipboard", variable=self.clipboard_var
        ).grid(row=6, column=0, columnspan=2, padx=20, pady=(8, 20), sticky="w")

        # -- Divider ---------------------------------------------------
        divider = ctk.CTkFrame(card, height=1, fg_color=("gray80", "gray30"))
        divider.grid(row=7, column=0, columnspan=3, sticky="ew", padx=20, pady=(0, 8))

        # FFmpeg path override
        ctk.CTkLabel(card, text="FFmpeg path override (optional):", font=ctk.CTkFont(size=13)).grid(
            row=8, column=0, padx=(20, 8), pady=8, sticky="w"
        )
        self.ffmpeg_path_var = ctk.StringVar(value=self.settings.ffmpeg_path)
        ffmpeg_entry = ctk.CTkEntry(
            card, textvariable=self.ffmpeg_path_var, height=34,
            placeholder_text="Leave blank to auto-detect from system PATH",
        )
        ffmpeg_entry.grid(row=8, column=1, padx=(0, 8), pady=8, sticky="ew")
        ffmpeg_browse_btn = ctk.CTkButton(
            card, text="Browse", width=90, height=34, command=self._browse_ffmpeg
        )
        ffmpeg_browse_btn.grid(row=8, column=2, padx=(0, 20), pady=8)

        self.ffmpeg_status_label = ctk.CTkLabel(
            card, text="", font=ctk.CTkFont(size=11), wraplength=650, justify="left", anchor="w",
        )
        self.ffmpeg_status_label.grid(row=9, column=1, padx=(0, 8), pady=(0, 8), sticky="w")
        self._refresh_ffmpeg_status()

        # yt-dlp version / update
        ctk.CTkLabel(card, text="yt-dlp version:", font=ctk.CTkFont(size=13)).grid(
            row=10, column=0, padx=(20, 8), pady=8, sticky="w"
        )
        self.ytdlp_version_label = ctk.CTkLabel(
            card, text=get_ytdlp_version(), font=ctk.CTkFont(size=13)
        )
        self.ytdlp_version_label.grid(row=10, column=1, padx=(0, 8), pady=8, sticky="w")
        self.check_update_btn = ctk.CTkButton(
            card, text="Check for Updates", width=140, height=34, command=self._check_for_updates
        )
        self.check_update_btn.grid(row=10, column=2, padx=(0, 20), pady=8)

        self.update_status_label = ctk.CTkLabel(
            card, text="", font=ctk.CTkFont(size=11), text_color="gray60",
            wraplength=650, justify="left", anchor="w",
        )
        self.update_status_label.grid(
            row=11, column=1, columnspan=2, padx=(0, 20), pady=(0, 20), sticky="w"
        )

        save_btn = ctk.CTkButton(self, text="Save Settings", width=160, height=38, command=self._save)
        save_btn.grid(row=2, column=0, sticky="w", padx=30, pady=(0, 10))

        self.saved_label = ctk.CTkLabel(self, text="", text_color="#30a46c", font=ctk.CTkFont(size=12))
        self.saved_label.grid(row=3, column=0, sticky="w", padx=30)

    def _browse_folder(self) -> None:
        folder = ctk.filedialog.askdirectory(initialdir=self.folder_var.get() or ".")
        if folder:
            self.folder_var.set(folder)

    def _browse_ffmpeg(self) -> None:
        """Let the user pick the ffmpeg executable directly (or a folder
        containing it, if their platform's dialog is used that way)."""
        filetypes = [("Executable", "*.exe"), ("All files", "*.*")] if os.name == "nt" else [("All files", "*.*")]
        path = ctk.filedialog.askopenfilename(
            title="Select ffmpeg executable", filetypes=filetypes
        )
        if path:
            self.ffmpeg_path_var.set(path)
            self._refresh_ffmpeg_status()

    def _refresh_ffmpeg_status(self) -> None:
        path = self.ffmpeg_path_var.get().strip()
        found = check_ffmpeg_installed(path)
        if found:
            source = "at the configured path" if path else "on system PATH"
            self.ffmpeg_status_label.configure(
                text=f"✓ FFmpeg detected {source}.", text_color="#30a46c"
            )
        else:
            self.ffmpeg_status_label.configure(
                text="✕ FFmpeg not found. Merging and audio extraction will fail until this is resolved.",
                text_color="#e5484d",
            )

    def _check_for_updates(self) -> None:
        self.check_update_btn.configure(state="disabled", text="Checking...")
        self.update_status_label.configure(text="Checking for updates...", text_color="gray60")
        thread = threading.Thread(target=self._update_worker, daemon=True)
        thread.start()

    def _update_worker(self) -> None:
        try:
            success, message = update_ytdlp()
        except Exception as exc:  # noqa: BLE001
            success, message = False, str(exc)
        self.after(0, lambda s=success, m=message: self.report_update_result(s, m))

    def report_update_result(self, success: bool, message: str, startup: bool = False) -> None:
        """Reflect an update-check result in the UI. Called both from the
        manual 'Check for Updates' button and from App's silent startup
        auto-update (when enabled in Settings)."""
        prefix = "Startup check: " if startup else ""
        self.update_status_label.configure(
            text=f"{prefix}{message}",
            text_color="#30a46c" if success else "#e5484d",
        )
        self.ytdlp_version_label.configure(text=get_ytdlp_version())
        self.check_update_btn.configure(state="normal", text="Check for Updates")

    def _save(self) -> None:
        self.settings.update(
            default_download_folder=self.folder_var.get(),
            default_resolution=self.resolution_var.get(),
            max_concurrent_downloads=int(self.concurrency_var.get()),
            auto_merge_audio=self.auto_merge_var.get(),
            open_folder_after_download=self.open_after_var.get(),
            auto_update_ytdlp=self.auto_update_var.get(),
            check_clipboard=self.clipboard_var.get(),
            ffmpeg_path=self.ffmpeg_path_var.get().strip(),
        )
        self._refresh_ffmpeg_status()
        self.saved_label.configure(text="✓ Settings saved.")
        self.on_settings_changed()
        self.after(2000, lambda: self.saved_label.configure(text=""))


class AboutView(BaseView):
    """Static about/info view."""

    def __init__(self, master: ctk.CTk) -> None:
        super().__init__(master)
        self.grid_columnconfigure(0, weight=1)

        card = ctk.CTkFrame(self, fg_color=("gray92", "gray17"), corner_radius=12)
        card.grid(row=0, column=0, sticky="nsew", padx=30, pady=30)
        self.grid_rowconfigure(0, weight=1)

        ctk.CTkLabel(
            card, text=APP_NAME, font=ctk.CTkFont(size=24, weight="bold")
        ).pack(pady=(40, 4))
        ctk.CTkLabel(
            card, text=f"Version {APP_VERSION}", text_color="gray60"
        ).pack(pady=(0, 20))
        ctk.CTkLabel(
            card,
            text=(
                "A modern desktop app for downloading YouTube videos with\n"
                "full resolution control, playlist support, queued downloads,\n"
                "and history tracking.\n\n"
                "Built with Python, CustomTkinter, and yt-dlp."
            ),
            justify="center",
            font=ctk.CTkFont(size=13),
        ).pack(pady=(0, 20))
        ctk.CTkLabel(
            card,
            text="For personal use. Please respect YouTube's Terms of Service\nand applicable copyright law when downloading content.",
            justify="center",
            font=ctk.CTkFont(size=11),
            text_color="gray50",
        ).pack(pady=(0, 40))


# --------------------------------------------------------------------------
# Main Application Window
# --------------------------------------------------------------------------

class App(ctk.CTk):
    """Root application window."""

    def __init__(self) -> None:
        super().__init__()

        self.settings = Settings.load()
        self.history_manager = HistoryManager()
        self.queue_manager = QueueManager(
            max_concurrent=self.settings.max_concurrent_downloads,
            on_update=self._on_queue_item_update,
        )
        ctk.set_appearance_mode(self.settings.theme)
        ctk.set_default_color_theme(self.settings.color_theme)

        self.title(APP_NAME)
        self.geometry(self.settings.window_geometry)
        self.minsize(900, 600)
        self._set_window_icon()

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Sidebar
        self.sidebar = Sidebar(self, on_navigate=self.show_view)
        self.sidebar.grid(row=0, column=0, sticky="nsw")
        self.sidebar.set_theme_switch(self.settings.theme)

        # Content container
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        # Instantiate views
        self.views: dict[str, BaseView] = {
            "Home": HomeView(self.content, self.settings, self.history_manager, self.queue_manager),
            "Downloads": DownloadsView(self.content, self.queue_manager),
            "History": HistoryView(self.content, self.history_manager),
            "Settings": SettingsView(self.content, self.settings, self.on_settings_changed),
            "About": AboutView(self.content),
        }
        for view in self.views.values():
            view.grid(row=0, column=0, sticky="nsew")

        self.current_view: Optional[str] = None
        self.show_view("Home")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if self.settings.auto_update_ytdlp:
            threading.Thread(target=self._auto_update_ytdlp, daemon=True).start()

    def _on_queue_item_update(self, item: QueueItem) -> None:
        """Called from QueueManager on whatever thread produced the change
        (main thread on add/cancel, a worker thread mid-download) -- always
        marshal onto the GUI thread before touching widgets or Settings."""
        self.after(0, lambda item=item: self._handle_queue_item_update(item))

    def _handle_queue_item_update(self, item: QueueItem) -> None:
        downloads_view = self.views.get("Downloads")
        if isinstance(downloads_view, DownloadsView):
            downloads_view.update_item(item)

        if item.status == QueueStatus.COMPLETED and item.result is not None:
            try:
                self.history_manager.add(item.result)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to record queued download in history: %s", exc)
            if self.settings.open_folder_after_download and item.result.filepath:
                open_in_file_explorer(str(Path(item.result.filepath).parent))

    def _set_window_icon(self) -> None:
        """Set the window/taskbar icon. Windows uses the .ico via
        iconbitmap; macOS/Linux Tk builds don't support .ico there, so
        they fall back to the PNG via iconphoto. Never fatal -- a missing
        or unreadable icon file just leaves Tk's default icon in place."""
        ico_path = ICONS_DIR / "app.ico"
        png_path = ICONS_DIR / "app.png"
        try:
            if sys.platform.startswith("win") and ico_path.exists():
                self.iconbitmap(str(ico_path))
            elif png_path.exists():
                from PIL import ImageTk
                icon_image = Image.open(png_path)
                self._icon_photo_ref = ImageTk.PhotoImage(icon_image)  # keep a ref alive
                self.iconphoto(True, self._icon_photo_ref)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not set window icon (non-fatal): %s", exc)

    def _auto_update_ytdlp(self) -> None:
        """Runs on a background thread at startup when 'Auto-update yt-dlp
        on startup' is enabled in Settings. Never blocks the GUI."""
        try:
            success, message = update_ytdlp()
        except Exception as exc:  # noqa: BLE001
            success, message = False, str(exc)
        self.after(
            0,
            lambda s=success, m=message: self.views["Settings"].report_update_result(s, m, startup=True),
        )

    def show_view(self, name: str) -> None:
        if name not in self.views:
            logger.warning("Attempted to navigate to unknown view: %s", name)
            return
        self.views[name].tkraise()
        self.views[name].on_show()
        self.sidebar.set_active(name)
        self.current_view = name

    def on_theme_changed(self, mode: str) -> None:
        self.settings.update(theme=mode)

    def on_settings_changed(self) -> None:
        """Called when the Settings view saves changes."""
        logger.info("Settings updated: %s", self.settings)
        self.queue_manager.set_max_concurrent(self.settings.max_concurrent_downloads)

    def _on_close(self) -> None:
        try:
            self.settings.update(window_geometry=self.geometry())
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to persist window geometry: %s", exc)
        self.queue_manager.shutdown()
        self.destroy()


def run_app() -> None:
    """Create and run the main application."""
    app = App()
    app.mainloop()
