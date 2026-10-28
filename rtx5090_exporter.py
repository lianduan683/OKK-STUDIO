from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import os
import math
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from exporter_core import (
    CREATE_NO_WINDOW,
    Capabilities,
    ExportCancelled,
    ExportSettings,
    MediaInfo,
    RollingProgressRate,
    build_encode_command,
    build_mux_command,
    detect_capabilities,
    find_binary,
    parse_fps_value,
    parse_progress_line,
    parse_speed_multiplier,
    progress_fps_value,
    probe_media,
    progress_seconds,
    quote_command,
    read_gpu_sample,
    split_ranges,
)
from hardware_encoders import (CODEC_LABELS, available_codecs, codec_from_label,
                               codec_label, encoder_vendor, preferred_codec)
from editor_core import (
    AlignmentResult,
    JoinExportPlan,
    align_audio,
    align_audio_segments,
    aligned_audio_intervals,
    aligned_clip_timeline,
    build_join_timeline_model,
    build_aligned_concat_copy_command,
    build_audio_encode_args,
    build_concat_encode_command,
    build_gap_preserving_concat_encode_command,
    build_join_mux_command,
    build_join_video_chunk_command,
    build_concat_copy_command,
    build_extract_audio_command,
    build_extract_video_command,
    build_replace_audio_command,
    build_trim_copy_command,
    concat_compatibility_issues,
    fit_aspect_size,
    format_signed_time,
    format_time,
    format_timeline_ruler_time,
    gap_preserving_timeline,
    join_uses_cuda_frames,
    make_join_export_plan,
    make_1080p_proxy_settings,
    normalize_audio_bitrate,
    normalize_audio_sample_rate,
    normalize_video_bitrate,
    parse_signed_timecode,
    parse_timecode,
    partition_join_video_spans,
    probe_media_duration,
    duplicate_proxy_stems,
    proxy_output_path,
    proxy_source_conflicts,
    resolve_join_output_range,
    timeline_frame_zoom_multiplier,
    write_concat_manifest,
)
from mpv_controller import MpvController
from multicam_ui import MulticamWorkspace
from cache_paths import cache_dir, initialize_cache_policy
from cache_publish import publish_completed
from proxy_cache import register_proxy
from studio_integration import StudioIntegrationMixin
from lossless_workspace import LosslessWorkspace
from editor_core import build_proxy_encode_command, proxy_format_labels, resolve_proxy_format


APP_NAME = "RTX 视频渲染工坊"
APP_VERSION = "5.12.4"
BG = "#18191b"
SURFACE = "#202124"
PANEL = "#242629"
PANEL_ALT = "#2c2e32"
INPUT = "#191a1c"
BORDER = "#3b3e43"
TEXT = "#e1e3e6"
MUTED = "#969ba3"
PURPLE = "#4c87ac"
PURPLE_BRIGHT = "#69a8ce"
PINK = "#58a6d6"
GREEN = "#76b99a"
BLUE = "#86adca"
RED = "#ed746d"
GLASS_HIGHLIGHT = "#484b51"
GLASS_EDGE = "#42454a"
GLASS_SHADOW = "#18191b"
UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Cascadia Mono"
TIMELINE_LEFT = 64.0
TIMELINE_HEIGHT = 186
TIMELINE_RULER_BOTTOM = 46
TIMELINE_VIDEO_TOP = 56
TIMELINE_VIDEO_BOTTOM = 132
TIMELINE_AUDIO_TOP = 148
TIMELINE_AUDIO_BOTTOM = 176
TIMELINE_FRAME_PIXELS = 12.0
TIMELINE_MAJOR_TICK_PIXELS = 230.0


def enable_dpi_awareness() -> None:
    """Keep Tk text and controls crisp on high-DPI Windows displays."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def app_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def asset_path(name: str) -> Path:
    """Return bundled asset paths for source and PyInstaller builds."""
    base = Path(getattr(sys, "_MEIPASS", app_directory()))
    return base / "assets" / name


class ExporterApp(StudioIntegrationMixin, tk.Tk):
    def __init__(self) -> None:
        selected_cache_root = initialize_cache_policy()
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1600x1000")
        self.minsize(1280, 820)
        self.configure(bg=BG)
        self.option_add("*Font", (UI_FONT, 10))
        try:
            pixels_per_inch = float(self.winfo_fpixels("1i"))
            self.tk.call("tk", "scaling", max(1.0, pixels_per_inch / 72.0))
        except (tk.TclError, ValueError):
            pass
        self.ui_scale = max(1.0, float(self.winfo_fpixels("1i")) / 96.0)
        self.geometry(f"{min(round(1600 * self.ui_scale), self.winfo_screenwidth())}x"
                      f"{min(round(1000 * self.ui_scale), self.winfo_screenheight())}")
        self.minsize(min(round(1280 * self.ui_scale), self.winfo_screenwidth()),
                     min(round(820 * self.ui_scale), self.winfo_screenheight()))
        self.protocol("WM_DELETE_WINDOW", self._close)
        try:
            self.iconbitmap(str(asset_path("app_icon.ico")))
        except (tk.TclError, OSError):
            pass

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.processes: list[subprocess.Popen] = []
        self.process_lock = threading.Lock()
        self.media: MediaInfo | None = None
        self.caps = Capabilities()
        self.working = False
        self.chunk_progress_state: dict[int, tuple[float, float, float]] = {}
        self.editor_join_chunk_state: dict[int, tuple[float, float, float, bool]] = {}
        self.editor_join_parallel_source_fps = 0.0
        self.last_gpu_poll = 0.0
        self.thumbnail_images: dict[str, tk.PhotoImage] = {}
        self.metric_thumbnail_images: dict[str, tk.PhotoImage] = {}
        self.logo_header_image: tk.PhotoImage | None = None
        self.mpv_path = find_binary("mpv", app_directory())
        self.mpv: MpvController | None = None
        self.editor_media: MediaInfo | None = None
        self.editor_duration = 0.0
        self.editor_current_time = 0.0
        self.editor_dragging = False
        self.editor_poll_busy = False
        self.editor_load_generation = 0
        self.editor_join_files: list[str] = []
        self.editor_join_alignments: dict[str, AlignmentResult] = {}
        self.editor_join_alignment_audio = ""
        self.editor_join_media_cache: dict[str, MediaInfo] = {}
        self.editor_join_audio_duration = 0.0
        self.editor_join_timeline_zoom = 1.0
        self.editor_join_timeline_fit_pps = 1.0
        self.editor_join_timeline_max_zoom = 1.0
        self.editor_join_timeline_canvas_width = 1.0
        self.editor_join_timeline_pixels_per_second = 1.0
        self.editor_join_timeline_origin = 0.0
        self.editor_join_timeline_zoom_updating = False
        self.editor_join_timeline_model = None
        self.editor_join_timeline_items: dict[int, int] = {}
        self.editor_join_timeline_drag: dict[str, float | int] | None = None
        self.editor_join_timeline_pan: dict[str, float | bool] | None = None
        self.editor_join_timeline_playhead_master = 0.0
        self.editor_join_preview_context: tuple[str, float] | None = None
        self.editor_join_timeline_probe_key: tuple[tuple[str, ...], str] | None = None
        self.editor_proxy_files: list[str] = []
        self.editor_proxy_progress_state: dict[str, float] = {}

        self._create_variables()
        self._configure_style()
        self.lossless_workspace = LosslessWorkspace(self)
        from studio_project import app_directory as project_app_directory
        self.workspace_layout_path = project_app_directory() / 'workspace-layout.json'
        self._build_ui()
        self._init_studio_integration()
        self._log(f'缓存目录：{selected_cache_root}；默认临时文件、代理与模型存放于此。')
        if os.name == "nt":
            self.after(0, lambda: self.state("zoomed"))
        self.after(80, self._drain_events)
        self.after(200, self._detect_async)
        self.after(500, self._poll_editor_player)

    def _create_variables(self) -> None:
        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.codec_var = tk.StringVar(value="HEVC / H.265 (NVENC)")
        self.preset_var = tk.StringVar(value="极速 P2")
        self.quality_var = tk.IntVar(value=20)
        self.resolution_var = tk.StringVar(value="保持原分辨率")
        self.depth_var = tk.StringVar(value="自动")
        self.audio_var = tk.StringVar(value="AAC 320 kbps")
        self.decode_var = tk.StringVar(value="NVIDIA CUDA（推荐）")
        self.split_var = tk.StringVar(value="驱动自动选择（检测中）")
        self.parallel_var = tk.IntVar(value=1)
        self.maxrate_var = tk.StringVar()
        self.gpu_status_var = tk.StringVar(value="正在检测显卡与可用编码器…")
        self.ffmpeg_status_var = tk.StringVar(value="正在检测 FFmpeg…")
        self.media_var = tk.StringVar(value="选择输入文件后显示媒体信息")
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text_var = tk.StringVar(value="就绪")
        self.monitor_var = tk.StringVar(value="GPU --%   编码 --%   解码 --%   显存 ---- MB   温度 --°C")
        self.gpu_metric_var = tk.StringVar(value="--%")
        self.encode_metric_var = tk.StringVar(value="--%")
        self.decode_metric_var = tk.StringVar(value="--%")
        self.memory_metric_var = tk.StringVar(value="---- MB")
        self.gpu_detail_var = tk.StringVar(value="等待显卡检测")
        self.encode_detail_var = tk.StringVar(value="硬件编码引擎")
        self.decode_detail_var = tk.StringVar(value="硬件解码")
        self.memory_detail_var = tk.StringVar(value="显存占用")
        self.gpu_meter_var = tk.DoubleVar(value=0)
        self.encode_meter_var = tk.DoubleVar(value=0)
        self.decode_meter_var = tk.DoubleVar(value=0)
        self.memory_meter_var = tk.DoubleVar(value=0)
        self.editor_video_var = tk.StringVar()
        self.editor_audio_var = tk.StringVar()
        self.editor_output_var = tk.StringVar()
        self.editor_time_var = tk.StringVar(value="00:00:00.000 / 00:00:00.000")
        self.editor_status_var = tk.StringVar(value="选择视频后可播放、逐帧与截帧")
        self.editor_preview_scale_var = tk.StringVar(value="适应窗口")
        self.editor_offset_var = tk.StringVar(value="0.000")
        self.editor_timeline_var = tk.DoubleVar(value=0)
        self.editor_seek_var = tk.StringVar(value="00:00:00.000")
        self.editor_trim_in_var = tk.StringVar(value="00:00:00.000")
        self.editor_trim_out_var = tk.StringVar(value="00:00:00.000")
        self.editor_trim_mode_var = tk.StringVar(value="逐帧精准（沿用编码设置）")
        self.editor_trim_bitrate_var = tk.StringVar()
        self.editor_trim_output_var = tk.StringVar()
        self.editor_join_output_var = tk.StringVar()
        self.editor_join_mode_var = tk.StringVar(value="重编码（沿用统一设置）")
        self.editor_join_video_output_var = tk.StringVar(value="MOV / HEVC H.265（自动适配）")
        self.editor_join_gap_var = tk.StringVar(value="保留停机空窗（黑场，主音频连续）")
        self.editor_join_bitrate_var = tk.StringVar(value="80M")
        self.editor_join_leading_mode_var = tk.StringVar(
            value="裁掉开头画面（从主音频 00:00 开始）"
        )
        self.editor_join_trailing_mode_var = tk.StringVar(
            value="裁掉结尾画面（到主音频结束）"
        )
        self.editor_join_range_start_var = tk.StringVar()
        self.editor_join_range_end_var = tk.StringVar()
        self.editor_join_audio_bitrate_var = tk.StringVar(value="320k")
        self.editor_join_audio_format_var = tk.StringVar(value="AAC（兼容 MP4）")
        self.editor_join_audio_sample_rate_var = tk.StringVar(value="48000")
        self.editor_join_alignment_var = tk.StringVar(value="未对齐：选择连续主音频后可自动分析全部片段")
        self.editor_join_manual_time_var = tk.StringVar(value="00:00:00.000")
        self.editor_join_nudge_var = tk.StringVar(value="0.100")
        self.editor_join_timeline_summary_var = tk.StringVar(value="添加片段后显示工程时间线")
        self.editor_join_timeline_hint_var = tk.StringVar(
            value="拖动顶部时间标尺横移 · Ctrl+滚轮缩放 · Shift+滚轮横移"
        )
        self.editor_join_timeline_zoom_slider_var = tk.DoubleVar(value=0.0)
        self.editor_join_timeline_zoom_text_var = tk.StringVar(value="适配全长")
        self.editor_join_shared_settings_var = tk.StringVar(value="沿用“导出与 GPU”页设置")
        self.editor_split_mode_var = tk.StringVar(value="FLAC 无损（推荐）")
        self.editor_split_dir_var = tk.StringVar()
        self.editor_proxy_format_var = tk.StringVar(value=proxy_format_labels()[0])
        self.editor_proxy_dir_var = tk.StringVar()
        self.editor_proxy_parallel_var = tk.StringVar(value="同时 1 路（检测中）")
        self.editor_proxy_count_var = tk.StringVar(value="队列 0 个")

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        for name, color in (("TFrame", BG), ("Surface.TFrame", SURFACE),
                            ("Panel.TFrame", PANEL), ("PanelAlt.TFrame", PANEL_ALT)):
            style.configure(name, background=color)
        for name, color in (("TLabel", BG), ("Surface.TLabel", SURFACE),
                            ("Panel.TLabel", PANEL), ("Muted.TLabel", PANEL)):
            style.configure(name, background=color, foreground=MUTED if name == "Muted.TLabel" else TEXT)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=(UI_FONT, 14, "bold"))
        style.configure("Section.TLabel", background=PANEL, foreground=TEXT, font=(UI_FONT, 11, "bold"))
        for name in ("TButton", "Compact.TButton"):
            style.configure(name, background=PANEL_ALT, foreground=TEXT, font=(UI_FONT, 9),
                            borderwidth=1, bordercolor=BORDER, relief="flat", padding=(10, 5))
            style.map(name, background=[("active", "#3b3e43"), ("disabled", PANEL)],
                      foreground=[("disabled", "#626873")])
        style.configure("Accent.TButton", background=PURPLE, foreground="#ffffff",
                        font=(UI_FONT, 10, "bold"), padding=(18, 7), borderwidth=0)
        style.map("Accent.TButton", background=[("active", PURPLE_BRIGHT), ("disabled", "#354652")])
        style.configure("Danger.TButton", background=PANEL_ALT, foreground="#edaaa4",
                        font=(UI_FONT, 9), padding=(10, 5), borderwidth=0)
        style.map("Danger.TButton", background=[("active", "#503231")], foreground=[("disabled", "#626873")])
        style.configure("TCombobox", fieldbackground=INPUT, background=PANEL_ALT, foreground=TEXT,
                        arrowcolor=MUTED, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        font=(UI_FONT, 10), padding=(6, 4))
        style.map("TCombobox", fieldbackground=[("readonly", INPUT)], foreground=[("readonly", TEXT)])
        style.configure("Horizontal.TProgressbar", troughcolor=INPUT, background=PURPLE, thickness=5)
        style.configure("Metric.Horizontal.TProgressbar", troughcolor=INPUT, background=PURPLE, thickness=3)
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=0)
        style.configure("TNotebook.Tab", background=SURFACE, foreground=MUTED,
                        font=(UI_FONT, 9), padding=(13, 6), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", PANEL_ALT)], foreground=[("selected", TEXT)])
        style.layout("Workspace.TNotebook.Tab", [])
        style.configure("Workspace.TNotebook", background=BG, borderwidth=0)
        style.configure("Vertical.TScrollbar", background="#393d43", troughcolor=INPUT, borderwidth=0, arrowcolor=MUTED)
        style.configure("Horizontal.TScrollbar", background="#393d43", troughcolor=INPUT, borderwidth=0, arrowcolor=MUTED)
        style.configure("TPanedwindow", background=BG, sashwidth=5)

    def _load_ui_thumbnails(self) -> None:
        logo_path = asset_path("okk_group_logo_white.png")
        if logo_path.is_file():
            try:
                logo = tk.PhotoImage(file=str(logo_path))
                self.logo_header_image = logo.subsample(2, 2)
            except tk.TclError:
                pass
        names = (
            "thumb_01_piano.png", "thumb_02_cat.png", "thumb_03_blonde.png",
            "thumb_04_closeup.png", "thumb_05_feathers.png", "thumb_06_wine.png",
            "thumb_07_ice.png", "thumb_08_hanfu.png", "thumb_09_silver_portrait.png",
            "thumb_10_chin.png", "thumb_11_purple.png", "thumb_12_silver_outfit.png",
            "thumb_13_red.png", "thumb_14_flowers.png",
        )
        for name in names:
            path = asset_path(name)
            if not path.is_file():
                continue
            try:
                full = tk.PhotoImage(file=str(path))
                self.thumbnail_images[name] = full
                self.metric_thumbnail_images[name] = full.subsample(2, 2)
            except tk.TclError:
                continue

    def _photo_badge(self, parent: tk.Widget, name: str, *, compact: bool = False, bg: str = PANEL_ALT) -> tk.Label:
        images = self.metric_thumbnail_images if compact else self.thumbnail_images
        return tk.Label(parent, image=images.get(name), bg=bg, bd=0, highlightthickness=0)

    def _add_glass_shine(self, frame: tk.Frame) -> None:
        # Flat panel edges keep the editing surface visually quiet.
        return

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=0)
        outer.pack(fill="both", expand=True)
        self._build_header(outer)
        self._build_footer(outer)
        main = ttk.Frame(outer)
        main.pack(fill="both", expand=True)
        notebook = ttk.Notebook(main, style="Workspace.TNotebook")
        notebook.pack(fill="both", expand=True)
        self.page_notebook = notebook
        basic = ttk.Frame(notebook, style="Panel.TFrame", padding=10)
        editor = ttk.Frame(notebook, style="Panel.TFrame", padding=8)
        multicam = ttk.Frame(notebook, style="Panel.TFrame", padding=0)
        log_tab = ttk.Frame(notebook, style="Panel.TFrame", padding=10)
        self.page_frames = {"basic": basic, "editor": editor, "multicam": multicam, "log": log_tab}
        for name, frame in self.page_frames.items():
            notebook.add(frame, text=name)
        self._build_export_tab(basic)
        self._build_editor(editor)
        self.multicam_workspace = MulticamWorkspace(multicam, self)
        self.multicam_workspace.pack(fill="both", expand=True)
        self._build_log(log_tab)
        notebook.bind("<<NotebookTabChanged>>", self._on_page_changed)
        self._select_page("multicam")
        if "--multicam-project" in sys.argv:
            project_index = sys.argv.index("--multicam-project") + 1
            if project_index < len(sys.argv):
                self.after_idle(lambda: self.multicam_workspace.load_project(sys.argv[project_index]))

    def _select_page(self, name: str) -> None:
        self.page_notebook.select(self.page_frames[name])
        self._on_page_changed()

    def _on_page_changed(self, _event=None) -> None:
        selected = self.page_notebook.select()
        self.active_page = next((name for name, frame in self.page_frames.items() if str(frame) == selected), "multicam")
        labels = {"basic": "转码与 GPU", "editor": "剪辑工具", "multicam": "多机位剪辑", "log": "任务日志"}
        self.page_title_var.set(labels[self.active_page])
        for name, button in self.page_buttons.items():
            button.configure(bg="#30363d" if name == self.active_page else SURFACE,
                             fg=TEXT if name == self.active_page else MUTED,
                             highlightbackground=PURPLE if name == self.active_page else SURFACE)
        if self.active_page != "multicam":
            self.multicam_workspace.pause()
        text = "交付 / 导出" if self.active_page == "multicam" else "开始转码" if self.active_page == "basic" else "导出设置"
        self.export_button.configure(text=text)

    def _context_export(self) -> None:
        if self.working:
            return
        if self.active_page == "multicam":
            self.multicam_workspace.show_delivery()
        elif self.active_page == "basic":
            self._start_export()
        else:
            self._select_page("basic")

    def _show_hardware_info(self) -> None:
        messagebox.showinfo("硬件与任务状态", self.gpu_status_var.get() + "\n\n" +
                            self.ffmpeg_status_var.get() + "\n\n" + self.monitor_var.get(), parent=self)

    def _build_export_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=4, minsize=440)
        parent.columnconfigure(1, weight=12, minsize=760)
        parent.rowconfigure(0, weight=1)

        media = tk.Frame(parent, bg=PANEL_ALT, highlightthickness=0)
        media.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        settings = ttk.Frame(parent, style="Panel.TFrame", padding=(14, 8))
        settings.grid(row=0, column=1, sticky="nsew")
        self._build_media_panel(media)
        self._build_unified_export_settings(settings)

    def _build_unified_export_settings(self, parent: ttk.Frame) -> None:
        """One compact page for output quality and RTX acceleration."""
        header = tk.Frame(parent, bg=PANEL)
        header.pack(fill="x", pady=(2, 12))
        tk.Label(header, text="导出与 GPU", bg=PANEL, fg=TEXT, font=(UI_FONT, 16, "bold")).pack(side="left")
        self.advanced_title_label = tk.Label(
            header, text="显卡自动适配加速", bg=PANEL, fg=GREEN, font=(UI_FONT, 10, "bold"),
        )
        self.advanced_title_label.pack(side="right", pady=(5, 0))
        self.basic_gpu_subtitle = tk.Label(
            parent, text="正在识别显卡并检测实际可用编码器…", bg=PANEL, fg=MUTED,
            font=(UI_FONT, 10), anchor="w",
        )
        self.basic_gpu_subtitle.pack(fill="x", pady=(0, 10))

        grid = tk.Frame(parent, bg=PANEL)
        grid.pack(fill="both", expand=True)
        for column in range(4):
            grid.columnconfigure(column, weight=1, uniform="unified_export")

        def combo_field(label: str, variable: tk.Variable, values: list[str], row: int, column: int) -> ttk.Combobox:
            card = tk.Frame(grid, bg=PANEL_ALT, highlightthickness=1, highlightbackground=BORDER, padx=12, pady=10)
            card.grid(row=row, column=column, sticky="nsew", padx=(0 if column == 0 else 6, 0 if column == 3 else 6), pady=6)
            tk.Label(card, text=label, bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 10, "bold")).pack(anchor="w")
            combo = ttk.Combobox(card, textvariable=variable, values=values, state="readonly")
            combo.pack(fill="x", pady=(8, 0))
            return combo

        self.codec_combo = combo_field(
            "编码格式", self.codec_var,
            list(CODEC_LABELS.values()), 0, 0,
        )
        combo_field(
            "速度 / 画质", self.preset_var,
            ["最快 P1", "极速 P2", "均衡 P4", "高质量 P6", "最高质量 P7"], 0, 1,
        )
        combo_field(
            "输出分辨率", self.resolution_var,
            ["保持原分辨率", "1080p", "1440p", "4K", "8K"], 0, 2,
        )
        combo_field("输出位深", self.depth_var, ["自动", "8-bit", "10-bit"], 0, 3)
        combo_field(
            "音频", self.audio_var,
            ["AAC 320 kbps", "AAC 192 kbps", "复制原音频", "无音频"], 1, 0,
        )
        self.decode_combo = combo_field(
            "解码路径", self.decode_var,
            ["NVIDIA CUDA（推荐）", "CPU 解码（兼容模式）"], 1, 1,
        )
        self.split_combo = combo_field(
            "多 NVENC 分帧", self.split_var,
            ["驱动自动选择（检测中）", "关闭"], 1, 2,
        )

        rate_card = tk.Frame(grid, bg=PANEL_ALT, highlightthickness=1, highlightbackground=BORDER, padx=12, pady=10)
        rate_card.grid(row=1, column=3, sticky="nsew", padx=(6, 0), pady=6)
        tk.Label(rate_card, text="最大码率（可留空）", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 10, "bold")).pack(anchor="w")
        tk.Entry(
            rate_card, textvariable=self.maxrate_var, bg=INPUT, fg=TEXT, insertbackground=TEXT,
            relief="flat", highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=PINK, font=(MONO_FONT, 10),
        ).pack(fill="x", pady=(8, 0), ipady=8)

        quality_card = tk.Frame(grid, bg=PANEL_ALT, highlightthickness=1, highlightbackground=BORDER, padx=12, pady=10)
        quality_card.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=(0, 6), pady=6)
        tk.Label(quality_card, text="恒定质量 CQ（越小画质越高）", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 10, "bold")).pack(anchor="w")
        quality_line = tk.Frame(quality_card, bg=PANEL_ALT)
        quality_line.pack(fill="x", pady=(5, 0))
        tk.Scale(
            quality_line, from_=14, to=32, orient="horizontal", variable=self.quality_var,
            bg=PANEL_ALT, fg=TEXT, troughcolor="#3c424a", highlightthickness=0,
            activebackground=PINK, sliderlength=22, width=15,
        ).pack(side="left", fill="x", expand=True)
        tk.Label(quality_line, textvariable=self.quality_var, bg=PANEL_ALT, fg=TEXT, width=3, font=(UI_FONT, 12, "bold")).pack(side="left")

        parallel_card = tk.Frame(grid, bg=PANEL_ALT, highlightthickness=1, highlightbackground=BORDER, padx=12, pady=10)
        parallel_card.grid(row=2, column=2, columnspan=2, sticky="nsew", padx=(6, 0), pady=6)
        tk.Label(parallel_card, text="并行分段任务数", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 10, "bold")).pack(anchor="w")
        parallel_line = tk.Frame(parallel_card, bg=PANEL_ALT)
        parallel_line.pack(fill="x", pady=(5, 0))
        tk.Scale(
            parallel_line, from_=1, to=4, resolution=1, orient="horizontal",
            variable=self.parallel_var, bg=PANEL_ALT, fg=TEXT, troughcolor="#3c424a",
            highlightthickness=0, activebackground=PINK, sliderlength=22, width=15,
        ).pack(side="left", fill="x", expand=True)
        tk.Label(parallel_line, textvariable=self.parallel_var, bg=PANEL_ALT, fg=TEXT, width=3, font=(UI_FONT, 12, "bold")).pack(side="left")

        recommendation = tk.Frame(parent, bg="#292d32", highlightthickness=1, highlightbackground="#414852", padx=14, pady=11)
        recommendation.pack(fill="x", pady=(10, 0))
        self.basic_recommendation_label = tk.Label(
            recommendation, text="推荐参数将在 GPU 检测完成后自动显示。", bg="#292d32",
            fg="#ccd4dc", font=(UI_FONT, 10), anchor="w", justify="left",
        )
        self.basic_recommendation_label.pack(fill="x")
        self.advanced_note_label = tk.Label(
            recommendation, text="正在检测显卡与可用编码器…",
            bg="#292d32", fg=MUTED, font=(UI_FONT, 9), anchor="w", justify="left",
            wraplength=1000,
        )
        self.advanced_note_label.pack(fill="x", pady=(5, 0))

    def _build_header(self, parent: ttk.Frame) -> None:
        top = tk.Frame(parent, bg=SURFACE, padx=12, pady=5,
                       highlightthickness=1, highlightbackground="#303237")
        top.pack(fill="x")
        tk.Label(top, text="OKK  STUDIO", bg=SURFACE, fg=TEXT,
                 font=(UI_FONT, 10, "bold")).pack(side="left", padx=(0, 18))
        self.page_title_var = tk.StringVar(value="多机位剪辑")
        tk.Label(top, textvariable=self.page_title_var, bg=SURFACE, fg=MUTED,
                 font=(UI_FONT, 9)).pack(side="left")
        self._build_studio_menu(top)
        self.export_button = ttk.Button(top, text="交付 / 导出", style="Accent.TButton", command=self._context_export)
        self.export_button.pack(side="right")
        self.ffmpeg_chip = tk.Label(top, text="FFmpeg…", bg=SURFACE, fg=MUTED, font=(UI_FONT, 8))
        self.ffmpeg_chip.pack(side="right", padx=12)
        hardware = tk.Button(top, textvariable=self.gpu_detail_var, command=self._show_hardware_info,
                             bg=SURFACE, fg=MUTED, activebackground=PANEL_ALT,
                             activeforeground=TEXT, relief="flat", bd=0, font=(UI_FONT, 8))
        hardware.pack(side="right", padx=6)

    def _chip(self, parent: tk.Widget, text: str, color: str) -> tk.Label:
        return tk.Label(
            parent, text=text, bg=SURFACE, fg=color, font=(UI_FONT, 11, "bold"),
            padx=18, pady=11, highlightthickness=1, highlightbackground=BORDER,
        )

    def _build_metrics(self, parent: ttk.Frame) -> None:
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(18, 0))
        cards = [
            ("GPU", self.gpu_metric_var, self.gpu_detail_var, self.gpu_meter_var, "thumb_01_piano.png"),
            ("视频编码", self.encode_metric_var, self.encode_detail_var, self.encode_meter_var, "thumb_04_closeup.png"),
            ("视频解码", self.decode_metric_var, self.decode_detail_var, self.decode_meter_var, "thumb_06_wine.png"),
            ("显存", self.memory_metric_var, self.memory_detail_var, self.memory_meter_var, "thumb_14_flowers.png"),
        ]
        for index, values in enumerate(cards):
            card = self._metric_card(row, *values)
            card.pack(side="left", fill="x", expand=True, padx=(0 if index == 0 else 7, 0 if index == 3 else 7))

    def _metric_card(
        self, parent: tk.Widget, title: str, value: tk.StringVar,
        detail: tk.StringVar, meter: tk.DoubleVar, thumb: str,
    ) -> tk.Frame:
        card = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER, padx=18, pady=14)
        self._add_glass_shine(card)
        photo = self._photo_badge(card, thumb, compact=True, bg=PANEL)
        photo.pack(side="right", padx=(16, 0))
        content = tk.Frame(card, bg=PANEL)
        content.pack(side="left", fill="x", expand=True)
        tk.Label(content, text=f"●  {title}", bg=PANEL, fg=PINK, font=(UI_FONT, 11, "bold")).pack(anchor="w")
        ttk.Progressbar(content, variable=meter, maximum=100, style="Metric.Horizontal.TProgressbar").pack(fill="x", pady=(11, 8))
        bottom = tk.Frame(content, bg=PANEL)
        bottom.pack(fill="x")
        tk.Label(bottom, textvariable=value, bg=PANEL, fg=TEXT, font=(UI_FONT, 19, "bold")).pack(side="left")
        tk.Label(bottom, textvariable=detail, bg=PANEL, fg=MUTED, font=(UI_FONT, 10)).pack(side="right", pady=(9, 0))
        return card

    def _build_media_panel(self, parent: tk.Frame) -> None:
        heading = tk.Frame(parent, bg=PANEL_ALT)
        heading.pack(fill="x", padx=20, pady=(18, 12))
        tk.Label(heading, text="素材文件", bg=PANEL_ALT, fg=TEXT, font=(UI_FONT, 15, "bold")).pack(side="left")
        tk.Label(heading, text="●  OKK GROUP", bg=PANEL_ALT, fg=PINK, font=(UI_FONT, 10, "bold")).pack(side="right")

        files = tk.Frame(parent, bg=PANEL_ALT)
        files.pack(fill="x", padx=20, pady=(8, 22))
        self._file_row(files, "输入视频", self.input_var, self._choose_input, 0)
        self._file_row(files, "输出文件", self.output_var, self._choose_output, 1)
        tk.Label(
            files, textvariable=self.media_var, bg=PANEL_ALT, fg=MUTED,
            font=(UI_FONT, 10), justify="left", wraplength=430,
        ).grid(row=2, column=0, sticky="w", pady=(10, 0))
        files.columnconfigure(0, weight=1)

    def _build_editor(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0)
        parent.rowconfigure(0, weight=4)
        parent.rowconfigure(1, weight=3, minsize=285)

        workspace = ttk.Panedwindow(parent, orient="horizontal")
        workspace.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.editor_workspace_pane = workspace
        self.editor_pane_initialized = False
        workspace.bind("<Configure>", self._set_initial_editor_pane_size)

        player_panel = tk.Frame(workspace, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        workspace.add(player_panel, weight=2)
        player_panel.columnconfigure(0, weight=1)
        player_panel.rowconfigure(1, weight=1)
        self._add_glass_shine(player_panel)

        player_head = tk.Frame(player_panel, bg=PANEL, padx=18, pady=13)
        player_head.grid(row=0, column=0, sticky="ew")
        tk.Label(player_head, text="视频监看与逐帧", bg=PANEL, fg=TEXT, font=(UI_FONT, 14, "bold")).pack(side="left")
        mpv_text = "●  MPV 已找到" if self.mpv_path else "●  缺少 mpv.exe"
        mpv_color = GREEN if self.mpv_path else RED
        self.mpv_status_label = tk.Label(player_head, text=mpv_text, bg=PANEL, fg=mpv_color, font=(UI_FONT, 10, "bold"))
        self.mpv_status_label.pack(side="right")
        preview_size = tk.Frame(player_head, bg=PANEL)
        preview_size.pack(side="right", padx=(0, 12))
        tk.Label(
            preview_size, text="监看大小", bg=PANEL, fg=MUTED, font=(UI_FONT, 8),
        ).pack(side="left", padx=(0, 5))
        ttk.Button(
            preview_size, text="－", style="Compact.TButton",
            command=lambda: self._adjust_editor_preview_scale(-1),
        ).pack(side="left")
        preview_scale_combo = ttk.Combobox(
            preview_size, textvariable=self.editor_preview_scale_var, state="readonly",
            values=["适应窗口", "90%", "75%", "60%", "50%"], width=8,
        )
        preview_scale_combo.pack(side="left", padx=4)
        preview_scale_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._resize_editor_preview_surface(),
        )
        ttk.Button(
            preview_size, text="＋", style="Compact.TButton",
            command=lambda: self._adjust_editor_preview_scale(1),
        ).pack(side="left")

        self.player_stage = tk.Frame(player_panel, bg="#111214", cursor="arrow")
        self.player_stage.grid(row=1, column=0, sticky="nsew", padx=14)
        self.player_surface = tk.Frame(
            self.player_stage, bg="#000000", cursor="arrow",
            highlightthickness=1, highlightbackground="#373a40",
        )
        self.player_surface.place(relx=0.5, rely=0.5, anchor="center", width=640, height=360)
        self.player_stage.bind("<Configure>", self._resize_editor_preview_surface)

        timeline = tk.Frame(player_panel, bg=PANEL, padx=14, pady=10)
        timeline.grid(row=2, column=0, sticky="ew")
        timeline.columnconfigure(0, weight=1)
        self.editor_timeline = tk.Scale(
            timeline, from_=0, to=1000, orient="horizontal", showvalue=False,
            variable=self.editor_timeline_var, bg=PANEL, troughcolor="#3c424a",
            activebackground=PINK, highlightthickness=0, sliderlength=24, width=18,
        )
        self.editor_timeline.grid(row=0, column=0, sticky="ew")
        self.editor_timeline.bind("<ButtonPress-1>", self._editor_seek_start)
        self.editor_timeline.bind("<ButtonRelease-1>", self._editor_seek_finish)
        tk.Label(timeline, textvariable=self.editor_time_var, bg=PANEL, fg=MUTED, font=(MONO_FONT, 10)).grid(
            row=0, column=1, padx=(14, 0)
        )

        controls = tk.Frame(player_panel, bg=PANEL, padx=14)
        controls.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        source_controls = tk.Frame(controls, bg=PANEL)
        source_controls.pack(fill="x")
        ttk.Button(
            source_controls, text="打开视频", style="Compact.TButton",
            command=self._choose_editor_video,
        ).pack(side="left")
        ttk.Button(
            source_controls, text="使用导出页素材", style="Compact.TButton",
            command=self._use_export_source,
        ).pack(side="left", padx=6)
        ttk.Button(
            source_controls, text="截取当前帧", style="Compact.TButton",
            command=self._capture_editor_frame,
        ).pack(side="right")
        playback_controls = tk.Frame(controls, bg=PANEL)
        playback_controls.pack(pady=(7, 0))
        ttk.Button(
            playback_controls, text="◀ 10 秒", style="Compact.TButton",
            command=lambda: self._editor_seek_relative(-10.0),
        ).pack(side="left")
        ttk.Button(
            playback_controls, text="◀ 1 帧", style="Compact.TButton",
            command=lambda: self._editor_frame_step(-1),
        ).pack(side="left", padx=(5, 0))
        ttk.Button(
            playback_controls, text="播放 / 暂停", style="Compact.TButton",
            command=self._editor_toggle_play,
        ).pack(side="left", padx=5)
        ttk.Button(
            playback_controls, text="1 帧 ▶", style="Compact.TButton",
            command=lambda: self._editor_frame_step(1),
        ).pack(side="left")
        ttk.Button(
            playback_controls, text="10 秒 ▶", style="Compact.TButton",
            command=lambda: self._editor_seek_relative(10.0),
        ).pack(side="left", padx=(5, 0))

        seek_line = tk.Frame(player_panel, bg=PANEL, padx=14)
        seek_line.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        tk.Label(
            seek_line, text="统一时间码  HH:MM:SS.mmm", bg=PANEL, fg=MUTED,
            font=(UI_FONT, 10, "bold"),
        ).pack(side="left")
        tk.Entry(
            seek_line, textvariable=self.editor_seek_var, width=18, bg=INPUT, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=PINK, font=(MONO_FONT, 11),
        ).pack(side="left", padx=(12, 8), ipady=7)
        ttk.Button(seek_line, text="跳转", command=self._seek_editor_time).pack(side="left")
        ttk.Button(seek_line, text="填入当前位置", command=self._copy_current_time_to_seek).pack(side="left", padx=8)

        tk.Label(
            player_panel, textvariable=self.editor_status_var, bg=PANEL, fg=BLUE,
            font=(UI_FONT, 10), anchor="w", justify="left",
        ).grid(row=5, column=0, sticky="ew", padx=14, pady=(0, 12))

        toolbox = ttk.Notebook(workspace)
        workspace.add(toolbox, weight=3)
        trim_panel = tk.Frame(toolbox, bg=PANEL_ALT, padx=20, pady=18)
        join_panel = tk.Frame(toolbox, bg=PANEL_ALT, padx=20, pady=18)
        split_panel = tk.Frame(toolbox, bg=PANEL_ALT, padx=20, pady=18)
        proxy_panel = tk.Frame(toolbox, bg=PANEL_ALT, padx=20, pady=18)
        toolbox.add(trim_panel, text="  裁切  ")
        toolbox.add(join_panel, text="  拼接 / 换音  ")
        toolbox.add(split_panel, text="  音视频分离  ")
        toolbox.add(proxy_panel, text="  1080p 代理  ")
        self._build_trim_panel(trim_panel)
        self._build_join_panel(join_panel)
        self._build_split_panel(split_panel)
        self._build_proxy_panel(proxy_panel)
        self.editor_toolbox = toolbox
        self.editor_proxy_panel = proxy_panel
        self.lossless_panel = tk.Frame(toolbox, bg=PANEL_ALT)
        toolbox.add(self.lossless_panel, text="  无损分段  ")
        self.lossless_workspace.build(self.lossless_panel)
        self.after_idle(self._set_initial_editor_pane_size)

        timeline_panel = tk.Frame(
            parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER,
        )
        timeline_panel.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        self._build_join_timeline_panel(timeline_panel)

    def _set_initial_editor_pane_size(self, event=None) -> None:
        if getattr(self, "editor_pane_initialized", False):
            return
        width = int(getattr(event, "width", 0) or self.editor_workspace_pane.winfo_width())
        if width < 600:
            return
        try:
            self.editor_workspace_pane.sashpos(0, int(width * 0.36))
            self.editor_pane_initialized = True
        except tk.TclError:
            pass

    def _resize_editor_preview_surface(self, _event=None) -> None:
        if not hasattr(self, "player_stage") or not hasattr(self, "player_surface"):
            return
        width = int(self.player_stage.winfo_width()) - 20
        height = int(self.player_stage.winfo_height()) - 16
        if width < 80 or height < 45:
            return
        media_width = float(getattr(self.editor_media, "width", 0.0) or 0.0)
        media_height = float(getattr(self.editor_media, "height", 0.0) or 0.0)
        if media_width <= 0 or media_height <= 0:
            media_width, media_height = 16.0, 9.0
        scale_text = self.editor_preview_scale_var.get()
        if scale_text == "适应窗口":
            scale = 1.0
        else:
            try:
                scale = min(1.0, max(0.25, float(scale_text.rstrip("%")) / 100.0))
            except ValueError:
                scale = 1.0
        surface_width, surface_height = fit_aspect_size(
            width, height, media_width, media_height, scale=scale,
        )
        self.player_surface.place_configure(
            relx=0.5, rely=0.5, anchor="center",
            width=surface_width, height=surface_height,
        )

    def _adjust_editor_preview_scale(self, direction: int) -> None:
        levels = ["50%", "60%", "75%", "90%", "适应窗口"]
        current = self.editor_preview_scale_var.get()
        try:
            index = levels.index(current)
        except ValueError:
            index = len(levels) - 1
        index = min(len(levels) - 1, max(0, index + (1 if direction > 0 else -1)))
        self.editor_preview_scale_var.set(levels[index])
        self._resize_editor_preview_surface()

    def _build_join_timeline_panel(self, panel: tk.Frame) -> None:
        self._add_glass_shine(panel)
        header = tk.Frame(panel, bg=PANEL, padx=10)
        header.pack(fill="x", pady=(9, 5))
        tk.Label(header, text="工程时间线", bg=PANEL, fg=TEXT, font=(UI_FONT, 13, "bold")).pack(side="left")
        tk.Label(
            header, textvariable=self.editor_join_timeline_summary_var, bg=PANEL,
            fg=BLUE, font=(UI_FONT, 9),
        ).pack(side="left", padx=(14, 0))

        toolbar = tk.Frame(panel, bg=PANEL, padx=10)
        toolbar.pack(fill="x", pady=(0, 7))
        actions = tk.Frame(toolbar, bg=PANEL)
        actions.pack(side="left")
        ttk.Button(
            actions, text="自动对齐", style="Compact.TButton", command=self._auto_align_join_audio,
        ).pack(side="left")
        ttk.Button(
            actions, text="试听选中", style="Compact.TButton", command=self._preview_join_alignment,
        ).pack(side="left", padx=(5, 0))
        ttk.Button(
            actions, text="设起点", style="Compact.TButton", command=lambda: self._set_join_range_from_playhead("start"),
        ).pack(side="left", padx=(12, 0))
        ttk.Button(
            actions, text="设终点", style="Compact.TButton", command=lambda: self._set_join_range_from_playhead("end"),
        ).pack(side="left", padx=(5, 0))
        ttk.Button(
            actions, text="清除范围", style="Compact.TButton", command=self._clear_join_timeline_range,
        ).pack(side="left", padx=(5, 0))

        zoom = tk.Frame(toolbar, bg=PANEL)
        zoom.pack(side="right")
        tk.Label(
            zoom, textvariable=self.editor_join_timeline_zoom_text_var, bg=PANEL, fg=GREEN,
            font=(UI_FONT, 8, "bold"), width=23, anchor="e",
        ).pack(side="left", padx=(0, 7))
        ttk.Button(
            zoom, text="－", style="Compact.TButton", command=lambda: self._set_join_timeline_zoom(1 / 1.6),
        ).pack(side="left")
        self.editor_join_timeline_zoom_slider = tk.Scale(
            zoom, from_=0, to=100, resolution=0.2, orient="horizontal", showvalue=False,
            variable=self.editor_join_timeline_zoom_slider_var,
            command=self._join_timeline_zoom_slider_changed,
            bg=PANEL, troughcolor="#3c424a", activebackground=PINK,
            highlightthickness=0, sliderlength=18, width=12, length=210,
        )
        self.editor_join_timeline_zoom_slider.pack(side="left", padx=5)
        ttk.Button(
            zoom, text="＋", style="Compact.TButton", command=lambda: self._set_join_timeline_zoom(1.6),
        ).pack(side="left")
        ttk.Button(
            zoom, text="逐帧", style="Compact.TButton", command=self._zoom_join_timeline_to_frames,
        ).pack(side="left", padx=(7, 0))
        ttk.Button(
            zoom, text="适配", style="Compact.TButton", command=self._fit_join_timeline,
        ).pack(side="left", padx=(5, 0))

        canvas_holder = tk.Frame(panel, bg=INPUT, padx=0, pady=0)
        canvas_holder.pack(fill="both", expand=True, padx=8)
        self.editor_join_timeline_canvas = tk.Canvas(
            canvas_holder, bg="#191a1c", height=TIMELINE_HEIGHT, highlightthickness=0,
            xscrollincrement=1, cursor="hand2",
        )
        timeline_scroll = ttk.Scrollbar(
            canvas_holder, orient="horizontal", command=self._scroll_join_timeline,
        )
        self.editor_join_timeline_canvas.configure(xscrollcommand=timeline_scroll.set)
        self.editor_join_timeline_canvas.pack(fill="both", expand=True)
        timeline_scroll.pack(fill="x")
        self.editor_join_timeline_canvas.bind("<Configure>", lambda _event: self._draw_join_timeline())
        self.editor_join_timeline_canvas.bind("<ButtonPress-1>", self._join_timeline_press)
        self.editor_join_timeline_canvas.bind("<B1-Motion>", self._join_timeline_motion)
        self.editor_join_timeline_canvas.bind("<ButtonRelease-1>", self._join_timeline_release)
        self.editor_join_timeline_canvas.bind("<Double-Button-1>", self._join_timeline_double_click)
        self.editor_join_timeline_canvas.bind("<Control-MouseWheel>", self._join_timeline_mouse_zoom)
        self.editor_join_timeline_canvas.bind("<Shift-MouseWheel>", self._join_timeline_mouse_scroll)
        self.editor_join_timeline_canvas.bind("<Key-plus>", lambda _event: self._set_join_timeline_zoom(1.6))
        self.editor_join_timeline_canvas.bind("<Key-minus>", lambda _event: self._set_join_timeline_zoom(1 / 1.6))
        tk.Label(
            panel, textvariable=self.editor_join_timeline_hint_var, bg=PANEL, fg=MUTED,
            font=(UI_FONT, 8), anchor="w",
        ).pack(fill="x", padx=10, pady=(4, 7))
        self.after_idle(self._draw_join_timeline)

    @staticmethod
    def _join_timeline_tick_step(seconds_per_100px: float) -> float:
        value = max(0.001, float(seconds_per_100px))
        power = 10 ** int(math.floor(math.log10(value)))
        scaled = value / power
        multiple = 1 if scaled <= 1 else (2 if scaled <= 2 else (5 if scaled <= 5 else 10))
        return multiple * power

    def _get_join_timeline_model(self):
        durations = [
            float(self.editor_join_media_cache[path].duration)
            if path in self.editor_join_media_cache else 0.0
            for path in self.editor_join_files
        ]
        return build_join_timeline_model(
            self.editor_join_files, durations, self.editor_join_alignments,
            audio_duration=self.editor_join_audio_duration,
        )

    def _draw_join_timeline(self) -> None:
        if not hasattr(self, "editor_join_timeline_canvas"):
            return
        canvas = self.editor_join_timeline_canvas
        width = max(600, canvas.winfo_width())
        model = self._get_join_timeline_model()
        self.editor_join_timeline_model = model
        self.editor_join_timeline_items.clear()
        canvas.delete("all")

        left = TIMELINE_LEFT
        visible_duration = max(1.0, model.duration)
        fit_pps = max(0.02, (width - left - 18) / visible_duration)
        fps = self._join_timeline_fps()
        max_zoom = timeline_frame_zoom_multiplier(
            fit_pps, fps, pixels_per_frame=TIMELINE_FRAME_PIXELS,
        )
        self.editor_join_timeline_fit_pps = fit_pps
        self.editor_join_timeline_max_zoom = max_zoom
        self.editor_join_timeline_zoom = min(max_zoom, max(1.0, self.editor_join_timeline_zoom))
        pps = fit_pps * self.editor_join_timeline_zoom
        canvas_width = max(width, int(left + visible_duration * pps + 24))
        self.editor_join_timeline_canvas_width = float(canvas_width)
        self.editor_join_timeline_pixels_per_second = pps
        self.editor_join_timeline_origin = model.start
        canvas.configure(scrollregion=(0, 0, canvas_width, TIMELINE_HEIGHT))
        self._sync_join_timeline_zoom_controls()

        if not model.clips:
            canvas.create_text(
                width / 2, TIMELINE_HEIGHT / 2,
                text="在“拼接 / 换音”中添加视频，时间线会自动出现",
                fill=MUTED, font=(UI_FONT, 10),
            )
            self.editor_join_timeline_summary_var.set("添加片段后显示工程时间线")
            return

        def xpos(seconds: float) -> float:
            return left + (seconds - model.start) * pps

        if model.audio_duration > 0:
            audio_x0, audio_x1 = xpos(0.0), xpos(model.audio_duration)
            canvas.create_rectangle(
                audio_x0, TIMELINE_AUDIO_TOP, audio_x1, TIMELINE_AUDIO_BOTTOM,
                fill="#21445c", outline="#5ab7d9", width=1,
            )
            canvas.create_text(
                audio_x0 + 7, (TIMELINE_AUDIO_TOP + TIMELINE_AUDIO_BOTTOM) / 2,
                text=f"主音频 · {format_time(model.audio_duration)}",
                anchor="w", fill="#c7f0ff", font=(UI_FONT, 7, "bold"),
            )

        selected = set(self.editor_join_listbox.curselection()) if hasattr(self, "editor_join_listbox") else set()
        for gap in model.gaps:
            x0, x1 = xpos(gap.start), xpos(gap.end)
            canvas.create_rectangle(
                x0, TIMELINE_VIDEO_TOP, x1, TIMELINE_VIDEO_BOTTOM,
                fill="#07070a", outline="#555c65", width=1,
            )
            if x1 - x0 >= 55:
                canvas.create_text(
                    (x0 + x1) / 2, (TIMELINE_VIDEO_TOP + TIMELINE_VIDEO_BOTTOM) / 2,
                    text=f"黑场\n{format_time(gap.duration)}",
                    fill="#a4abb4", font=(UI_FONT, 7), justify="center",
                )

        for previous, current in zip(model.clips, model.clips[1:]):
            overlap = previous.end - current.start
            if previous.aligned and current.aligned and overlap > 0.0005:
                x0, x1 = xpos(current.start), xpos(min(previous.end, current.end))
                canvas.create_rectangle(
                    x0, TIMELINE_VIDEO_TOP - 3, x1, TIMELINE_VIDEO_BOTTOM + 3,
                    fill="#5a1730", outline=RED, stipple="gray50",
                )
                if x1 - x0 > 70:
                    canvas.create_text(
                        (x0 + x1) / 2, (TIMELINE_VIDEO_TOP + TIMELINE_VIDEO_BOTTOM) / 2,
                        text=f"重叠 {overlap:.2f}s", fill="#ffd8e2", font=(UI_FONT, 7, "bold"),
                    )

        for clip in model.clips:
            x0, x1 = xpos(clip.start), xpos(clip.end)
            if x1 - x0 < 32:
                x1 = x0 + 32
            active = clip.index in selected
            fill = "#4e7f9d" if active else ("#37596c" if clip.aligned else "#343a43")
            outline = "#b6cbd8" if active else (PINK if clip.aligned else "#626b77")
            tags = (f"timeline_clip_{clip.index}", "timeline_clip")
            rect = canvas.create_rectangle(
                x0, TIMELINE_VIDEO_TOP, x1, TIMELINE_VIDEO_BOTTOM,
                fill=fill, outline=outline, width=2 if active else 1, tags=tags,
            )
            self.editor_join_timeline_items[clip.index] = rect
            if self.editor_join_leading_mode_var.get().startswith("裁掉") and clip.start < 0:
                crop_x1 = xpos(min(0.0, clip.end))
                canvas.create_rectangle(
                    x0, TIMELINE_VIDEO_TOP, crop_x1, TIMELINE_VIDEO_BOTTOM,
                    fill="#1d2024", outline="#697581",
                    stipple="gray50", tags=tags,
                )
                if crop_x1 - x0 > 58:
                    canvas.create_text(
                        (x0 + crop_x1) / 2, (TIMELINE_VIDEO_TOP + TIMELINE_VIDEO_BOTTOM) / 2,
                        text="开头将裁掉", fill="#c1c8d0",
                        font=(UI_FONT, 7, "bold"), tags=tags,
                    )
            if (
                self.editor_join_trailing_mode_var.get().startswith("裁掉")
                and model.audio_duration > 0 and clip.end > model.audio_duration
            ):
                crop_x0 = xpos(max(model.audio_duration, clip.start))
                canvas.create_rectangle(
                    crop_x0, TIMELINE_VIDEO_TOP, x1, TIMELINE_VIDEO_BOTTOM,
                    fill="#1d2024", outline="#697581",
                    stipple="gray50", tags=tags,
                )
                if x1 - crop_x0 > 58:
                    canvas.create_text(
                        (crop_x0 + x1) / 2, (TIMELINE_VIDEO_TOP + TIMELINE_VIDEO_BOTTOM) / 2,
                        text="结尾将裁掉", fill="#c1c8d0",
                        font=(UI_FONT, 7, "bold"), tags=tags,
                    )
            label_x = x0 + 7
            if (
                self.editor_join_leading_mode_var.get().startswith("裁掉")
                and clip.start < 0 < clip.end
            ):
                label_x = xpos(0.0) + 7
            confidence = f"{clip.confidence * 100:.0f}%" if clip.aligned else "待对齐"
            canvas.create_text(
                label_x, TIMELINE_VIDEO_TOP + 10,
                text=f"{clip.index + 1:02d}  {Path(clip.path).name}  {confidence}",
                anchor="nw", fill="#ffffff", font=(UI_FONT, 9, "bold"), tags=tags,
            )
            canvas.create_text(
                label_x, TIMELINE_VIDEO_TOP + 39,
                text=f"{format_signed_time(clip.start)} → {format_signed_time(clip.end)}",
                anchor="nw", fill="#c0c8d2", font=(MONO_FONT, 8), tags=tags,
            )

        for variable, label, color in (
            (self.editor_join_range_start_var, "成片起点", GREEN),
            (self.editor_join_range_end_var, "成片终点", "#c8ae72"),
        ):
            try:
                output_position = parse_timecode(variable.get()) if variable.get().strip() else None
            except ValueError:
                output_position = None
            if output_position is not None:
                marker_x = xpos(self._timeline_master_from_output(output_position))
                canvas.create_line(
                    marker_x, TIMELINE_RULER_BOTTOM, marker_x, TIMELINE_AUDIO_BOTTOM,
                    fill=color, width=2, dash=(4, 2),
                )
                canvas.create_text(
                    marker_x + 4, TIMELINE_RULER_BOTTOM + 2, text=label,
                    anchor="nw", fill=color, font=(UI_FONT, 7, "bold"),
                )

        self.editor_join_timeline_playhead_id = canvas.create_line(
            xpos(self.editor_join_timeline_playhead_master), 0,
            xpos(self.editor_join_timeline_playhead_master), TIMELINE_AUDIO_BOTTOM,
            fill=RED, width=2, tags=("timeline_playhead",),
        )
        self.editor_join_timeline_playhead_head = canvas.create_polygon(
            xpos(self.editor_join_timeline_playhead_master) - 6, 0,
            xpos(self.editor_join_timeline_playhead_master) + 6, 0,
            xpos(self.editor_join_timeline_playhead_master), 10,
            fill=RED, outline=RED, tags=("timeline_playhead",),
        )
        audio_name = Path(self.editor_audio_var.get()).name if self.editor_audio_var.get().strip() else "未选择主音频"
        aligned = sum(clip.aligned for clip in model.clips)
        output_duration = self._join_timeline_output_duration(model.duration)
        self.editor_join_timeline_summary_var.set(
            f"全长 {format_time(output_duration)} · {len(model.clips)} 段 · 已对齐 {aligned} 段 · {audio_name}"
        )
        self._draw_join_timeline_ruler()

    def _join_timeline_fps(self) -> float:
        for path in self.editor_join_files:
            info = self.editor_join_media_cache.get(path)
            fps = float(getattr(info, "fps", 0.0) or 0.0)
            if math.isfinite(fps) and fps > 0:
                return fps
        fps = float(getattr(self.editor_media, "fps", 0.0) or 0.0)
        return fps if math.isfinite(fps) and fps > 0 else 30.0

    def _sync_join_timeline_zoom_controls(self) -> None:
        max_zoom = max(1.0, float(self.editor_join_timeline_max_zoom))
        zoom = min(max_zoom, max(1.0, float(self.editor_join_timeline_zoom)))
        if max_zoom <= 1.000001:
            slider_value = 0.0
        else:
            slider_value = math.log(zoom) / math.log(max_zoom) * 100.0
        self.editor_join_timeline_zoom_updating = True
        try:
            self.editor_join_timeline_zoom_slider_var.set(slider_value)
        finally:
            self.editor_join_timeline_zoom_updating = False
        fps = self._join_timeline_fps()
        frame_pixels = self.editor_join_timeline_pixels_per_second / fps
        if zoom <= 1.0001:
            text = "适配全长"
        elif frame_pixels >= TIMELINE_FRAME_PIXELS * 0.98:
            text = f"逐帧 · {fps:.3f} FPS · {frame_pixels:.0f}px/帧"
        else:
            visible_seconds = max(
                0.001,
                self.editor_join_timeline_canvas.winfo_width()
                / max(0.000001, self.editor_join_timeline_pixels_per_second),
            )
            text = f"{zoom:,.1f}× · 可视 {format_time(visible_seconds)}"
        self.editor_join_timeline_zoom_text_var.set(text)

    def _draw_join_timeline_ruler(self) -> None:
        if not hasattr(self, "editor_join_timeline_canvas") or self.editor_join_timeline_model is None:
            return
        canvas = self.editor_join_timeline_canvas
        model = self.editor_join_timeline_model
        if not model.clips:
            return
        canvas.delete("timeline_ruler")
        canvas.delete("timeline_overlay")
        pps = max(0.000001, self.editor_join_timeline_pixels_per_second)
        view_left = canvas.canvasx(0)
        view_right = canvas.canvasx(max(1, canvas.winfo_width()))
        start = self.editor_join_timeline_origin + (view_left - TIMELINE_LEFT) / pps
        end = self.editor_join_timeline_origin + (view_right - TIMELINE_LEFT) / pps
        step = self._join_timeline_tick_step(TIMELINE_MAJOR_TICK_PIXELS / pps)
        minor_step = step / 5.0
        minor_index = math.floor(start / minor_step)
        minor_count = 0
        while minor_index * minor_step <= end + minor_step and minor_count < 4000:
            minor_time = minor_index * minor_step
            if minor_index % 5:
                x = TIMELINE_LEFT + (minor_time - self.editor_join_timeline_origin) * pps
                canvas.create_line(
                    x, 35, x, TIMELINE_RULER_BOTTOM,
                    fill="#42464d", width=1, tags=("timeline_ruler",),
                )
            minor_index += 1
            minor_count += 1
        first_tick = math.floor(start / step) * step
        tick = first_tick
        count = 0
        while tick <= end + step and count < 2000:
            x = TIMELINE_LEFT + (tick - self.editor_join_timeline_origin) * pps
            canvas.create_line(
                x, TIMELINE_RULER_BOTTOM, x, TIMELINE_AUDIO_BOTTOM,
                fill="#2d3035", width=1, tags=("timeline_ruler",),
            )
            canvas.create_line(
                x, 22, x, TIMELINE_RULER_BOTTOM, fill="#646b75", width=1,
                tags=("timeline_ruler",),
            )
            label = format_timeline_ruler_time(tick, step)
            canvas.create_text(
                x + 4, 3, text=label, anchor="nw", fill="#969ca5",
                font=(MONO_FONT, 8), tags=("timeline_ruler",),
            )
            tick += step
            count += 1

        fps = self._join_timeline_fps()
        frame_pixels = pps / fps
        if frame_pixels >= 3.5:
            first_frame = math.ceil(start * fps - 0.000001)
            last_frame = math.floor(end * fps + 0.000001)
            nominal_fps = max(1, int(round(fps)))
            maximum_frames = min(last_frame, first_frame + 1600)
            for frame_index in range(first_frame, maximum_frames + 1):
                frame_time = frame_index / fps
                x = TIMELINE_LEFT + (frame_time - self.editor_join_timeline_origin) * pps
                every_five = frame_index % 5 == 0
                y0 = 31 if every_five else 38
                canvas.create_line(
                    x, y0, x, TIMELINE_RULER_BOTTOM,
                    fill="#b7cad6" if every_five else "#5e6b78",
                    width=1, tags=("timeline_ruler",),
                )
                show_number = frame_pixels >= 24 or (every_five and frame_pixels * 5 >= 42)
                if show_number:
                    canvas.create_text(
                        x + 2, 24, text=f"{frame_index % nominal_fps:02d}",
                        anchor="nw", fill="#bbcad8", font=(MONO_FONT, 6),
                        tags=("timeline_ruler",),
                    )

        canvas.tag_lower("timeline_ruler")
        canvas.create_rectangle(
            view_left, TIMELINE_RULER_BOTTOM, view_left + TIMELINE_LEFT - 5,
            TIMELINE_AUDIO_BOTTOM, fill="#191a1c", outline="#323841",
            tags=("timeline_overlay",),
        )
        canvas.create_text(
            view_left + 10, (TIMELINE_VIDEO_TOP + TIMELINE_VIDEO_BOTTOM) / 2,
            text="画面", anchor="w", fill=MUTED, font=(UI_FONT, 9, "bold"),
            tags=("timeline_overlay",),
        )
        canvas.create_text(
            view_left + 10, (TIMELINE_AUDIO_TOP + TIMELINE_AUDIO_BOTTOM) / 2,
            text="音频", anchor="w", fill=MUTED, font=(UI_FONT, 9, "bold"),
            tags=("timeline_overlay",),
        )
        if frame_pixels >= 3.5:
            canvas.create_text(
                view_left + 10, 33, text="帧", anchor="w", fill=PURPLE_BRIGHT,
                font=(UI_FONT, 8, "bold"), tags=("timeline_overlay",),
            )
        canvas.tag_raise("timeline_overlay")
        canvas.tag_raise("timeline_playhead")

    def _join_timeline_master_at(self, event) -> float:
        canvas_x = self.editor_join_timeline_canvas.canvasx(event.x)
        master = self.editor_join_timeline_origin + (
            canvas_x - TIMELINE_LEFT
        ) / max(0.000001, self.editor_join_timeline_pixels_per_second)
        frame_pixels = self.editor_join_timeline_pixels_per_second / self._join_timeline_fps()
        if frame_pixels >= 3.5:
            fps = self._join_timeline_fps()
            master = round(master * fps) / fps
        return master

    def _join_timeline_clip_at(self, event) -> int | None:
        canvas = self.editor_join_timeline_canvas
        canvas_x, canvas_y = canvas.canvasx(event.x), canvas.canvasy(event.y)
        items = canvas.find_overlapping(canvas_x, canvas_y, canvas_x, canvas_y)
        for item in reversed(items):
            for tag in canvas.gettags(item):
                if tag.startswith("timeline_clip_"):
                    try:
                        return int(tag.rsplit("_", 1)[1])
                    except ValueError:
                        pass
        return None

    def _select_join_timeline_clip(self, index: int) -> None:
        if not hasattr(self, "editor_join_listbox") or not (0 <= index < len(self.editor_join_files)):
            return
        self.editor_join_listbox.selection_clear(0, "end")
        self.editor_join_listbox.selection_set(index)
        self.editor_join_listbox.see(index)
        self._load_selected_join_alignment()

    def _join_timeline_press(self, event) -> str:
        canvas = self.editor_join_timeline_canvas
        canvas.focus_set()
        if float(event.y) < TIMELINE_RULER_BOTTOM:
            self.editor_join_timeline_drag = None
            self.editor_join_timeline_pan = {
                "press_x": float(event.x),
                "master": self._join_timeline_master_at(event),
                "moved": False,
            }
            canvas.scan_mark(int(event.x), int(event.y))
            canvas.configure(cursor="fleur")
            return "break"
        self.editor_join_timeline_pan = None
        master = self._join_timeline_master_at(event)
        index = self._join_timeline_clip_at(event)
        self._update_join_timeline_playhead(master)
        if index is not None:
            self._select_join_timeline_clip(index)
            model = self.editor_join_timeline_model
            if not self.working and model is not None and index < len(model.clips):
                self.editor_join_timeline_drag = {
                    "index": index,
                    "anchor_x": self.editor_join_timeline_canvas.canvasx(event.x),
                    "start": model.clips[index].start,
                }
                self._draw_join_timeline()
        return "break"

    def _join_timeline_motion(self, event) -> str:
        pan = self.editor_join_timeline_pan
        if pan is not None:
            if abs(float(event.x) - float(pan["press_x"])) >= 3.0:
                pan["moved"] = True
                self.editor_join_timeline_canvas.scan_dragto(
                    int(event.x), int(event.y), gain=1,
                )
                self._draw_join_timeline_ruler()
            return "break"
        drag = self.editor_join_timeline_drag
        if not drag:
            return "break"
        index = int(drag["index"])
        dx = self.editor_join_timeline_canvas.canvasx(event.x) - float(drag["anchor_x"])
        self.editor_join_timeline_canvas.move(f"timeline_clip_{index}", dx, 0)
        drag["anchor_x"] = self.editor_join_timeline_canvas.canvasx(event.x)
        return "break"

    def _join_timeline_release(self, event) -> str:
        pan = self.editor_join_timeline_pan
        if pan is not None:
            self.editor_join_timeline_pan = None
            self.editor_join_timeline_canvas.configure(cursor="hand2")
            if not bool(pan["moved"]):
                self._update_join_timeline_playhead(float(pan["master"]))
            self._draw_join_timeline_ruler()
            return "break"
        drag = self.editor_join_timeline_drag
        self.editor_join_timeline_drag = None
        if not drag:
            return "break"
        index = int(drag["index"])
        rect = self.editor_join_timeline_items.get(index)
        if rect is None:
            return "break"
        coords = self.editor_join_timeline_canvas.coords(rect)
        if not coords:
            return "break"
        new_start = self.editor_join_timeline_origin + (
            coords[0] - TIMELINE_LEFT
        ) / max(0.000001, self.editor_join_timeline_pixels_per_second)
        fps = self._join_timeline_fps()
        if self.editor_join_timeline_pixels_per_second / fps >= 3.5:
            new_start = round(new_start * fps) / fps
        if abs(new_start - float(drag["start"])) < 0.01:
            self._draw_join_timeline()
            return "break"
        self._select_join_timeline_clip(index)
        self._set_manual_join_alignment(round(new_start, 6))
        return "break"

    def _join_timeline_double_click(self, event) -> str:
        index = self._join_timeline_clip_at(event)
        if index is not None:
            self._select_join_timeline_clip(index)
            master = self._join_timeline_master_at(event)
            self._update_join_timeline_playhead(master)
            self._preview_join_alignment(master_seek=master)
        return "break"

    def _join_timeline_mouse_zoom(self, event) -> str:
        anchor_master = self._join_timeline_master_at(event)
        self._set_join_timeline_zoom(
            1.35 if event.delta > 0 else 1 / 1.35,
            anchor_master=anchor_master, anchor_screen_x=float(event.x),
        )
        return "break"

    def _join_timeline_mouse_scroll(self, event) -> str:
        self.editor_join_timeline_canvas.xview_scroll(-3 if event.delta > 0 else 3, "units")
        self._draw_join_timeline_ruler()
        return "break"

    def _scroll_join_timeline(self, *args) -> None:
        self.editor_join_timeline_canvas.xview(*args)
        self._draw_join_timeline_ruler()

    def _join_timeline_zoom_slider_changed(self, raw_value: str) -> None:
        if self.editor_join_timeline_zoom_updating:
            return
        maximum = max(1.0, self.editor_join_timeline_max_zoom)
        fraction = min(1.0, max(0.0, float(raw_value) / 100.0))
        target = maximum ** fraction
        self._set_join_timeline_zoom_absolute(target)

    def _set_join_timeline_zoom(
        self, factor: float, *,
        anchor_master: float | None = None,
        anchor_screen_x: float | None = None,
    ) -> None:
        self._set_join_timeline_zoom_absolute(
            self.editor_join_timeline_zoom * factor,
            anchor_master=anchor_master, anchor_screen_x=anchor_screen_x,
        )

    def _set_join_timeline_zoom_absolute(
        self, target: float, *,
        anchor_master: float | None = None,
        anchor_screen_x: float | None = None,
    ) -> None:
        if not hasattr(self, "editor_join_timeline_canvas"):
            return
        canvas = self.editor_join_timeline_canvas
        screen_width = max(1.0, float(canvas.winfo_width()))
        if anchor_screen_x is None:
            anchor_screen_x = screen_width / 2.0
        if anchor_master is None:
            old_canvas_x = canvas.canvasx(anchor_screen_x)
            anchor_master = self.editor_join_timeline_origin + (
                old_canvas_x - TIMELINE_LEFT
            ) / max(0.000001, self.editor_join_timeline_pixels_per_second)
        self.editor_join_timeline_zoom = min(
            max(1.0, self.editor_join_timeline_max_zoom), max(1.0, float(target)),
        )
        self._draw_join_timeline()
        target_canvas_x = TIMELINE_LEFT + (
            float(anchor_master) - self.editor_join_timeline_origin
        ) * self.editor_join_timeline_pixels_per_second
        maximum_left = max(0.0, self.editor_join_timeline_canvas_width - screen_width)
        desired_left = min(maximum_left, max(0.0, target_canvas_x - anchor_screen_x))
        canvas.xview_moveto(
            desired_left / max(1.0, self.editor_join_timeline_canvas_width)
        )
        self._draw_join_timeline_ruler()

    def _zoom_join_timeline_to_frames(self) -> None:
        if hasattr(self, "editor_join_timeline_canvas"):
            self._draw_join_timeline()
        self._set_join_timeline_zoom_absolute(
            self.editor_join_timeline_max_zoom,
            anchor_master=self.editor_join_timeline_playhead_master,
        )

    def _fit_join_timeline(self) -> None:
        self.editor_join_timeline_zoom = 1.0
        if hasattr(self, "editor_join_timeline_canvas"):
            self.editor_join_timeline_canvas.xview_moveto(0.0)
        self._draw_join_timeline()

    def _update_join_timeline_playhead(self, master_time: float) -> None:
        self.editor_join_timeline_playhead_master = float(master_time)
        if not hasattr(self, "editor_join_timeline_canvas") or self.editor_join_timeline_model is None:
            return
        x = TIMELINE_LEFT + (
            self.editor_join_timeline_playhead_master - self.editor_join_timeline_origin
        ) * self.editor_join_timeline_pixels_per_second
        canvas = self.editor_join_timeline_canvas
        if hasattr(self, "editor_join_timeline_playhead_id"):
            canvas.coords(
                self.editor_join_timeline_playhead_id, x, 0, x, TIMELINE_AUDIO_BOTTOM,
            )
            canvas.coords(
                self.editor_join_timeline_playhead_head,
                x - 6, 0, x + 6, 0, x, 10,
            )
        fps = self._join_timeline_fps()
        frame_detail = ""
        if self.editor_join_timeline_pixels_per_second / fps >= 3.5:
            frame_detail = f" · 第 {round(self.editor_join_timeline_playhead_master * fps):,} 帧"
        self.editor_join_timeline_hint_var.set(
            f"播放头 {format_signed_time(self.editor_join_timeline_playhead_master)}{frame_detail} · "
            "拖动顶部标尺横移 · 拖动片段微调"
        )

    def _timeline_output_position(self, master_time: float) -> float:
        model = self.editor_join_timeline_model or self._get_join_timeline_model()
        if not model.clips:
            return 0.0
        geometry = self._join_timeline_export_geometry()
        if geometry is not None:
            intervals, audio_origin = geometry
            if self.editor_join_gap_var.get().startswith("保留"):
                return max(0.0, master_time - audio_origin)
            elapsed = 0.0
            for start, duration in intervals:
                if master_time <= start + duration:
                    return max(0.0, elapsed + max(0.0, master_time - start))
                elapsed += duration
            return elapsed
        if self.editor_join_gap_var.get().startswith("保留"):
            return max(0.0, master_time - model.clips[0].start)
        elapsed = 0.0
        for clip in model.clips:
            if master_time <= clip.end:
                return max(0.0, elapsed + max(0.0, master_time - clip.start))
            elapsed += clip.duration
        return elapsed

    def _join_timeline_export_geometry(self) -> tuple[list[tuple[float, float]], float] | None:
        paths = self.editor_join_files
        if not paths or any(path not in self.editor_join_alignments for path in paths):
            return None
        if any(path not in self.editor_join_media_cache for path in paths):
            return None
        durations = [float(self.editor_join_media_cache[path].duration) for path in paths]
        if any(duration <= 0 for duration in durations):
            return None
        results = [self.editor_join_alignments[path] for path in paths]
        trim_before = self.editor_join_leading_mode_var.get().startswith("裁掉")
        keep_before = self.editor_join_leading_mode_var.get().startswith("保留")
        try:
            _trims, intervals = aligned_clip_timeline(
                results, durations, trim_before_audio=trim_before, keep_before_audio=keep_before,
            )
            if self.editor_join_gap_var.get().startswith("保留"):
                _starts, _gaps, origin, _duration = gap_preserving_timeline(
                    results, durations, trim_before_audio=trim_before, keep_before_audio=keep_before,
                )
            else:
                origin = intervals[0][0]
            return intervals, origin
        except ValueError:
            return None

    def _join_timeline_output_duration(self, fallback: float) -> float:
        paths = self.editor_join_files
        if not paths or any(path not in self.editor_join_alignments for path in paths):
            return fallback
        if any(path not in self.editor_join_media_cache for path in paths):
            return fallback
        try:
            custom_start, custom_end = self._join_manual_output_range()
            output_start, output_end, _full = resolve_join_output_range(
                [self.editor_join_alignments[path] for path in paths],
                [float(self.editor_join_media_cache[path].duration) for path in paths],
                trim_before_audio=self.editor_join_leading_mode_var.get().startswith("裁掉"),
                keep_before_audio=self.editor_join_leading_mode_var.get().startswith("保留"),
                preserve_gaps=self.editor_join_gap_var.get().startswith("保留"),
                master_audio_duration=self.editor_join_audio_duration or None,
                trim_after_audio=(
                    bool(self.editor_audio_var.get().strip())
                    and self.editor_join_trailing_mode_var.get().startswith("裁掉")
                ),
                custom_start=custom_start,
                custom_end=custom_end,
            )
            return max(0.0, output_end - output_start)
        except ValueError:
            return fallback

    def _timeline_master_from_output(self, output_position: float) -> float:
        model = self.editor_join_timeline_model or self._get_join_timeline_model()
        if not model.clips:
            return 0.0
        remaining = max(0.0, float(output_position))
        geometry = self._join_timeline_export_geometry()
        if geometry is not None:
            intervals, audio_origin = geometry
            if self.editor_join_gap_var.get().startswith("保留"):
                return audio_origin + remaining
            for start, duration in intervals:
                if remaining <= duration:
                    return start + remaining
                remaining -= duration
            return intervals[-1][0] + intervals[-1][1]
        if self.editor_join_gap_var.get().startswith("保留"):
            return model.clips[0].start + remaining
        for clip in model.clips:
            if remaining <= clip.duration:
                return clip.start + remaining
            remaining -= clip.duration
        return model.clips[-1].end

    def _set_join_range_from_playhead(self, boundary: str) -> None:
        value = format_time(self._timeline_output_position(self.editor_join_timeline_playhead_master))
        if boundary == "start":
            self.editor_join_range_start_var.set(value)
            label = "起点"
        else:
            self.editor_join_range_end_var.set(value)
            label = "终点"
        self.editor_join_timeline_hint_var.set(f"已把播放头设为成片{label}：{value}")
        self._draw_join_timeline()

    def _clear_join_timeline_range(self) -> None:
        self.editor_join_range_start_var.set("")
        self.editor_join_range_end_var.set("")
        self.editor_join_timeline_hint_var.set("已恢复自动成片范围 · 拖动顶部标尺横移 · 拖动片段微调")
        self._draw_join_timeline()

    def _build_subtitles(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=2, minsize=560)
        parent.columnconfigure(1, weight=3, minsize=680)
        parent.rowconfigure(0, weight=1)

        settings = tk.Frame(parent, bg=PANEL_ALT, padx=24, pady=20)
        settings.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self._add_glass_shine(settings)
        tk.Label(
            settings, text="AI 自动字幕", bg=PANEL_ALT, fg=TEXT,
            font=(UI_FONT, 17, "bold"),
        ).pack(anchor="w")
        tk.Label(
            settings, text="Faster-Whisper · RTX CUDA FP16 · 本地处理不上传",
            bg=PANEL_ALT, fg=GREEN, font=(UI_FONT, 10, "bold"),
        ).pack(anchor="w", pady=(4, 14))

        self._editor_path_field(settings, "视频或音频文件", self.subtitle_input_var, self._choose_subtitle_input)
        source_buttons = tk.Frame(settings, bg=PANEL_ALT)
        source_buttons.pack(fill="x", pady=(0, 10))
        ttk.Button(source_buttons, text="使用导出页素材", command=self._use_export_source_for_subtitles).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(source_buttons, text="使用剪辑页素材", command=self._use_editor_source_for_subtitles).pack(
            side="left", fill="x", expand=True, padx=(8, 0)
        )
        self._editor_path_field(settings, "SRT 输出文件", self.subtitle_output_var, self._choose_subtitle_output)

        option_grid = tk.Frame(settings, bg=PANEL_ALT)
        option_grid.pack(fill="x", pady=(10, 4))
        option_grid.columnconfigure(0, weight=1)
        option_grid.columnconfigure(1, weight=1)
        tk.Label(
            option_grid, text="识别模型", bg=PANEL_ALT, fg="#d7dbe1",
            font=(UI_FONT, 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            option_grid, text="素材语言", bg=PANEL_ALT, fg="#d7dbe1",
            font=(UI_FONT, 10, "bold"),
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Combobox(
            option_grid, textvariable=self.subtitle_model_var, state="readonly",
            values=list(MODEL_CHOICES),
        ).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Combobox(
            option_grid, textvariable=self.subtitle_language_var, state="readonly",
            values=list(LANGUAGE_CHOICES),
        ).grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(6, 0))

        prompt_box = tk.Frame(settings, bg=PANEL_ALT)
        prompt_box.pack(fill="x", pady=(14, 4))
        tk.Label(
            prompt_box, text="专有名词提示（人名、歌名、地名）", bg=PANEL_ALT,
            fg="#d7dbe1", font=(UI_FONT, 10, "bold"),
        ).pack(anchor="w")
        tk.Entry(
            prompt_box, textvariable=self.subtitle_prompt_var, bg=INPUT, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=PINK, font=(UI_FONT, 11),
        ).pack(fill="x", pady=(6, 0), ipady=10)

        tk.Checkbutton(
            settings, text="启用人声检测，过滤长时间无对白片段",
            variable=self.subtitle_vad_var, bg=PANEL_ALT, fg=TEXT,
            activebackground=PANEL_ALT, activeforeground=TEXT,
            selectcolor=INPUT, font=(UI_FONT, 10), bd=0,
        ).pack(anchor="w", pady=(12, 8))
        ttk.Progressbar(
            settings, variable=self.subtitle_progress_var, maximum=100,
            style="Horizontal.TProgressbar",
        ).pack(fill="x", pady=(4, 8))
        tk.Label(
            settings, textvariable=self.subtitle_status_var, bg=PANEL_ALT, fg=BLUE,
            font=(UI_FONT, 10), justify="left", wraplength=520,
        ).pack(anchor="w", pady=(0, 12))
        self.subtitle_generate_button = ttk.Button(
            settings, text="RTX 识别并生成 SRT", style="Accent.TButton",
            command=self._generate_subtitles,
        )
        self.subtitle_generate_button.pack(fill="x", pady=(4, 8))
        ttk.Button(
            settings, text="安装 AI 字幕组件", command=self._install_subtitle_dependencies,
        ).pack(fill="x", pady=(0, 10))
        tk.Label(
            settings,
            text="首次使用会下载所选模型。Turbo 推荐抢首发；Large-v3 更准但下载更大、识别更慢。"
                 "生成的是 UTF-8 中文 SRT，可直接导入 DaVinci Resolve。",
            bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 9), wraplength=520, justify="left",
        ).pack(anchor="w")

        preview_panel = tk.Frame(parent, bg=PANEL, padx=20, pady=18)
        preview_panel.grid(row=0, column=1, sticky="nsew")
        preview_panel.columnconfigure(0, weight=1)
        preview_panel.rowconfigure(2, weight=1)
        self._add_glass_shine(preview_panel)
        preview_head = tk.Frame(preview_panel, bg=PANEL)
        preview_head.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        tk.Label(
            preview_head, text="字幕实时预览", bg=PANEL, fg=TEXT,
            font=(UI_FONT, 15, "bold"),
        ).pack(side="left")
        tk.Label(
            preview_head, text="●  CUDA GPU", bg=PANEL, fg=GREEN,
            font=(UI_FONT, 10, "bold"),
        ).pack(side="right")
        lyrics_box = tk.Frame(
            preview_panel, bg=INPUT, padx=12, pady=8,
            highlightthickness=1, highlightbackground="#5f3b70",
        )
        lyrics_box.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        lyrics_top = tk.Frame(lyrics_box, bg=INPUT)
        lyrics_top.pack(fill="x")
        tk.Checkbutton(
            lyrics_top, text="启用 OKK GROUP 黄霄雲歌词库校正",
            variable=self.subtitle_lyrics_enabled_var, bg=INPUT, fg=TEXT,
            activebackground=INPUT, activeforeground=TEXT, selectcolor=PANEL_ALT,
            font=(UI_FONT, 10, "bold"), bd=0,
        ).pack(side="left")
        ttk.Combobox(
            lyrics_top, textvariable=self.subtitle_lyrics_mode_var, state="readonly",
            values=list(MATCH_MODE_CHOICES), width=20,
        ).pack(side="right", padx=(8, 0))
        lyrics_bottom = tk.Frame(lyrics_box, bg=INPUT)
        lyrics_bottom.pack(fill="x", pady=(4, 0))
        tk.Label(
            lyrics_bottom, textvariable=self.subtitle_lyrics_status_var, bg=INPUT, fg=GREEN,
            font=(UI_FONT, 9), justify="left",
        ).pack(side="left")
        ttk.Button(
            lyrics_bottom, text="打开歌词库", command=self._open_lyrics_library_folder,
        ).pack(side="right")
        preview_holder = tk.Frame(preview_panel, bg=PANEL)
        preview_holder.grid(row=2, column=0, sticky="nsew")
        preview_scroll = ttk.Scrollbar(preview_holder, orient="vertical")
        self.subtitle_preview = tk.Text(
            preview_holder, bg=INPUT, fg=TEXT, insertbackground=TEXT,
            selectbackground=PURPLE, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, font=(MONO_FONT, 11), wrap="word",
            yscrollcommand=preview_scroll.set, padx=16, pady=14,
        )
        preview_scroll.configure(command=self.subtitle_preview.yview)
        self.subtitle_preview.pack(side="left", fill="both", expand=True)
        preview_scroll.pack(side="right", fill="y")
        self.subtitle_preview.insert(
            "end", "生成时，这里会实时显示：\n\n"
                   "1\n00:00:00,000 --> 00:00:03,500\n识别出的字幕内容\n"
        )
        self.subtitle_preview.configure(state="disabled")
        preview_footer = tk.Frame(preview_panel, bg=PANEL)
        preview_footer.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(
            preview_footer, text="打开字幕所在文件夹", command=self._open_subtitle_folder,
        ).pack(side="right")

    def _build_audio_panel(self, audio_panel: tk.Frame) -> None:
        self._add_glass_shine(audio_panel)
        tk.Label(audio_panel, text="替换音频", bg=PANEL_ALT, fg=TEXT, font=(UI_FONT, 15, "bold")).pack(anchor="w")
        tk.Label(
            audio_panel, text="保留原视频流，不重新编码画面", bg=PANEL_ALT, fg=MUTED,
            font=(UI_FONT, 10),
        ).pack(anchor="w", pady=(4, 14))

        self._editor_path_field(audio_panel, "视频文件", self.editor_video_var, self._choose_editor_video)
        self._editor_path_field(audio_panel, "外部音频", self.editor_audio_var, self._choose_editor_audio)

        offset_box = tk.Frame(audio_panel, bg=PANEL_ALT)
        offset_box.pack(fill="x", pady=(10, 8))
        tk.Label(
            offset_box, text="音频偏移（秒）", bg=PANEL_ALT, fg="#d7dbe1",
            font=(UI_FONT, 11, "bold"),
        ).pack(anchor="w")
        offset_line = tk.Frame(offset_box, bg=PANEL_ALT)
        offset_line.pack(fill="x", pady=(7, 0))
        tk.Entry(
            offset_line, textvariable=self.editor_offset_var, bg=INPUT, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=PINK, font=(MONO_FONT, 12),
        ).pack(side="left", fill="x", expand=True, ipady=10)
        ttk.Button(offset_line, text="自动对齐", command=self._auto_align_audio).pack(side="left", padx=(8, 0))
        tk.Label(
            offset_box, text="正数＝音频延后；负数＝音频提前", bg=PANEL_ALT,
            fg=MUTED, font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(6, 0))

        ttk.Button(audio_panel, text="应用偏移并试听", command=self._preview_external_audio).pack(fill="x", pady=(10, 14))
        self._editor_path_field(audio_panel, "输出文件", self.editor_output_var, self._choose_editor_output)
        ttk.Button(
            audio_panel, text="替换音频并导出", style="Accent.TButton",
            command=self._export_replaced_audio,
        ).pack(fill="x", pady=(14, 10))

    def _build_trim_panel(self, panel: tk.Frame) -> None:
        self._add_glass_shine(panel)
        tk.Label(panel, text="按时间裁切", bg=PANEL_ALT, fg=TEXT, font=(UI_FONT, 15, "bold")).pack(anchor="w")
        tk.Label(
            panel, text="时间码统一为 HH:MM:SS.mmm，可直接手动修改",
            bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 10),
        ).pack(anchor="w", pady=(4, 14))
        self._editor_timecode_field(panel, "入点", self.editor_trim_in_var, self._set_trim_in, self._preview_trim_in)
        self._editor_timecode_field(panel, "出点", self.editor_trim_out_var, self._set_trim_out, self._preview_trim_out)
        tk.Label(panel, text="裁切模式", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 10, "bold")).pack(anchor="w", pady=(12, 6))
        ttk.Combobox(
            panel, textvariable=self.editor_trim_mode_var, state="readonly",
            values=["逐帧精准（沿用编码设置）", "无损极速（按关键帧）"],
        ).pack(fill="x")
        tk.Label(
            panel, text="精准模式沿用“导出与 GPU”页的编码、预设、位深和 CQ；填写下方码率后改为目标码率输出。",
            bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 9), wraplength=410, justify="left",
        ).pack(anchor="w", pady=(7, 10))
        tk.Label(
            panel, text="视频目标码率（可留空使用 CQ；40000 = 40000k）",
            bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 10, "bold"),
        ).pack(anchor="w", pady=(4, 6))
        tk.Entry(
            panel, textvariable=self.editor_trim_bitrate_var, bg=INPUT, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=PINK, font=(MONO_FONT, 11),
        ).pack(fill="x", ipady=9, pady=(0, 10))
        self._editor_path_field(panel, "输出文件", self.editor_trim_output_var, self._choose_trim_output)
        ttk.Button(
            panel, text="裁切并导出", style="Accent.TButton", command=self._export_trim,
        ).pack(fill="x", pady=(16, 8))

    def _editor_timecode_field(self, parent, label, variable, set_command, preview_command) -> None:
        holder = tk.Frame(parent, bg=PANEL_ALT)
        holder.pack(fill="x", pady=7)
        tk.Label(holder, text=label, bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 10, "bold")).pack(anchor="w")
        line = tk.Frame(holder, bg=PANEL_ALT)
        line.pack(fill="x", pady=(6, 0))
        tk.Entry(
            line, textvariable=variable, bg=INPUT, fg=TEXT, insertbackground=TEXT,
            relief="flat", highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=PINK, font=(MONO_FONT, 11),
        ).pack(side="left", fill="x", expand=True, ipady=9)
        ttk.Button(line, text="设为当前", command=set_command).pack(side="left", padx=(8, 0))
        ttk.Button(line, text="预览", command=preview_command).pack(side="left", padx=(8, 0))

    def _build_join_panel(self, panel: tk.Frame) -> None:
        self._add_glass_shine(panel)
        # The join workflow contains all export and RTX settings.  Keep the
        # player visible while this right-hand page scrolls independently.
        canvas = tk.Canvas(panel, bg=PANEL_ALT, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(panel, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y", padx=(8, 0))
        content = tk.Frame(canvas, bg=PANEL_ALT, padx=4, pady=2)
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self.editor_join_canvas = canvas

        tk.Label(content, text="演唱会拼接 + 主音频对齐", bg=PANEL_ALT, fg=TEXT, font=(UI_FONT, 14, "bold")).pack(anchor="w")
        tk.Label(
            content,
            text="每段相机文件分别对齐同一条连续录音；停机期间保留黑场，主音频不中断，第二段自动回到正确时间。",
            bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 9), wraplength=410, justify="left",
        ).pack(anchor="w", pady=(3, 7))
        list_holder = tk.Frame(content, bg=PANEL_ALT)
        list_holder.pack(fill="x")
        list_scrollbar = ttk.Scrollbar(list_holder, orient="vertical")
        self.editor_join_listbox = tk.Listbox(
            list_holder, bg=INPUT, fg=TEXT, selectbackground=PURPLE,
            selectforeground="#ffffff", relief="flat", highlightthickness=1,
            highlightbackground=BORDER, font=(UI_FONT, 9), height=4,
            yscrollcommand=list_scrollbar.set, selectmode="extended",
        )
        list_scrollbar.configure(command=self.editor_join_listbox.yview)
        self.editor_join_listbox.pack(side="left", fill="both", expand=True)
        list_scrollbar.pack(side="right", fill="y")
        self.editor_join_listbox.bind("<Double-Button-1>", lambda _event: self._preview_join_alignment())
        self.editor_join_listbox.bind("<<ListboxSelect>>", self._load_selected_join_alignment)
        buttons = tk.Frame(content, bg=PANEL_ALT)
        buttons.pack(fill="x", pady=(8, 9))
        ttk.Button(buttons, text="添加视频", command=self._add_join_files).pack(side="left")
        ttk.Button(buttons, text="删除", command=self._remove_join_files).pack(side="left", padx=6)
        ttk.Button(buttons, text="上移", command=lambda: self._move_join_file(-1)).pack(side="left")
        ttk.Button(buttons, text="下移", command=lambda: self._move_join_file(1)).pack(side="left", padx=6)
        ttk.Button(buttons, text="清空", command=self._clear_join_files).pack(side="right")

        self._editor_path_field(content, "连续主音频（WAV / FLAC / AAC）", self.editor_audio_var, self._choose_editor_audio)
        align_line = tk.Frame(content, bg=PANEL_ALT)
        align_line.pack(fill="x", pady=(2, 7))
        ttk.Button(align_line, text="自动对齐全部片段", command=self._auto_align_join_audio).pack(side="left")
        self.editor_join_preview_button = ttk.Button(
            align_line, text="试听对齐结果", command=self._preview_join_alignment, state="disabled",
        )
        self.editor_join_preview_button.pack(side="left", padx=(8, 0))
        tk.Label(
            align_line, textvariable=self.editor_join_alignment_var, bg=PANEL_ALT, fg=GREEN,
            font=(UI_FONT, 8), anchor="w", justify="left", wraplength=220,
        ).pack(side="left", fill="x", expand=True, padx=(10, 0))

        manual_line = tk.Frame(content, bg=PANEL_ALT)
        manual_line.pack(fill="x", pady=(0, 7))
        tk.Label(
            manual_line, text="手动对齐", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 8, "bold"),
        ).pack(side="left")
        tk.Entry(
            manual_line, textvariable=self.editor_join_manual_time_var, bg=INPUT, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=PINK, font=(MONO_FONT, 9), width=14,
        ).pack(side="left", padx=(7, 4), ipady=5)
        ttk.Button(manual_line, text="应用时间", command=self._apply_manual_join_alignment).pack(side="left")
        tk.Label(manual_line, text="移动(秒)", bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 8)).pack(side="left", padx=(9, 3))
        tk.Entry(
            manual_line, textvariable=self.editor_join_nudge_var, bg=INPUT, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=PINK, font=(MONO_FONT, 9), width=7,
        ).pack(side="left", ipady=5)
        ttk.Button(
            manual_line, text="提前", command=lambda: self._nudge_manual_join_alignment(-1),
        ).pack(side="left", padx=(4, 2))
        ttk.Button(
            manual_line, text="延后", command=lambda: self._nudge_manual_join_alignment(1),
        ).pack(side="left")

        tk.Label(
            content, text="输出与加速", bg=PANEL_ALT, fg=PINK,
            font=(UI_FONT, 11, "bold"),
        ).pack(anchor="w", pady=(8, 5))
        export_grid = tk.Frame(content, bg=PANEL_ALT)
        export_grid.pack(fill="x", pady=(0, 5))
        for column in range(2):
            export_grid.columnconfigure(column, weight=1)
        tk.Label(export_grid, text="画面处理", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 9, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(export_grid, text="视频封装 / 编码", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 9, "bold")).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Combobox(
            export_grid, textvariable=self.editor_join_mode_var, state="readonly",
            values=["无损复制视频流（最快）", "重编码（沿用统一设置）"],
        ).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        join_video_output_combo = ttk.Combobox(
            export_grid, textvariable=self.editor_join_video_output_var, state="readonly",
            values=[
                "MOV / HEVC H.265（自动适配）", "MP4 / HEVC H.265（自动适配）",
                "MOV / H.264（自动适配）", "MP4 / H.264（自动适配）",
                "MKV / AV1（自动适配）", "MP4 / AV1（自动适配）",
            ],
        )
        join_video_output_combo.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(4, 0))
        join_video_output_combo.bind("<<ComboboxSelected>>", self._sync_join_output_format)
        tk.Label(export_grid, text="视频目标码率（留空使用 CQ）", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 9, "bold")).grid(row=2, column=0, columnspan=2, sticky="w", pady=(7, 0))
        tk.Entry(
            export_grid, textvariable=self.editor_join_bitrate_var, bg=INPUT, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=PINK, font=(MONO_FONT, 9),
        ).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0), ipady=7)

        shared = tk.Frame(content, bg="#21152c", highlightthickness=1, highlightbackground=BORDER, padx=10, pady=8)
        shared.pack(fill="x", pady=(4, 8))
        tk.Label(
            shared, textvariable=self.editor_join_shared_settings_var, bg="#21152c", fg=GREEN,
            font=(UI_FONT, 8), anchor="w", justify="left", wraplength=405,
        ).pack(fill="x")
        tk.Label(
            shared, text="编码、预设、位深、CQ、NVDEC、NVENC 分帧和并行数统一在“导出与 GPU”页设置。",
            bg="#21152c", fg=MUTED, font=(UI_FONT, 8), anchor="w", justify="left", wraplength=405,
        ).pack(fill="x", pady=(3, 0))

        audio_grid = tk.Frame(content, bg=PANEL_ALT)
        audio_grid.pack(fill="x", pady=(0, 7))
        audio_grid.columnconfigure(0, weight=2)
        audio_grid.columnconfigure(1, weight=1)
        audio_grid.columnconfigure(2, weight=1)
        tk.Label(audio_grid, text="主音频格式", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 9, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(audio_grid, text="采样率", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 9, "bold")).grid(row=0, column=1, sticky="w", padx=(8, 0))
        tk.Label(audio_grid, text="AAC 码率", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 9, "bold")).grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Combobox(
            audio_grid, textvariable=self.editor_join_audio_format_var, state="readonly",
            values=[
                "AAC（兼容 MP4）", "PCM 16-bit（MOV/MKV）", "PCM 24-bit（MOV/MKV）",
                "PCM 32-bit 整数（MOV/MKV）", "PCM 32-bit 浮点（MOV/MKV）",
            ],
        ).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Combobox(
            audio_grid, textvariable=self.editor_join_audio_sample_rate_var, state="normal",
            values=["保留原采样率", "44100", "48000", "88200", "96000", "176400", "192000"],
        ).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(4, 0))
        ttk.Combobox(
            audio_grid, textvariable=self.editor_join_audio_bitrate_var, state="normal",
            values=["48k", "64k", "80k", "96k", "128k", "160k", "192k", "256k", "320k", "384k", "512k"],
        ).grid(row=1, column=2, sticky="ew", padx=(8, 0), pady=(4, 0))

        for variable in (
            self.codec_var, self.preset_var, self.resolution_var, self.depth_var,
            self.quality_var, self.decode_var, self.split_var, self.parallel_var,
        ):
            variable.trace_add("write", lambda *_args: self._update_join_shared_settings_summary())
        self._update_join_shared_settings_summary()

        gap_grid = tk.Frame(content, bg=PANEL_ALT)
        gap_grid.pack(fill="x", pady=(0, 5))
        gap_grid.columnconfigure(0, weight=1)
        gap_grid.columnconfigure(1, weight=1)
        tk.Label(
            gap_grid, text="停机空窗处理", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 9, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            gap_grid, text="主音频开始前", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 9, "bold"),
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        gap_combo = ttk.Combobox(
            gap_grid, textvariable=self.editor_join_gap_var, state="readonly",
            values=["保留停机空窗（黑场，主音频连续）", "删除停机空窗（紧凑拼接）"],
        )
        gap_combo.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        gap_combo.bind("<<ComboboxSelected>>", lambda _event: self._draw_join_timeline())
        leading_combo = ttk.Combobox(
            gap_grid, textvariable=self.editor_join_leading_mode_var, state="readonly",
            values=[
                "裁掉开头画面（从主音频 00:00 开始）",
                "保留开头画面（主音频开始前静音）",
            ],
        )
        leading_combo.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(4, 0))
        leading_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_join_list())

        tk.Label(
            gap_grid, text="主音频结束后", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 9, "bold"),
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(7, 0))
        trailing_combo = ttk.Combobox(
            gap_grid, textvariable=self.editor_join_trailing_mode_var, state="readonly",
            values=[
                "裁掉结尾画面（到主音频结束）",
                "保留结尾画面（主音频结束后静音）",
            ],
        )
        trailing_combo.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        trailing_combo.bind("<<ComboboxSelected>>", lambda _event: self._draw_join_timeline())

        range_grid = tk.Frame(content, bg=PANEL_ALT)
        range_grid.pack(fill="x", pady=(3, 5))
        range_grid.columnconfigure(0, weight=1)
        range_grid.columnconfigure(1, weight=1)
        tk.Label(
            range_grid, text="成片开始（留空自动）", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 9, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            range_grid, text="成片结束（留空按上方）", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 9, "bold"),
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        range_start_entry = tk.Entry(
            range_grid, textvariable=self.editor_join_range_start_var, bg=INPUT, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=PINK, font=(MONO_FONT, 9),
        )
        range_start_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0), ipady=7)
        range_end_entry = tk.Entry(
            range_grid, textvariable=self.editor_join_range_end_var, bg=INPUT, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=PINK, font=(MONO_FONT, 9),
        )
        range_end_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(4, 0), ipady=7)
        for entry in (range_start_entry, range_end_entry):
            entry.bind("<FocusOut>", lambda _event: self._draw_join_timeline())
            entry.bind("<Return>", lambda _event: self._draw_join_timeline())
        tk.Label(
            content,
            text="手动时间使用最终成片时间轴 HH:MM:SS.mmm；00:00:00.000 从导出文件第一帧算起。填写后覆盖对应的自动边界。",
            bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 8), wraplength=410, justify="left",
        ).pack(anchor="w", pady=(0, 4))

        footer = tk.Frame(content, bg=PANEL_ALT)
        footer.pack(fill="x", pady=(8, 12))
        self._editor_path_field(footer, "输出文件", self.editor_join_output_var, self._choose_join_output)
        self.editor_join_export_button = ttk.Button(
            footer, text="对齐、拼接并导出", style="Accent.TButton", command=self._export_join,
        )
        self.editor_join_export_button.pack(fill="x", pady=(4, 0))

        def scroll_page(event) -> str:
            if isinstance(event.widget, tk.Listbox):
                return ""
            delta = -1 if getattr(event, "delta", 0) > 0 else 1
            canvas.yview_scroll(delta * 3, "units")
            return "break"

        def bind_scroll_tree(widget: tk.Widget) -> None:
            if not isinstance(widget, tk.Listbox):
                widget.bind("<MouseWheel>", scroll_page, add="+")
            for child in widget.winfo_children():
                bind_scroll_tree(child)

        bind_scroll_tree(content)

    def _build_split_panel(self, panel: tk.Frame) -> None:
        self._add_glass_shine(panel)
        tk.Label(panel, text="分离视频与音频", bg=PANEL_ALT, fg=TEXT, font=(UI_FONT, 15, "bold")).pack(anchor="w")
        tk.Label(
            panel, text="使用左侧已打开的视频。视频轨直接复制，画质、位深和色彩信息保持不变。",
            bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 10), wraplength=410, justify="left",
        ).pack(anchor="w", pady=(4, 18))
        tk.Label(panel, text="音频输出格式", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 10, "bold")).pack(anchor="w", pady=(0, 6))
        ttk.Combobox(
            panel, textvariable=self.editor_split_mode_var, state="readonly",
            values=["复制原音频（MKA）", "24-bit WAV（剪辑推荐）", "FLAC 无损（推荐）", "AAC 320k（体积小）"],
        ).pack(fill="x")
        tk.Label(
            panel, text="复制原音频完全不转码；WAV/FLAC 适合后续剪辑，AAC 适合快速交付。",
            bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 9), wraplength=410, justify="left",
        ).pack(anchor="w", pady=(7, 16))
        self._editor_path_field(panel, "输出目录", self.editor_split_dir_var, self._choose_split_directory)
        ttk.Button(
            panel, text="分离视频轨和音频轨", style="Accent.TButton", command=self._export_split_streams,
        ).pack(fill="x", pady=(18, 10))

    def _build_proxy_panel(self, panel: tk.Frame) -> None:
        self._add_glass_shine(panel)
        tk.Label(panel, text="批量 1080p 代理", bg=PANEL_ALT, fg=TEXT, font=(UI_FONT, 15, "bold")).pack(anchor="w")
        tk.Label(
            panel, text="一次提交多个原片，统一生成到独立代理文件夹供 DaVinci Resolve 使用。",
            bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 10), wraplength=410, justify="left",
        ).pack(anchor="w", pady=(4, 10))

        list_holder = tk.Frame(panel, bg=PANEL_ALT)
        list_holder.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(list_holder, orient="vertical")
        self.editor_proxy_listbox = tk.Listbox(
            list_holder, bg=INPUT, fg=TEXT, selectbackground=PURPLE,
            selectforeground="#ffffff", relief="flat", highlightthickness=1,
            highlightbackground=BORDER, font=(UI_FONT, 10), height=8,
            yscrollcommand=scrollbar.set, selectmode="extended",
        )
        scrollbar.configure(command=self.editor_proxy_listbox.yview)
        self.editor_proxy_listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        buttons = tk.Frame(panel, bg=PANEL_ALT)
        buttons.pack(fill="x", pady=(8, 10))
        ttk.Button(buttons, text="添加多个视频", command=self._add_proxy_files).pack(side="left")
        ttk.Button(buttons, text="加入当前", command=self._add_current_proxy_file).pack(side="left", padx=6)
        ttk.Button(buttons, text="删除", command=self._remove_proxy_files).pack(side="left")
        ttk.Button(buttons, text="清空", command=self._clear_proxy_files).pack(side="right")
        tk.Label(
            buttons, textvariable=self.editor_proxy_count_var, bg=PANEL_ALT, fg=PINK,
            font=(UI_FONT, 9, "bold"),
        ).pack(side="right", padx=(0, 10))

        specs = tk.Frame(panel, bg=PANEL, highlightthickness=1, highlightbackground=BORDER, padx=14, pady=10)
        specs.pack(fill="x", pady=(0, 10))
        self.proxy_specs_title_label = tk.Label(
            specs, text="显卡自动适配代理规格", bg=PANEL, fg=PINK, font=(UI_FONT, 10, "bold"),
        )
        self.proxy_specs_title_label.pack(anchor="w")
        tk.Label(
            specs,
            text="按硬件检测选择 NVENC / AMF / QSV，或 CPU 编码\n"
                 "1080p · 原帧率/时长/时间码 · AAC 320k · CQ 19 · P2",
            bg=PANEL, fg=TEXT, font=(UI_FONT, 9), justify="left",
        ).pack(anchor="w", pady=(6, 0))
        format_row = tk.Frame(panel, bg=PANEL_ALT)
        format_row.pack(fill="x", pady=6)
        tk.Label(format_row, text="代理格式", bg=PANEL_ALT, fg=TEXT).pack(side="left")
        ttk.Combobox(format_row, textvariable=self.editor_proxy_format_var,
                     values=proxy_format_labels(), state="readonly", width=44).pack(side="right")
        self._editor_path_field(panel, "代理输出目录", self.editor_proxy_dir_var, self._choose_proxy_directory)

        parallel = tk.Frame(panel, bg=PANEL_ALT)
        parallel.pack(fill="x", pady=(5, 0))
        tk.Label(
            parallel, text="并行任务", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 10, "bold"),
        ).pack(side="left")
        self.proxy_parallel_combo = ttk.Combobox(
            parallel, textvariable=self.editor_proxy_parallel_var, state="readonly", width=20,
            values=["同时 1 路（检测中）"],
        )
        self.proxy_parallel_combo.pack(side="right")
        ttk.Button(
            panel, text="批量生成 1080p 代理", style="Accent.TButton", command=self._create_1080p_proxy,
        ).pack(fill="x", pady=(14, 7))
        tk.Label(
            panel,
            text="DaVinci 标准命名：与原片主文件名相同，仅扩展名改为 .mp4；不同目录的同名原片会提前提示。",
            bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 9), wraplength=410, justify="left",
        ).pack(anchor="w", pady=(3, 0))

    def _editor_path_field(self, parent: tk.Widget, label: str, variable: tk.StringVar, command) -> None:
        holder = tk.Frame(parent, bg=PANEL_ALT)
        holder.pack(fill="x", pady=7)
        tk.Label(holder, text=label, bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 10, "bold")).pack(anchor="w")
        line = tk.Frame(holder, bg=PANEL_ALT)
        line.pack(fill="x", pady=(6, 0))
        tk.Entry(
            line, textvariable=variable, bg=INPUT, fg=TEXT, insertbackground=TEXT,
            relief="flat", highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=PINK, font=(UI_FONT, 10),
        ).pack(side="left", fill="x", expand=True, ipady=9)
        ttk.Button(line, text="浏览…", command=command).pack(side="left", padx=(8, 0))

    def _set_subtitle_source(self, path: str) -> None:
        source = Path(path)
        self.subtitle_input_var.set(str(source))
        self.subtitle_output_var.set(str(source.with_suffix(".srt")))
        self.subtitle_status_var.set("素材已选择；确认模型、语言和输出位置后开始识别。")

    def _choose_subtitle_input(self) -> None:
        path = filedialog.askopenfilename(
            title="选择需要识别字幕的视频或音频",
            filetypes=[
                ("视频与音频", "*.mp4 *.mov *.mkv *.mxf *.avi *.webm *.mts *.m2ts *.wav *.flac *.mp3 *.m4a *.aac *.ogg *.opus"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self._set_subtitle_source(path)

    def _use_export_source_for_subtitles(self) -> None:
        path = self.input_var.get().strip()
        if not path or not Path(path).is_file():
            messagebox.showerror(APP_NAME, "“导出与 GPU”页还没有选择有效素材。")
            return
        self._set_subtitle_source(path)

    def _use_editor_source_for_subtitles(self) -> None:
        path = self.editor_video_var.get().strip()
        if not path or not Path(path).is_file():
            messagebox.showerror(APP_NAME, "剪辑工具还没有打开有效视频。")
            return
        self._set_subtitle_source(path)

    def _choose_subtitle_output(self) -> None:
        source = Path(self.subtitle_input_var.get() or "subtitles")
        path = filedialog.asksaveasfilename(
            title="保存 SRT 字幕",
            initialfile=f"{source.stem}.srt",
            defaultextension=".srt",
            filetypes=[("SubRip 字幕", "*.srt")],
        )
        if path:
            self.subtitle_output_var.set(path)

    def _install_subtitle_dependencies(self) -> None:
        installer = app_directory() / "install_ai_subtitles.bat"
        if os.name != "nt" or not installer.is_file():
            messagebox.showerror(APP_NAME, "没有找到 install_ai_subtitles.bat。请重新解压完整软件包。")
            return
        try:
            os.startfile(str(installer))  # type: ignore[attr-defined]
            self.subtitle_status_var.set("安装窗口已打开；完成后请重启软件。")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法启动安装程序：{exc}")

    def _lyrics_library_root(self) -> Path:
        return find_library_root(app_directory(), getattr(sys, "_MEIPASS", None))

    def _refresh_lyrics_library_status(self) -> None:
        try:
            self.lyrics_library = LyricLibrary.load(self._lyrics_library_root())
            self.subtitle_lyrics_status_var.set(library_status_text(self.lyrics_library))
        except (OSError, ValueError, KeyError) as exc:
            self.lyrics_library = None
            self.subtitle_lyrics_status_var.set(f"歌词库不可用：{exc}")

    def _open_lyrics_library_folder(self) -> None:
        root = self._lyrics_library_root()
        if not (root / "manifest.json").is_file():
            messagebox.showerror(APP_NAME, "没有找到完整歌词库，请重新解压软件包。")
            return
        self._open_folder(str(root / "manifest.json"))

    def _open_subtitle_folder(self) -> None:
        output = self.subtitle_output_var.get().strip()
        if not output:
            messagebox.showerror(APP_NAME, "还没有选择 SRT 输出位置。")
            return
        folder = Path(output).resolve().parent
        if not folder.exists():
            messagebox.showerror(APP_NAME, "字幕输出文件夹尚不存在。")
            return
        self._open_folder(str(Path(output)))

    def _generate_subtitles(self) -> None:
        if self.working:
            messagebox.showwarning(APP_NAME, "已有任务正在运行，请等待完成或先取消。")
            return
        source = self.subtitle_input_var.get().strip()
        output = self.subtitle_output_var.get().strip()
        if not source or not Path(source).is_file():
            messagebox.showerror(APP_NAME, "请先选择有效的视频或音频文件。")
            return
        if not output:
            output = str(Path(source).with_suffix(".srt"))
            self.subtitle_output_var.set(output)
        if Path(output).suffix.lower() != ".srt":
            messagebox.showerror(APP_NAME, "字幕输出文件必须以 .srt 结尾。")
            return

        self.cancel_event.clear()
        self.subtitle_progress_var.set(0)
        self.progress_var.set(0)
        self.subtitle_status_var.set("正在准备 RTX AI 字幕引擎…")
        self.subtitle_preview.configure(state="normal")
        self.subtitle_preview.delete("1.0", "end")
        self.subtitle_preview.configure(state="disabled")
        self._set_working(True)
        model_cache = cache_dir('models/faster-whisper')
        model_choice = self.subtitle_model_var.get()
        language_choice = self.subtitle_language_var.get()
        initial_prompt = self.subtitle_prompt_var.get()
        vad_filter = bool(self.subtitle_vad_var.get())
        gpu_memory_mb = int(self.caps.gpu_memory_mb or 0)
        lyric_transform = None
        if bool(self.subtitle_lyrics_enabled_var.get()):
            if self.lyrics_library is None:
                self._refresh_lyrics_library_status()
            if self.lyrics_library is None:
                messagebox.showerror(APP_NAME, "歌词库不可用，请重新解压完整软件包或关闭歌词校正。")
                self._set_working(False)
                return
            lyric_transform = make_lyric_transform(
                self.lyrics_library, self.subtitle_lyrics_mode_var.get()
            )
            initial_prompt = self.lyrics_library.titles_prompt(initial_prompt)

        def worker() -> None:
            try:
                result = transcribe_to_srt(
                    source,
                    output,
                    model_choice=model_choice,
                    language_choice=language_choice,
                    initial_prompt=initial_prompt,
                    vad_filter=vad_filter,
                    gpu_memory_mb=gpu_memory_mb,
                    model_cache=model_cache,
                    cancel_event=self.cancel_event,
                    progress_callback=lambda percent, status: self.events.put(
                        ("subtitle_progress", (percent, status))
                    ),
                    segment_callback=lambda cue, index: self.events.put(
                        ("subtitle_segment", (cue, index))
                    ),
                    cue_transform=lyric_transform,
                )
                self.events.put(("subtitle_done", result))
            except SubtitleCancelled as exc:
                self.events.put(("subtitle_cancelled", str(exc)))
            except Exception as exc:
                self.events.put(("subtitle_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _choose_editor_video(self) -> None:
        path = filedialog.askopenfilename(title="选择剪辑视频", filetypes=[
            ("视频文件", "*.mp4 *.mov *.mkv *.mxf *.avi *.webm *.mts *.m2ts"),
            ("所有文件", "*.*"),
        ])
        if path:
            self._load_editor_video(path)

    def _use_export_source(self) -> None:
        path = self.input_var.get().strip()
        if not path or not Path(path).is_file():
            messagebox.showerror(APP_NAME, "“导出与 GPU”页还没有选择有效视频。")
            return
        self._load_editor_video(path)

    def _load_editor_video(self, path: str) -> None:
        self._invalidate_editor_player()
        generation = self.editor_load_generation
        self.editor_join_preview_context = None
        self.editor_video_var.set(path)
        source = Path(path)
        self.editor_output_var.set(str(source.with_name(f"{source.stem}_新音频{source.suffix}")))
        self.editor_status_var.set("正在启动 MPV 并读取视频…")
        self.update_idletasks()
        window_id = self.player_surface.winfo_id()
        mpv_path = self.mpv_path or find_binary("mpv", app_directory())
        self.mpv_path = mpv_path

        def worker() -> None:
            controller: MpvController | None = None
            try:
                if not mpv_path:
                    raise RuntimeError("未找到 mpv.exe。请把 Windows 版 mpv.exe 放入软件的 bin 文件夹。")
                if not self.caps.ffprobe_path:
                    raise RuntimeError("未找到 ffprobe.exe。")
                info = probe_media(self.caps.ffprobe_path, path)
                if generation != self.editor_load_generation:
                    return
                if controller is None or not controller.process or controller.process.poll() is not None:
                    controller = MpvController(mpv_path, window_id)
                    controller.start()
                playback_backend = controller.load(path)
                if generation != self.editor_load_generation or getattr(self, '_closing', False):
                    controller.stop()
                    return
                self.events.put(("editor_loaded", (generation, controller, info, playback_backend)))
            except Exception as exc:
                if controller is not None:
                    controller.stop()
                if generation == self.editor_load_generation:
                    self.events.put(("editor_error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _invalidate_editor_player(self) -> None:
        self.editor_load_generation += 1
        controller, self.mpv = self.mpv, None
        if controller is not None:
            threading.Thread(target=controller.stop, daemon=True).start()

    def _choose_editor_audio(self) -> None:
        path = filedialog.askopenfilename(title="选择替换音频", filetypes=[
            ("音频或视频文件", "*.wav *.flac *.mp3 *.m4a *.aac *.ogg *.opus *.mov *.mp4 *.mkv"),
            ("所有文件", "*.*"),
        ])
        if path:
            self.editor_audio_var.set(path)
            self.editor_join_alignments.clear()
            self.editor_join_alignment_audio = ""
            self.editor_join_audio_duration = 0.0
            self.editor_join_timeline_probe_key = None
            self.editor_join_alignment_var.set("主音频已更改，请重新自动对齐全部片段")
            self._refresh_join_list()
            self.editor_status_var.set("已选择连续主音频；可自动对齐全部相机片段。")

    def _choose_editor_output(self) -> None:
        source = Path(self.editor_video_var.get() or "output.mp4")
        path = filedialog.asksaveasfilename(
            title="保存替换音频后的视频",
            initialfile=f"{source.stem}_新音频{source.suffix or '.mp4'}",
            defaultextension=source.suffix or ".mp4",
            filetypes=[("MP4", "*.mp4"), ("MOV", "*.mov"), ("Matroska", "*.mkv")],
        )
        if path:
            self.editor_output_var.set(path)

    def _read_timecode(self, variable: tk.StringVar, label: str) -> float | None:
        try:
            seconds = parse_timecode(variable.get())
        except ValueError as exc:
            messagebox.showerror(APP_NAME, f"{label}无效：{exc}")
            return None
        if self.editor_duration > 0 and seconds > self.editor_duration + 0.001:
            messagebox.showerror(APP_NAME, f"{label}超过视频总时长 {format_time(self.editor_duration)}。")
            return None
        variable.set(format_time(seconds))
        return seconds

    def _seek_to_timecode(self, variable: tk.StringVar, label: str) -> None:
        seconds = self._read_timecode(variable, label)
        if seconds is None:
            return
        self.editor_seek_var.set(format_time(seconds))
        self._mpv_command_async(["seek", seconds, "absolute+exact"], error_prefix="时间码跳转失败")

    def _seek_editor_time(self) -> None:
        self._seek_to_timecode(self.editor_seek_var, "跳转时间")

    def _copy_current_time_to_seek(self) -> None:
        self.editor_seek_var.set(format_time(self.editor_current_time))

    def _set_trim_in(self) -> None:
        self.editor_trim_in_var.set(format_time(self.editor_current_time))

    def _set_trim_out(self) -> None:
        self.editor_trim_out_var.set(format_time(self.editor_current_time))

    def _preview_trim_in(self) -> None:
        self._seek_to_timecode(self.editor_trim_in_var, "入点")

    def _preview_trim_out(self) -> None:
        self._seek_to_timecode(self.editor_trim_out_var, "出点")

    def _choose_trim_output(self) -> None:
        source = Path(self.editor_video_var.get() or "video.mp4")
        path = filedialog.asksaveasfilename(
            title="保存裁切视频",
            initialfile=f"{source.stem}_裁切{source.suffix or '.mp4'}",
            defaultextension=source.suffix or ".mp4",
            filetypes=[("MP4", "*.mp4"), ("MOV", "*.mov"), ("Matroska", "*.mkv")],
        )
        if path:
            self.editor_trim_output_var.set(path)

    def _add_join_files(self) -> None:
        paths = filedialog.askopenfilenames(title="添加需要拼接的视频", filetypes=[
            ("视频文件", "*.mp4 *.mov *.mkv *.mxf *.avi *.webm *.mts *.m2ts"),
            ("所有文件", "*.*"),
        ])
        known = {os.path.normcase(str(Path(item).resolve())) for item in self.editor_join_files}
        for path in paths:
            key = os.path.normcase(str(Path(path).resolve()))
            if key not in known:
                self.editor_join_files.append(path)
                known.add(key)
        self._refresh_join_list()
        if any(path not in self.editor_join_alignments for path in self.editor_join_files):
            self.editor_join_alignment_var.set("列表已变化，请重新自动对齐全部片段")
        if self.editor_join_files and not self.editor_join_output_var.get().strip():
            first = Path(self.editor_join_files[0])
            suffix = self._join_output_suffix()
            self.editor_join_output_var.set(str(first.with_name(f"{first.stem}_拼接{suffix}")))

    def _refresh_join_list(self) -> None:
        self.editor_join_listbox.delete(0, "end")
        for index, path in enumerate(self.editor_join_files, start=1):
            result = self.editor_join_alignments.get(path)
            if result is None:
                alignment = "未对齐"
            else:
                master_start = max(0.0, -result.offset_seconds)
                leading_trim = max(0.0, result.offset_seconds)
                if leading_trim > 0.25 and self.editor_join_leading_mode_var.get().startswith("裁掉"):
                    alignment = f"裁开头 {format_time(leading_trim)} → 主音频 00:00:00.000"
                elif leading_trim > 0.25:
                    alignment = f"保留开头 {format_time(leading_trim)}（主音频前静音）"
                elif result.engine.startswith("手动"):
                    alignment = f"主音频 {format_time(master_start)}  手动设置"
                else:
                    alignment = f"主音频 {format_time(master_start)}  可信度 {result.confidence:.3f}"
            self.editor_join_listbox.insert("end", f"{index:02d}  {Path(path).name}  ·  {alignment}")
        self._update_join_preview_button()
        self._draw_join_timeline()
        self._request_join_timeline_probe()

    def _request_join_timeline_probe(self) -> None:
        """Probe clip/audio durations off the UI thread for the visual timeline."""
        if not hasattr(self, "editor_join_timeline_canvas"):
            return
        paths = tuple(self.editor_join_files)
        audio = self.editor_audio_var.get().strip()
        key = (paths, audio)
        missing = [path for path in paths if path not in self.editor_join_media_cache]
        need_audio = bool(audio) and self.editor_join_audio_duration <= 0
        if not missing and not need_audio:
            self._draw_join_timeline()
            return
        if key == self.editor_join_timeline_probe_key:
            return
        ffprobe = self.caps.ffprobe_path
        if not ffprobe:
            self.editor_join_timeline_hint_var.set("等待 ffprobe 检测完成后读取时间线时长…")
            return
        self.editor_join_timeline_probe_key = key
        self.editor_join_timeline_hint_var.set("正在读取素材时长并生成工程时间线…")

        def worker() -> None:
            media_items: list[tuple[str, MediaInfo]] = []
            audio_duration = self.editor_join_audio_duration
            failures: list[str] = []
            for path in missing:
                try:
                    media_items.append((path, probe_media(ffprobe, path)))
                except Exception as exc:
                    failures.append(f"{Path(path).name}: {exc}")
            if need_audio:
                try:
                    audio_duration = probe_media_duration(ffprobe, audio)
                except Exception as exc:
                    failures.append(f"{Path(audio).name}: {exc}")
            self.events.put((
                "join_timeline_media",
                (key, media_items, audio_duration, failures),
            ))

        threading.Thread(target=worker, daemon=True).start()

    def _load_selected_join_alignment(self, _event=None) -> None:
        if not hasattr(self, "editor_join_listbox"):
            return
        selected = list(self.editor_join_listbox.curselection())
        if not selected or selected[0] >= len(self.editor_join_files):
            return
        result = self.editor_join_alignments.get(self.editor_join_files[selected[0]])
        if result is not None:
            self.editor_join_manual_time_var.set(format_signed_time(-result.offset_seconds))
        if self.editor_join_timeline_drag is None:
            self._draw_join_timeline()

    def _set_manual_join_alignment(self, master_start: float) -> None:
        selected = list(self.editor_join_listbox.curselection())
        if len(selected) != 1:
            messagebox.showinfo(APP_NAME, "请在列表中只选择一个视频片段再手动对齐。")
            return
        index = selected[0]
        if index >= len(self.editor_join_files):
            return
        audio = self.editor_audio_var.get().strip()
        if not audio or not Path(audio).is_file():
            messagebox.showerror(APP_NAME, "请先选择有效的连续主音频。")
            return
        path = self.editor_join_files[index]
        previous = self.editor_join_alignments.get(path)
        envelope_rate = previous.envelope_rate if previous else 50
        self.editor_join_alignments[path] = AlignmentResult(
            offset_seconds=-master_start, confidence=1.0,
            envelope_rate=envelope_rate, engine="手动设置",
        )
        self.editor_join_alignment_audio = audio
        self.editor_join_manual_time_var.set(format_signed_time(master_start))
        aligned = sum(path in self.editor_join_alignments for path in self.editor_join_files)
        self.editor_join_alignment_var.set(
            f"第 {index + 1} 段已手动设为 {format_signed_time(master_start)}；已对齐 {aligned}/{len(self.editor_join_files)} 段"
        )
        self._refresh_join_list()
        self.editor_join_listbox.selection_set(index)
        self.editor_join_listbox.see(index)
        self.editor_status_var.set("手动对齐已应用；可点击“试听对齐结果”检查。")

    def _apply_manual_join_alignment(self) -> None:
        try:
            master_start = parse_signed_timecode(self.editor_join_manual_time_var.get())
        except ValueError as exc:
            messagebox.showerror(APP_NAME, f"手动对齐时间无效：{exc}")
            return
        self._set_manual_join_alignment(master_start)

    def _nudge_manual_join_alignment(self, direction: int) -> None:
        selected = list(self.editor_join_listbox.curselection())
        if len(selected) != 1 or selected[0] >= len(self.editor_join_files):
            messagebox.showinfo(APP_NAME, "请在列表中只选择一个视频片段再微调。")
            return
        try:
            amount = float(self.editor_join_nudge_var.get().strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "移动量必须是正数，例如 0.040、0.100 或 1.000 秒。")
            return
        path = self.editor_join_files[selected[0]]
        result = self.editor_join_alignments.get(path)
        if result is not None:
            current = -result.offset_seconds
        else:
            try:
                current = parse_signed_timecode(self.editor_join_manual_time_var.get())
            except ValueError as exc:
                messagebox.showerror(APP_NAME, f"手动对齐时间无效：{exc}")
                return
        self._set_manual_join_alignment(current + direction * amount)

    def _update_join_preview_button(self) -> None:
        if not hasattr(self, "editor_join_preview_button"):
            return
        audio = self.editor_audio_var.get().strip()
        ready = bool(
            not self.working
            and self.editor_join_files
            and audio
            and self.editor_join_alignment_audio == audio
            and any(path in self.editor_join_alignments for path in self.editor_join_files)
        )
        self.editor_join_preview_button.configure(state="normal" if ready else "disabled")

    def _remove_join_files(self) -> None:
        selected = list(self.editor_join_listbox.curselection())
        for index in reversed(selected):
            path = self.editor_join_files.pop(index)
            self.editor_join_alignments.pop(path, None)
        self._refresh_join_list()

    def _move_join_file(self, direction: int) -> None:
        selected = list(self.editor_join_listbox.curselection())
        if len(selected) != 1:
            messagebox.showinfo(APP_NAME, "请只选择一个视频再调整顺序。")
            return
        index = selected[0]
        target = index + direction
        if target < 0 or target >= len(self.editor_join_files):
            return
        item = self.editor_join_files.pop(index)
        self.editor_join_files.insert(target, item)
        self._refresh_join_list()
        self.editor_join_listbox.selection_set(target)
        self.editor_join_listbox.see(target)

    def _clear_join_files(self) -> None:
        self.editor_join_files.clear()
        self.editor_join_alignments.clear()
        self.editor_join_alignment_audio = ""
        self.editor_join_alignment_var.set("未对齐：选择连续主音频后可自动分析全部片段")
        self._refresh_join_list()

    def _choose_join_output(self) -> None:
        suffix = self._join_output_suffix()
        first = Path(self.editor_join_files[0]) if self.editor_join_files else Path(f"拼接输出{suffix}")
        file_type = {".mov": "MOV", ".mp4": "MP4", ".mkv": "Matroska"}[suffix]
        path = filedialog.asksaveasfilename(
            title="保存拼接视频", initialfile=f"{first.stem}_拼接{suffix}",
            defaultextension=suffix, filetypes=[(file_type, f"*{suffix}")],
        )
        if path:
            self.editor_join_output_var.set(str(Path(path).with_suffix(suffix)))

    def _update_join_shared_settings_summary(self) -> None:
        if not hasattr(self, "editor_join_shared_settings_var"):
            return
        self.editor_join_shared_settings_var.set(
            f"统一设置：{self.codec_var.get()} · {self.preset_var.get()} · "
            f"{self.resolution_var.get()} · {self.depth_var.get()} · CQ {self.quality_var.get()} · "
            f"{self.decode_var.get()} · 并行 {self.parallel_var.get()} 路"
        )

    def _join_output_suffix(self) -> str:
        text = self.editor_join_video_output_var.get()
        return ".mov" if text.startswith("MOV") else (".mkv" if text.startswith("MKV") else ".mp4")

    def _join_video_output_profile(self) -> tuple[str, str]:
        text = self.editor_join_video_output_var.get()
        suffix = self._join_output_suffix()
        family = "h264" if "H.264" in text else "av1" if "AV1" in text else "hevc"
        codec = preferred_codec(self.caps, family)
        actual_family = 'h264' if codec == 'libx264' else 'hevc' if codec == 'libx265' else codec.split('_',1)[0]
        if actual_family != family:
            raise ValueError(f'本机未检测到可用的 {family.upper()} 编码器，请在视频封装 / 编码中选择其他格式。')
        return suffix, codec

    def _sync_join_output_format(self, _event=None) -> None:
        suffix = self._join_output_suffix()
        current = self.editor_join_output_var.get().strip()
        if current:
            self.editor_join_output_var.set(str(Path(current).with_suffix(suffix)))

    def _choose_split_directory(self) -> None:
        path = filedialog.askdirectory(title="选择音视频分离输出目录")
        if path:
            self.editor_split_dir_var.set(path)

    def _choose_proxy_directory(self) -> None:
        path = filedialog.askdirectory(title="选择 ProxyMedia 代理输出目录")
        if path:
            self.editor_proxy_dir_var.set(path)

    def _add_proxy_paths(self, paths) -> None:
        known = {os.path.normcase(str(Path(item).resolve())) for item in self.editor_proxy_files}
        first_new: str | None = None
        for path in paths:
            candidate = str(path)
            key = os.path.normcase(str(Path(candidate).resolve()))
            if key not in known:
                self.editor_proxy_files.append(candidate)
                known.add(key)
                first_new = first_new or candidate
        self._refresh_proxy_list()
        if first_new and not self.editor_proxy_dir_var.get().strip():
            self.editor_proxy_dir_var.set(str(cache_dir('proxies')))

    def _add_proxy_files(self) -> None:
        paths = filedialog.askopenfilenames(title="批量添加代理原片", filetypes=[
            ("视频文件", "*.mp4 *.mov *.mkv *.mxf *.avi *.webm *.mts *.m2ts"),
            ("所有文件", "*.*"),
        ])
        self._add_proxy_paths(paths)

    def _add_current_proxy_file(self) -> None:
        source = self.editor_video_var.get().strip()
        if not source or not Path(source).is_file():
            messagebox.showerror(APP_NAME, "请先在左侧播放器打开一个视频。")
            return
        self._add_proxy_paths([source])

    def _refresh_proxy_list(self) -> None:
        self.editor_proxy_listbox.delete(0, "end")
        for index, path in enumerate(self.editor_proxy_files, start=1):
            self.editor_proxy_listbox.insert("end", f"{index:02d}  {Path(path).name}")
        self.editor_proxy_count_var.set(f"队列 {len(self.editor_proxy_files)} 个")

    def _remove_proxy_files(self) -> None:
        selected = list(self.editor_proxy_listbox.curselection())
        for index in reversed(selected):
            del self.editor_proxy_files[index]
        self._refresh_proxy_list()

    def _clear_proxy_files(self) -> None:
        self.editor_proxy_files.clear()
        self._refresh_proxy_list()

    def _proxy_parallel_count(self) -> int:
        text = self.editor_proxy_parallel_var.get()
        return 3 if "3" in text else 1 if "1" in text else 2

    def _run_editor_process(self, command: list[str], progress_callback=None) -> tuple[int, str]:
        process: subprocess.Popen | None = None
        stderr_thread: threading.Thread | None = None
        try:
            if self.cancel_event.is_set():
                return 1, "任务已取消"
            self.events.put(("log", "剪辑导出命令：" + quote_command(command)))
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
            self._register_process(process)
            if progress_callback is None:
                stdout_text, stderr_text = process.communicate()
                output_text = "\n".join(part for part in (stdout_text, stderr_text) if part)
            else:
                output_lines: list[str] = []
                error_lines: list[str] = []
                progress: dict[str, str] = {}
                assert process.stdout is not None

                def drain_stderr() -> None:
                    if process is None or process.stderr is None:
                        return
                    for error_line in process.stderr:
                        stripped = error_line.rstrip()
                        if stripped:
                            error_lines.append(stripped)
                            del error_lines[:-100]

                stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
                stderr_thread.start()
                # Make the shared footer leave "就绪" immediately, even before
                # FFmpeg has processed its first frame.
                progress_callback({
                    "progress": "continue", "out_time_us": "0",
                    "fps": "0", "speed": "0x",
                })
                for line in process.stdout:
                    if self.cancel_event.is_set():
                        self._terminate(process)
                        break
                    parsed = parse_progress_line(line)
                    if parsed:
                        key, value = parsed
                        progress[key] = value
                        if key == "progress":
                            progress_callback(progress.copy())
                            progress.clear()
                    else:
                        output_lines.append(line.rstrip())
                        output_lines = output_lines[-100:]
                process.wait()
                stderr_thread.join(timeout=2.0)
                output_text = "\n".join(output_lines + error_lines)
            return int(process.returncode or 0), output_text
        except Exception as exc:
            return 1, str(exc)
        finally:
            if process is not None:
                # Progress callbacks or pipe reads may fail before the normal
                # wait path. Reap the child before removing its registration or
                # allowing the multi-camera renderer to clean temporary files.
                try:
                    if process.poll() is None:
                        self._terminate(process)
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if stderr_thread is not None:
                    stderr_thread.join(timeout=2.0)
                self._unregister_process(process)
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass

    def _begin_shared_progress(self, label: str) -> None:
        """Reset the single footer progress bar for any export workflow."""
        self.progress_var.set(0)
        self.progress_text_var.set(label)
        self.update_idletasks()

    def _editor_progress_reporter(
        self,
        duration: float,
        label: str,
        *,
        start_percent: float = 0.0,
        span_percent: float = 100.0,
        source_fps: float = 0.0,
    ):
        total = max(0.0, float(duration or 0.0))
        recent_rate = RollingProgressRate()

        def report(progress: dict[str, str]) -> None:
            if total > 0:
                task_percent = min(99.9, max(0.0, progress_seconds(progress) / total * 100.0))
            else:
                task_percent = 100.0 if progress.get("progress") == "end" else 0.0
            overall = start_percent + task_percent * span_percent / 100.0
            recent_fps, recent_speed = recent_rate.update(progress, source_fps)
            self.events.put((
                "editor_export_progress",
                (
                    overall, label, task_percent,
                    f"{recent_fps:.3f}", f"{recent_speed:.3f}x",
                ),
            ))

        return report

    def _export_trim(self) -> None:
        if self.working:
            return
        source = self.editor_video_var.get().strip()
        output = self.editor_trim_output_var.get().strip()
        start = self._read_timecode(self.editor_trim_in_var, "入点")
        end = self._read_timecode(self.editor_trim_out_var, "出点")
        if start is None or end is None:
            return
        if end <= start:
            messagebox.showerror(APP_NAME, "出点必须晚于入点。")
            return
        if not source or not Path(source).is_file() or not output:
            messagebox.showerror(APP_NAME, "请先打开视频并选择裁切输出文件。")
            return
        if Path(source).resolve() == Path(output).resolve():
            messagebox.showerror(APP_NAME, "裁切输出不能覆盖原视频。")
            return
        if not self.caps.ffmpeg_path:
            messagebox.showerror(APP_NAME, "未找到 ffmpeg.exe。")
            return
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        precise = self.editor_trim_mode_var.get().startswith("逐帧精准")
        if precise:
            media = self.editor_media
            if media is None:
                messagebox.showerror(APP_NAME, "视频信息尚未读取完成，请稍等后重试。")
                return
            settings = self._settings()
            settings.resolution = "original"
            settings.audio = "aac"
            settings.audio_bitrate = "320k"
            settings.decode_mode = "cuda" if settings.codec.endswith('_nvenc') and self.caps.cuda_decode else "cpu"
            settings.split_encode = "auto"
            try:
                settings.video_bitrate = normalize_video_bitrate(self.editor_trim_bitrate_var.get())
            except ValueError as exc:
                messagebox.showerror(APP_NAME, str(exc))
                return
            command = build_encode_command(
                self.caps.ffmpeg_path, source, output, settings, media, self.caps,
                start=start, duration=end - start,
            )
            operation = "逐帧精准裁切"
        else:
            command = build_trim_copy_command(self.caps.ffmpeg_path, source, output, start, end)
            operation = "无损极速裁切"
        self.cancel_event.clear()
        self._set_working(True)
        self._begin_shared_progress(f"{operation} 0.0%")
        self.editor_status_var.set(f"正在执行{operation}：{format_time(start)} → {format_time(end)}")
        report_progress = self._editor_progress_reporter(
            end - start, operation,
            source_fps=float(self.editor_media.fps if self.editor_media else 0.0),
        )

        def worker() -> None:
            code, details = self._run_editor_process(command, report_progress)
            if self.cancel_event.is_set():
                self.events.put(("cancelled", None))
            elif code or not Path(output).is_file() or Path(output).stat().st_size <= 0:
                self.events.put(("editor_job_error", f"{operation}失败：\n{details[-2600:]}"))
            else:
                self.events.put(("editor_job_done", (operation, [output])))
        threading.Thread(target=worker, daemon=True).start()

    def _auto_align_join_audio(self) -> None:
        if self.working:
            return
        paths = list(self.editor_join_files)
        audio = self.editor_audio_var.get().strip()
        if len(paths) < 2:
            messagebox.showerror(APP_NAME, "请先添加至少两个相机视频片段。")
            return
        if any(not Path(path).is_file() for path in paths):
            messagebox.showerror(APP_NAME, "列表中存在已经移动或删除的视频。")
            return
        if not audio or not Path(audio).is_file():
            messagebox.showerror(APP_NAME, "请选择贯穿整场的连续主音频。")
            return
        if not self.caps.ffmpeg_path:
            messagebox.showerror(APP_NAME, "未找到 ffmpeg.exe。")
            return
        self.cancel_event.clear()
        self._set_working(True)
        self.editor_join_alignment_var.set(f"正在把 {len(paths)} 段视频分别对齐主音频…")
        self._begin_shared_progress("正在准备中段波形同步…")
        self.editor_status_var.set("正在截取各视频中段波形，并使用原版互相关对齐主录音…")

        def worker() -> None:
            try:
                results = align_audio_segments(
                    self.caps.ffmpeg_path or "ffmpeg", paths, audio,
                    max_offset_seconds=0.0,
                    cancel_event=self.cancel_event,
                    progress=lambda percent, phase: self.events.put(("audio_sync_progress", (percent, phase))),
                )
                self.events.put(("join_alignment_done", (audio, list(zip(paths, results)))))
            except ExportCancelled:
                self.events.put(("cancelled", None))
            except Exception as exc:
                self.events.put(("editor_job_error", "全部片段自动对齐失败：\n" + str(exc)))
        threading.Thread(target=worker, daemon=False).start()

    def _join_manual_output_range(self) -> tuple[float | None, float | None]:
        start_text = self.editor_join_range_start_var.get().strip()
        end_text = self.editor_join_range_end_var.get().strip()
        start = parse_timecode(start_text) if start_text else None
        end = parse_timecode(end_text) if end_text else None
        if start is not None and end is not None and end <= start:
            raise ValueError("成片结束时间必须晚于开始时间。")
        return start, end

    def _preview_join_alignment(self, master_seek: float | None = None) -> None:
        if self.working:
            return
        paths = list(self.editor_join_files)
        audio = self.editor_audio_var.get().strip()
        if not paths:
            messagebox.showerror(APP_NAME, "请先添加相机视频片段。")
            return
        if not audio or not Path(audio).is_file():
            messagebox.showerror(APP_NAME, "请选择有效的连续主音频。")
            return
        if self.editor_join_alignment_audio != audio:
            messagebox.showerror(APP_NAME, "主音频已变化，请重新自动对齐或手动设置片段时间。")
            return
        selected = list(self.editor_join_listbox.curselection())
        if selected and selected[0] < len(paths) and paths[selected[0]] in self.editor_join_alignments:
            index = selected[0]
        else:
            index = next((i for i, path in enumerate(paths) if path in self.editor_join_alignments), -1)
        if index < 0:
            messagebox.showerror(APP_NAME, "所选片段尚未对齐，请先自动对齐或填写手动时间。")
            return
        if index >= len(paths):
            index = 0
        self.editor_join_listbox.selection_clear(0, "end")
        self.editor_join_listbox.selection_set(index)
        self.editor_join_listbox.see(index)
        video = paths[index]
        result = self.editor_join_alignments[video]
        if not Path(video).is_file():
            messagebox.showerror(APP_NAME, "所选视频已移动或删除。")
            return
        if not self.caps.ffprobe_path:
            messagebox.showerror(APP_NAME, "未找到 ffprobe.exe。")
            return
        mpv_path = self.mpv_path or find_binary("mpv", app_directory())
        self.mpv_path = mpv_path
        if not mpv_path:
            messagebox.showerror(APP_NAME, "未找到 mpv.exe。请把 Windows 版 mpv.exe 放入软件的 bin 文件夹。")
            return
        try:
            custom_start, custom_end = self._join_manual_output_range()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        if any(path not in self.editor_join_alignments for path in paths):
            messagebox.showerror(APP_NAME, "完整范围试听需要先对齐全部片段；请点击“自动对齐全部片段”。")
            return

        self.editor_video_var.set(video)
        source = Path(video)
        self.editor_output_var.set(str(source.with_name(f"{source.stem}_新音频{source.suffix}")))
        trim_before_audio = self.editor_join_leading_mode_var.get().startswith("裁掉")
        keep_before_audio = self.editor_join_leading_mode_var.get().startswith("保留")
        trim_after_audio = self.editor_join_trailing_mode_var.get().startswith("裁掉")
        preserve_gap = self.editor_join_gap_var.get().startswith("保留")
        self.editor_status_var.set(f"正在计算成片范围并加载第 {index + 1} 段试听…")
        self.update_idletasks()
        window_id = self.player_surface.winfo_id()

        def worker() -> None:
            controller: MpvController | None = self.mpv
            try:
                media = [probe_media(self.caps.ffprobe_path or "ffprobe", path) for path in paths]
                info = media[index]
                audio_duration = probe_media_duration(self.caps.ffprobe_path or "ffprobe", audio)
                all_results = [self.editor_join_alignments[path] for path in paths]
                durations = [item.duration for item in media]
                output_start, output_end, _full_duration = resolve_join_output_range(
                    all_results, durations,
                    trim_before_audio=trim_before_audio,
                    keep_before_audio=keep_before_audio,
                    preserve_gaps=preserve_gap,
                    master_audio_duration=audio_duration,
                    trim_after_audio=trim_after_audio,
                    custom_start=custom_start,
                    custom_end=custom_end,
                )
                video_trims, intervals = aligned_clip_timeline(
                    all_results, durations,
                    trim_before_audio=trim_before_audio,
                    keep_before_audio=keep_before_audio,
                )
                if preserve_gap:
                    _starts, _gaps, audio_origin, _duration = gap_preserving_timeline(
                        all_results, durations,
                        trim_before_audio=trim_before_audio,
                        keep_before_audio=keep_before_audio,
                    )
                    clip_output_start = intervals[index][0] - audio_origin
                else:
                    clip_output_start = sum(duration for _start, duration in intervals[:index])
                clip_output_end = clip_output_start + intervals[index][1]
                preview_output_start = max(output_start, clip_output_start)
                preview_output_end = min(output_end, clip_output_end)
                if preview_output_end <= preview_output_start + 0.0005:
                    raise RuntimeError("所选片段不在当前手动成片范围内，请选择范围内的片段试听。")
                video_trim = video_trims[index] + (preview_output_start - clip_output_start)
                video_end = video_trims[index] + (preview_output_end - clip_output_start)
                if master_seek is not None:
                    requested_source_time = float(master_seek) + result.offset_seconds
                    video_trim = min(max(video_trim, requested_source_time), max(video_trim, video_end - 0.001))
                    preview_output_start = clip_output_start + (video_trim - video_trims[index])
                master_start = -result.offset_seconds + video_trim
                leading_silence = max(0.0, result.offset_seconds - video_trim)
                if controller is None or not controller.process or controller.process.poll() is not None:
                    controller = MpvController(mpv_path, window_id)
                    controller.start()
                playback_backend = controller.load_aligned_preview(
                    video, audio, result.offset_seconds,
                    start_seconds=video_trim, end_seconds=video_end,
                )
                self.events.put(("editor_loaded", (controller, info, playback_backend)))
                self.events.put((
                    "join_preview_started",
                    (
                        index + 1, Path(video).name, master_start,
                        result.confidence, video_trim, leading_silence,
                        preview_output_start, preview_output_end,
                        video, result.offset_seconds,
                    ),
                ))
            except Exception as exc:
                self.events.put(("editor_error", f"试听对齐结果失败：{exc}"))
        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _bitrate_bits_per_second(value: str) -> float:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmMgG]?)", str(value or "").strip())
        if not match:
            return 0.0
        multiplier = {"": 1.0, "k": 1_000.0, "m": 1_000_000.0, "g": 1_000_000_000.0}
        return float(match.group(1)) * multiplier[match.group(2).lower()]

    def _parallel_join_has_space(
        self, output: str, duration: float, video_bitrate: str,
    ) -> tuple[bool, str]:
        """Reserve room for encoded chunks and the final copied video."""
        temporary_root = cache_dir('exports')
        bits_per_second = self._bitrate_bits_per_second(video_bitrate)
        if bits_per_second <= 0 or duration <= 0:
            return True, f"CQ 模式无法预估临时空间；分段缓存使用 {temporary_root}"
        video_bytes = bits_per_second * duration / 8.0
        required = video_bytes * 2.15 + 2 * 1024 ** 3
        free = float(shutil.disk_usage(temporary_root).free)
        output_free = float(shutil.disk_usage(Path(output).parent).free)
        message = (
            f"双路临时空间预计需要 {required / 1024 ** 3:.1f} GB；"
            f"缓存盘当前可用 {free / 1024 ** 3:.1f} GB；"
            f"成片磁盘可用 {output_free / 1024 ** 3:.1f} GB"
        )
        return free >= required and output_free >= video_bytes*1.1, message

    def _run_parallel_join_export(
        self,
        paths: list[str],
        audio: str,
        media: list[MediaInfo],
        output: str,
        settings: ExportSettings,
        plan: JoinExportPlan,
        workers: int,
        action: str,
        *,
        audio_bitrate: str,
        audio_codec: str,
        audio_sample_rate: int | None,
    ) -> tuple[int, str]:
        """Run independent video jobs, then stream-copy them with master audio."""
        chunks = partition_join_video_spans(plan.spans, workers, plan.fps)
        workers = min(max(1, workers), len(chunks))
        if workers <= 1 or len(chunks) <= 1:
            raise RuntimeError("并行时间轴没有生成足够的独立分段。")
        durations = [sum(span.frames for span in chunk) / plan.fps for chunk in chunks]
        self.events.put(("editor_join_parallel_init", (durations, workers, plan.fps)))

        with tempfile.TemporaryDirectory(
            prefix=".okk_parallel_", dir=cache_dir('exports'),
        ) as folder:
            temp_dir = Path(folder)
            chunk_paths = [temp_dir / f"video_{index:03d}.mov" for index in range(len(chunks))]
            self.events.put((
                "log",
                f"剪辑双路模式：{workers} 个并发 NVDEC/NVENC 会话，"
                f"{len(chunks)} 个连续视频分段；临时目录 {temp_dir}",
            ))

            def encode_chunk(index: int) -> None:
                duration = durations[index]
                command = build_join_video_chunk_command(
                    self.caps.ffmpeg_path or "ffmpeg",
                    paths, chunks[index], media, str(chunk_paths[index]), settings, self.caps,
                )
                self.events.put((
                    "log",
                    f"双路分段 {index + 1}/{len(chunks)}："
                    f"{sum(span.frames for span in chunks[index])} 帧，{duration:.3f} 秒",
                ))
                recent_rate = RollingProgressRate()

                def report(progress: dict[str, str]) -> None:
                    ended = progress.get("progress") == "end"
                    seconds = duration if ended else min(duration, progress_seconds(progress))
                    recent_fps, _recent_speed = recent_rate.update(progress, plan.fps)
                    self.events.put((
                        "editor_join_parallel_progress",
                        (index, seconds, duration, recent_fps, not ended, workers, len(chunks)),
                    ))

                code, details = self._run_editor_process(command, report)
                if self.cancel_event.is_set():
                    raise ExportCancelled("剪辑导出已取消")
                if code or not chunk_paths[index].is_file() or chunk_paths[index].stat().st_size <= 0:
                    raise RuntimeError(
                        f"双路分段 {index + 1}/{len(chunks)} 编码失败：\n{details[-2600:]}"
                    )
                self.events.put((
                    "editor_join_parallel_progress",
                    (index, duration, duration, 0.0, False, workers, len(chunks)),
                ))

            failure: Exception | None = None
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="join_nvdec") as pool:
                futures = [pool.submit(encode_chunk, index) for index in range(len(chunks))]
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as exc:
                        failure = exc
                        for pending in futures:
                            pending.cancel()
                        with self.process_lock:
                            running = list(self.processes)
                        for process in running:
                            self._terminate(process)
                        break
            if failure is not None:
                raise failure

            if self.cancel_event.is_set():
                raise ExportCancelled("剪辑导出已取消")
            manifest = temp_dir / "video.ffconcat"
            write_concat_manifest((str(path) for path in chunk_paths), manifest)
            completed = temp_dir / ('completed' + Path(output).suffix.lower())
            mux_command = build_join_mux_command(
                self.caps.ffmpeg_path or "ffmpeg",
                str(manifest), audio, plan.audio_intervals, plan.duration,
                str(completed), media[0],
                audio_bitrate=audio_bitrate,
                audio_codec=audio_codec,
                audio_sample_rate=audio_sample_rate,
            )
            self.events.put((
                "log",
                "视频分段已完成；按原帧顺序复制码流，并一次写入连续主音频：\n"
                + quote_command(mux_command),
            ))
            self.events.put(("editor_status", f"正在{action}：视频已编码，正在无损合并并写入主音频…"))
            report_mux = self._editor_progress_reporter(
                plan.duration, "无损合并并写入主音频",
                start_percent=95.0, span_percent=5.0, source_fps=plan.fps,
            )
            code, details = self._run_editor_process(mux_command, report_mux)
            if self.cancel_event.is_set():
                raise ExportCancelled('剪辑导出已取消')
            if not code:
                publish_completed(completed, output, cancel_event=self.cancel_event,
                    progress=lambda copied,total:self.events.put(('editor_status',f'写入完成成片 {copied/max(1,total)*100:.1f}%')))
            return code, details

    def _export_join(self) -> None:
        if self.working:
            return
        paths = list(self.editor_join_files)
        output_suffix = self._join_output_suffix()
        join_video_codec = None
        output = self.editor_join_output_var.get().strip()
        if output and Path(output).suffix.lower() != output_suffix:
            output = str(Path(output).with_suffix(output_suffix))
            self.editor_join_output_var.set(output)
        if len(paths) < 2:
            messagebox.showerror(APP_NAME, "拼接至少需要两个视频。")
            return
        if not output:
            messagebox.showerror(APP_NAME, "请选择拼接输出文件。")
            return
        if any(not Path(path).is_file() for path in paths):
            messagebox.showerror(APP_NAME, "列表中存在已经移动或删除的视频。")
            return
        if Path(output).resolve() in {Path(path).resolve() for path in paths}:
            messagebox.showerror(APP_NAME, "拼接输出不能覆盖输入视频。")
            return
        if not self.caps.ffmpeg_path or not self.caps.ffprobe_path:
            messagebox.showerror(APP_NAME, "未找到 ffmpeg.exe 或 ffprobe.exe。")
            return
        audio = self.editor_audio_var.get().strip()
        if audio and not Path(audio).is_file():
            messagebox.showerror(APP_NAME, "连续主音频文件不存在。")
            return
        try:
            video_bitrate = normalize_video_bitrate(self.editor_join_bitrate_var.get())
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        join_mode_text = self.editor_join_mode_var.get()
        lossless = join_mode_text.startswith("无损")
        if not lossless:
            try:
                _output_suffix, join_video_codec = self._join_video_output_profile()
            except ValueError as exc:
                messagebox.showerror(APP_NAME, str(exc))
                return
        preserve_gap = self.editor_join_gap_var.get().startswith("保留")
        leading_mode_text = self.editor_join_leading_mode_var.get()
        trim_before_audio = leading_mode_text.startswith("裁掉")
        keep_before_audio = leading_mode_text.startswith("保留")
        trim_after_audio = bool(audio) and self.editor_join_trailing_mode_var.get().startswith("裁掉")
        try:
            custom_start, custom_end = self._join_manual_output_range()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        if (custom_start is not None or custom_end is not None) and not audio:
            messagebox.showerror(APP_NAME, "手动成片开始/结束时间需要先选择连续主音频。")
            return
        if preserve_gap and not audio:
            messagebox.showerror(APP_NAME, "保留停机空窗需要选择连续主音频，以确定黑场时长。")
            return
        if preserve_gap and lossless:
            messagebox.showerror(
                APP_NAME,
                "黑场属于新增视频帧，不能用纯无损复制生成。请将画面处理改为“重编码”。",
            )
            return
        if lossless and (trim_after_audio or custom_start is not None or custom_end is not None):
            messagebox.showerror(
                APP_NAME,
                "按主音频结尾或手动成片范围裁切需要逐帧处理。请将画面处理改为“重编码”。",
            )
            return
        audio_format_text = self.editor_join_audio_format_var.get()
        if audio_format_text.startswith("PCM 16"):
            audio_codec = "pcm_s16le"
        elif audio_format_text.startswith("PCM 24"):
            audio_codec = "pcm_s24le"
        elif audio_format_text.startswith("PCM 32-bit 整数"):
            audio_codec = "pcm_s32le"
        elif audio_format_text.startswith("PCM 32-bit 浮点"):
            audio_codec = "pcm_f32le"
        else:
            audio_codec = "aac"
        try:
            audio_sample_rate = normalize_audio_sample_rate(self.editor_join_audio_sample_rate_var.get())
            audio_bitrate = (
                normalize_audio_bitrate(self.editor_join_audio_bitrate_var.get())
                if audio_codec == "aac" else self.editor_join_audio_bitrate_var.get().strip() or "320k"
            )
            if audio:
                build_audio_encode_args(audio_codec, audio_bitrate, audio_sample_rate, output)
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        encode_settings = None
        if not lossless:
            encode_settings = self._settings()
            encode_settings.codec = join_video_codec
            if encode_settings.codec not in available_codecs(self.caps):
                messagebox.showerror(
                    APP_NAME,
                    f"当前显卡、驱动或 FFmpeg 无法使用 {encode_settings.codec}。请选择已检测可用的编码器。",
                )
                return
            if encode_settings.decode_mode == "cuda" and not self.caps.cuda_decode:
                messagebox.showerror(APP_NAME, "当前 FFmpeg 没有 CUDA 硬件解码支持；请在本页改用 CPU 解码兼容模式。")
                return
            encode_settings.video_bitrate = video_bitrate
            if audio_codec == "aac":
                encode_settings.audio_bitrate = audio_bitrate
        cached_results: dict[str, AlignmentResult] = {}
        if audio and self.editor_join_alignment_audio == audio:
            cached_results = {
                path: self.editor_join_alignments[path]
                for path in paths if path in self.editor_join_alignments
            }
        join_parallel_setting = max(1, int(self.parallel_var.get()))
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        self.cancel_event.clear()
        self._set_working(True)
        action = "无损紧凑拼接" if lossless else ("硬件黑场连续拼接" if preserve_gap else "硬件紧凑拼接")
        if audio:
            action += "并替换主音频"
        self._begin_shared_progress(f"{action}：准备素材…")
        self.editor_status_var.set(f"正在检查 {len(paths)} 个视频，准备{action}…")

        def worker() -> None:
            try:
                media = [probe_media(self.caps.ffprobe_path or "ffprobe", path) for path in paths]
                issues = concat_compatibility_issues(media, include_audio=not bool(audio))
                if issues:
                    raise RuntimeError(
                        "拼接要求视频规格一致：\n" + "\n".join(issues)
                        + "\n请先用导出页统一编码规格后再拼接。"
                    )
                intervals: list[tuple[float, float]] = []
                alignment_results: list[AlignmentResult] = []
                if audio:
                    audio_duration = probe_media_duration(self.caps.ffprobe_path or "ffprobe", audio)
                    result_by_path = dict(cached_results)
                    missing_paths = [path for path in paths if path not in result_by_path]
                    if missing_paths:
                        if any(not item.has_audio for path, item in zip(paths, media) if path in missing_paths):
                            raise RuntimeError("未对齐的片段缺少现场参考音轨，请先为这些片段手动设置主音频时间。")
                        self.events.put((
                            "editor_status",
                            f"正在自动对齐剩余 {len(missing_paths)} 段；已经手动设置的片段不会被覆盖…",
                        ))
                        missing_results = align_audio_segments(
                            self.caps.ffmpeg_path or "ffmpeg", missing_paths, audio,
                            max_offset_seconds=0.0,
                        )
                        result_by_path.update(zip(missing_paths, missing_results))
                        self.events.put((
                            "join_alignment_cached",
                            (audio, [(path, result_by_path[path]) for path in paths]),
                        ))
                    results = [result_by_path[path] for path in paths]
                    weak = [index for index, result in enumerate(results, start=1) if result.confidence < 0.10]
                    if weak:
                        raise RuntimeError(
                            "以下片段的音频匹配可信度过低："
                            + "、".join(str(index) for index in weak)
                            + "。请检查相机是否录到现场声，或先单独裁掉静音开头。"
                        )
                    alignment_results = results
                    intervals = aligned_audio_intervals(
                        results, [item.duration for item in media],
                        trim_before_audio=trim_before_audio,
                        keep_before_audio=keep_before_audio,
                    )
                    needs_leading_trim = any(result.offset_seconds > 0.25 for result in results)
                    if lossless and needs_leading_trim and trim_before_audio:
                        raise RuntimeError(
                            "裁掉主音频开始前的画面需要逐帧处理，不能使用无损视频流复制。"
                            "请将画面处理改为“重编码”。"
                        )
                if self.cancel_event.is_set():
                    self.events.put(("cancelled", None))
                    return
                parallel_plan: JoinExportPlan | None = None
                if audio and not lossless:
                    parallel_plan = make_join_export_plan(
                        alignment_results, media,
                        trim_before_audio=trim_before_audio,
                        keep_before_audio=keep_before_audio,
                        preserve_gaps=preserve_gap,
                        master_audio_duration=audio_duration,
                        trim_after_audio=trim_after_audio,
                        custom_start=custom_start,
                        custom_end=custom_end,
                    )
                    export_duration = parallel_plan.duration
                elif audio:
                    output_start, output_end, _ = resolve_join_output_range(
                        alignment_results, [item.duration for item in media],
                        trim_before_audio=trim_before_audio,
                        keep_before_audio=keep_before_audio,
                        preserve_gaps=preserve_gap,
                        master_audio_duration=audio_duration,
                        trim_after_audio=trim_after_audio,
                        custom_start=custom_start,
                        custom_end=custom_end,
                    )
                    export_duration = output_end - output_start
                else:
                    export_duration = sum(item.duration for item in media)

                requested_workers = min(
                    join_parallel_setting,
                    max(1, self.caps.gpu_profile.nvdec_engines),
                )
                use_parallel = bool(
                    parallel_plan is not None
                    and encode_settings is not None
                    and encode_settings.decode_mode == "cuda"
                    and requested_workers > 1
                    and export_duration >= 60.0
                )
                if use_parallel:
                    has_space, space_message = self._parallel_join_has_space(
                        output, export_duration, encode_settings.video_bitrate,
                    )
                    self.events.put(("log", space_message + "。"))
                    if not has_space:
                        use_parallel = False
                        self.events.put((
                            "log",
                            "缓存盘或成片磁盘空间不足，已自动改用单路直接导出。",
                        ))

                if use_parallel:
                    assert parallel_plan is not None and encode_settings is not None
                    route = (
                        f"{requested_workers} 路 NVDEC 并行 → 每段 NVENC → "
                        "视频码流无损合并 → 一次写入主音频"
                    )
                    self.events.put((
                        "log",
                        f"导出路径：{route}；保留原码率、预设、位深和多 NVENC 设置。",
                    ))
                    self.events.put(("editor_status", f"正在{action}；{route}"))
                    code, details = self._run_parallel_join_export(
                        paths, audio, media, output, encode_settings, parallel_plan,
                        requested_workers, action,
                        audio_bitrate=audio_bitrate,
                        audio_codec=audio_codec,
                        audio_sample_rate=audio_sample_rate,
                    )
                else:
                    with tempfile.TemporaryDirectory(prefix="okk_concat_", dir=cache_dir('exports')) as folder:
                        manifest = str(Path(folder) / "segments.ffconcat")
                        if lossless or not audio:
                            write_concat_manifest(paths, manifest)
                        if preserve_gap:
                            assert encode_settings is not None
                            command = build_gap_preserving_concat_encode_command(
                                self.caps.ffmpeg_path or "ffmpeg", paths, audio,
                                alignment_results, media, output, encode_settings, self.caps,
                                audio_bitrate=audio_bitrate, audio_codec=audio_codec,
                                audio_sample_rate=audio_sample_rate,
                                trim_before_audio=trim_before_audio,
                                keep_before_audio=keep_before_audio, preserve_gaps=True,
                                master_audio_duration=audio_duration,
                                trim_after_audio=trim_after_audio,
                                custom_start=custom_start, custom_end=custom_end,
                            )
                        elif lossless and audio:
                            command = build_aligned_concat_copy_command(
                                self.caps.ffmpeg_path or "ffmpeg", manifest, audio, intervals, output,
                                audio_bitrate=audio_bitrate, audio_codec=audio_codec,
                                audio_sample_rate=audio_sample_rate,
                            )
                        elif lossless:
                            command = build_concat_copy_command(self.caps.ffmpeg_path or "ffmpeg", manifest, output)
                        elif audio:
                            assert encode_settings is not None
                            command = build_gap_preserving_concat_encode_command(
                                self.caps.ffmpeg_path or "ffmpeg", paths, audio,
                                alignment_results, media, output, encode_settings, self.caps,
                                audio_bitrate=audio_bitrate, audio_codec=audio_codec,
                                audio_sample_rate=audio_sample_rate,
                                trim_before_audio=trim_before_audio,
                                keep_before_audio=keep_before_audio, preserve_gaps=False,
                                master_audio_duration=audio_duration,
                                trim_after_audio=trim_after_audio,
                                custom_start=custom_start, custom_end=custom_end,
                            )
                        else:
                            assert encode_settings is not None
                            command = build_concat_encode_command(
                                self.caps.ffmpeg_path or "ffmpeg", manifest, output,
                                encode_settings, media[0], self.caps,
                                external_audio=audio or None, intervals=intervals,
                                audio_bitrate=audio_bitrate, audio_codec=audio_codec,
                                audio_sample_rate=audio_sample_rate,
                            )
                        if encode_settings is not None and audio:
                            route = (
                                "CUDA 显存拼接 → 单路 NVENC 直接导出"
                                if join_uses_cuda_frames(encode_settings, media[0], self.caps)
                                else "内存兼容拼接 → 所选编码器直接导出"
                            )
                            self.events.put((
                                "log",
                                f"导出路径：{route}；只读取保留区间；无末尾整文件重排。",
                            ))
                        else:
                            route = join_mode_text
                        self.events.put(("editor_status", f"正在{action}；{route}"))
                        report_progress = self._editor_progress_reporter(
                            export_duration, action, source_fps=float(media[0].fps or 0.0),
                        )
                        code, details = self._run_editor_process(command, report_progress)
                if self.cancel_event.is_set():
                    self.events.put(("cancelled", None))
                elif code or not Path(output).is_file() or Path(output).stat().st_size <= 0:
                    self.events.put(("editor_job_error", f"{action}失败：\n" + details[-2600:]))
                else:
                    self.events.put(("editor_job_done", (action, [output])))
            except ExportCancelled:
                self.events.put(("cancelled", None))
            except Exception as exc:
                self.events.put(("editor_job_error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _export_split_streams(self) -> None:
        if self.working:
            return
        source = self.editor_video_var.get().strip()
        output_dir = self.editor_split_dir_var.get().strip()
        if not source or not Path(source).is_file() or not output_dir:
            messagebox.showerror(APP_NAME, "请先打开视频并选择输出目录。")
            return
        if self.editor_media and not self.editor_media.has_audio:
            messagebox.showerror(APP_NAME, "当前视频没有可分离的音频轨。")
            return
        if not self.caps.ffmpeg_path:
            messagebox.showerror(APP_NAME, "未找到 ffmpeg.exe。")
            return
        folder = Path(output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        source_path = Path(source)
        video_output = folder / f"{source_path.stem}_纯视频{source_path.suffix}"
        mode_text = self.editor_split_mode_var.get()
        mode, extension = (
            ("copy", ".mka") if mode_text.startswith("复制原音频")
            else ("wav24", ".wav") if mode_text.startswith("24-bit WAV")
            else ("flac", ".flac") if mode_text.startswith("FLAC")
            else ("aac", ".m4a")
        )
        audio_output = folder / f"{source_path.stem}_音频{extension}"
        existing = [path for path in (video_output, audio_output) if path.exists()]
        if existing and not messagebox.askyesno(APP_NAME, "输出文件已存在，是否覆盖？"):
            return
        video_command = build_extract_video_command(self.caps.ffmpeg_path, source, str(video_output))
        audio_command = build_extract_audio_command(self.caps.ffmpeg_path, source, str(audio_output), mode)
        self.cancel_event.clear()
        self._set_working(True)
        self._begin_shared_progress("音视频分离 0.0%")
        self.editor_status_var.set("正在无损复制视频轨并分离音频轨…")
        duration = max(0.0, float(self.editor_duration or 0.0))
        video_progress = self._editor_progress_reporter(
            duration, "分离视频 1/2", start_percent=0.0, span_percent=50.0,
            source_fps=float(self.editor_media.fps if self.editor_media else 0.0),
        )
        audio_progress = self._editor_progress_reporter(
            duration, "分离音频 2/2", start_percent=50.0, span_percent=50.0,
            source_fps=0.0,
        )

        def worker() -> None:
            code_v, details_v = self._run_editor_process(video_command, video_progress)
            if code_v == 0 and not self.cancel_event.is_set():
                code_a, details_a = self._run_editor_process(audio_command, audio_progress)
            else:
                code_a, details_a = 1, ""
            if self.cancel_event.is_set():
                self.events.put(("cancelled", None))
            elif code_v or code_a:
                self.events.put(("editor_job_error", "音视频分离失败：\n" + (details_v + details_a)[-2600:]))
            else:
                self.events.put(("editor_job_done", ("音视频分离", [str(video_output), str(audio_output)])))
        threading.Thread(target=worker, daemon=True).start()

    def _create_1080p_proxy(self) -> None:
        if self.working:
            return
        sources = list(self.editor_proxy_files)
        proxy_format = resolve_proxy_format(self.editor_proxy_format_var.get()).key
        output_dir = self.editor_proxy_dir_var.get().strip()
        if not sources:
            messagebox.showerror(APP_NAME, "请先点击“添加多个视频”，把需要代理的原片加入列表。")
            return
        if not output_dir:
            messagebox.showerror(APP_NAME, "请选择 ProxyMedia 代理输出目录。")
            return
        missing = [path for path in sources if not Path(path).is_file()]
        if missing:
            names = "、".join(Path(path).name for path in missing[:5])
            messagebox.showerror(APP_NAME, f"列表中有已经移动或删除的文件：{names}")
            return
        if not self.caps.ffmpeg_path or not self.caps.ffprobe_path:
            messagebox.showerror(APP_NAME, "未找到 ffmpeg.exe 或 ffprobe.exe。")
            return
        duplicate_names = duplicate_proxy_stems(sources)
        if duplicate_names:
            messagebox.showerror(
                APP_NAME,
                "不同原片会生成同名代理，请先修改这些原片的文件名：\n"
                + "\n".join(duplicate_names[:12]),
            )
            return
        folder = Path(output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        source_conflicts = proxy_source_conflicts(sources, folder, proxy_format)
        if source_conflicts:
            messagebox.showerror(
                APP_NAME,
                "代理输出目录与 MP4 原片所在目录相同，会覆盖原片。\n"
                "请新建或选择独立的 ProxyMedia 文件夹。",
            )
            return
        outputs = [proxy_output_path(source, folder, proxy_format) for source in sources]
        existing = [output for output in outputs if output.exists()]
        if existing and not messagebox.askyesno(
            APP_NAME, f"代理文件夹中已有 {len(existing)} 个同名代理，是否覆盖并继续其余任务？",
        ):
            return
        parallel_count = min(self._proxy_parallel_count(), len(sources))
        self.cancel_event.clear()
        self._set_working(True)
        self.editor_proxy_progress_state = {source: 0.0 for source in sources}
        self.progress_var.set(0)
        self.progress_text_var.set(f"批量代理 0/{len(sources)} · 同时 {parallel_count} 路")
        self.editor_status_var.set(
            f"已提交 {len(sources)} 个代理任务，{self.caps.gpu_profile.display_name} 同时处理 {parallel_count} 路…"
        )

        def worker() -> None:
            completed = 0
            successful: list[str] = []
            failures: list[tuple[str, str]] = []

            def encode_one(source: str, output: Path) -> tuple[str, str | None]:
                if self.cancel_event.is_set():
                    return str(output), "任务已取消"
                pending = output.with_name(f'.{output.stem}.okk-{uuid.uuid4().hex}{output.suffix}')
                try:
                    media = probe_media(self.caps.ffprobe_path or "ffprobe", source)
                    command = build_proxy_encode_command(
                        self.caps.ffmpeg_path or "ffmpeg", source, str(pending), media, self.caps, proxy_format,
                    )
                    self.events.put(("log", "代理命令：" + quote_command(command)))
                    recent_rate = RollingProgressRate()

                    def report_progress(progress: dict[str, str]) -> None:
                        if media.duration <= 0:
                            return
                        percent = min(99.9, progress_seconds(progress) / media.duration * 100.0)
                        recent_fps, recent_speed = recent_rate.update(progress, media.fps)
                        self.events.put((
                            "editor_proxy_live",
                            (
                                source, len(sources), percent,
                                f"{recent_fps:.3f}", f"{recent_speed:.3f}x", parallel_count,
                            ),
                        ))

                    code, details = self._run_editor_process(command, report_progress)
                    if self.cancel_event.is_set():
                        return str(output), "任务已取消"
                    if code or not pending.is_file() or pending.stat().st_size <= 0:
                        return str(output), details[-1600:] or f"FFmpeg 返回错误码 {code}"
                    from proxy_media import validate_proxy_media
                    validate_proxy_media(self.caps.ffprobe_path, source, str(pending), media.duration, media.fps)
                    publish_completed(pending, output, cancel_event=self.cancel_event)
                    try:
                        registered = register_proxy(source, str(output))
                    except Exception:
                        registered = False
                    if not registered:
                        self.events.put(('log', f'代理已生成，但自动发现索引未登记，请手动指定代理：{output}'))
                    return str(output), None
                except Exception as exc:
                    return str(output), str(exc)
                finally:
                    pending.unlink(missing_ok=True)

            with ThreadPoolExecutor(max_workers=parallel_count) as executor:
                future_map = {
                    executor.submit(encode_one, source, output): source
                    for source, output in zip(sources, outputs)
                }
                for future in as_completed(future_map):
                    source = future_map[future]
                    try:
                        output, error = future.result()
                    except Exception as exc:
                        output, error = str(proxy_output_path(source, folder, proxy_format)), str(exc)
                    completed += 1
                    if error:
                        failures.append((Path(source).name, error))
                    else:
                        successful.append(output)
                    self.events.put((
                        "editor_proxy_progress",
                        (completed, len(sources), source, error is None, parallel_count),
                    ))
            if self.cancel_event.is_set():
                self.events.put(("cancelled", None))
            else:
                self.events.put((
                    "editor_proxy_batch_done",
                    (successful, failures, len(sources), str(folder)),
                ))
        threading.Thread(target=worker, daemon=True).start()

    def _editor_seek_start(self, _event: tk.Event) -> None:
        self.editor_dragging = True

    def _editor_seek_finish(self, _event: tk.Event) -> None:
        self.editor_dragging = False
        if not self.mpv or self.editor_duration <= 0:
            return
        target = self.editor_timeline_var.get() / 1000.0 * self.editor_duration
        self._mpv_command_async(["seek", target, "absolute+exact"])

    def _mpv_command_async(self, command: list, *, error_prefix: str = "播放器操作失败") -> None:
        controller = self.mpv
        if not controller:
            messagebox.showerror(APP_NAME, "请先在剪辑工具中打开视频。")
            return

        def worker() -> None:
            try:
                controller.command(command)
            except Exception as exc:
                self.events.put(("editor_error", f"{error_prefix}：{exc}"))
        threading.Thread(target=worker, daemon=True).start()

    def _editor_toggle_play(self) -> None:
        self._mpv_command_async(["cycle", "pause"])

    def _editor_frame_step(self, direction: int) -> None:
        command = ["frame-step"] if direction > 0 else ["frame-back-step"]
        self._mpv_command_async(command, error_prefix="逐帧失败")

    def _editor_seek_relative(self, seconds: float) -> None:
        self._mpv_command_async(
            ["seek", float(seconds), "relative+exact"],
            error_prefix="10 秒跳转失败",
        )

    def _poll_editor_player(self) -> None:
        controller = self.mpv
        if controller and not self.editor_poll_busy and controller.process and controller.process.poll() is None:
            self.editor_poll_busy = True

            def worker() -> None:
                try:
                    current = float(controller.command(["get_property", "time-pos"]) or 0)
                    duration = float(controller.command(["get_property", "duration"]) or self.editor_duration or 0)
                    paused = bool(controller.command(["get_property", "pause"]))
                    preview_end = controller.preview_end_seconds
                    if preview_end is not None and current >= preview_end:
                        if not paused:
                            controller.command(["set_property", "pause", True])
                        current = min(current, preview_end)
                        paused = True
                    self.events.put(("editor_player_state", (current, duration, paused)))
                except Exception:
                    self.events.put(("editor_poll_end", None))
            threading.Thread(target=worker, daemon=True).start()
        self.after(250, self._poll_editor_player)

    def _capture_editor_frame(self) -> None:
        source = self.editor_video_var.get().strip()
        if not source or not Path(source).is_file():
            messagebox.showerror(APP_NAME, "请先在剪辑工具中打开视频。")
            return
        controller = self.mpv
        if not controller or not controller.process or controller.process.poll() is not None:
            messagebox.showerror(APP_NAME, "播放器尚未启动，请先点击“打开视频”。")
            return
        marker = format_time(self.editor_current_time).replace(":", "-").replace(".", "-")
        path = filedialog.asksaveasfilename(
            title="保存当前帧为高质量 JPEG",
            initialfile=f"{Path(source).stem}_{marker}.jpg",
            defaultextension=".jpg",
            filetypes=[("JPEG 高质量图像", "*.jpg *.jpeg")],
        )
        if not path:
            return
        if Path(path).suffix.lower() not in {".jpg", ".jpeg"}:
            path = str(Path(path).with_suffix(".jpg"))
        self.editor_status_var.set(f"正在截取 {format_time(self.editor_current_time)}…")

        def worker() -> None:
            try:
                output_path = Path(path)
                if output_path.exists():
                    output_path.unlink()
                controller.command(["screenshot-to-file", path, "video"], timeout=10.0)
                deadline = time.monotonic() + 15.0
                last_size = -1
                stable_checks = 0
                while time.monotonic() < deadline:
                    size = output_path.stat().st_size if output_path.is_file() else 0
                    if size > 0 and size == last_size:
                        stable_checks += 1
                        if stable_checks >= 2:
                            break
                    else:
                        stable_checks = 0
                    last_size = size
                    time.sleep(0.1)
                if not output_path.is_file() or output_path.stat().st_size <= 0:
                    raise RuntimeError("MPV 没有在规定时间内写出图片。")
                self.events.put(("editor_frame_done", path))
            except Exception as exc:
                self.events.put(("editor_error", f"截帧失败：{exc}"))
        threading.Thread(target=worker, daemon=True).start()

    def _editor_offset(self) -> float | None:
        try:
            return float(self.editor_offset_var.get().strip() or "0")
        except ValueError:
            messagebox.showerror(APP_NAME, "音频偏移必须是数字，例如 1.250 或 -0.500。")
            return None

    def _auto_align_audio(self) -> None:
        video = self.editor_video_var.get().strip()
        audio = self.editor_audio_var.get().strip()
        if not video or not Path(video).is_file() or not audio or not Path(audio).is_file():
            messagebox.showerror(APP_NAME, "请先选择视频和外部音频。")
            return
        if not self.caps.ffmpeg_path:
            messagebox.showerror(APP_NAME, "未找到 ffmpeg.exe。")
            return
        self.editor_status_var.set("正在分析两条音轨的波形；长演唱会可能需要一段时间…")

        def worker() -> None:
            try:
                result = align_audio(self.caps.ffmpeg_path or "ffmpeg", video, audio)
                self.events.put(("alignment_done", result))
            except Exception as exc:
                self.events.put(("editor_error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _preview_external_audio(self) -> None:
        video = self.editor_video_var.get().strip()
        audio = self.editor_audio_var.get().strip()
        offset = self._editor_offset()
        if offset is None:
            return
        if not self.mpv or not video or not audio or not Path(audio).is_file():
            messagebox.showerror(APP_NAME, "请先打开视频并选择外部音频。")
            return
        controller = self.mpv
        current = self.editor_current_time
        self.editor_status_var.set("正在加载外部音频试听…")

        def worker() -> None:
            try:
                try:
                    controller.command(["audio-remove"])
                except Exception:
                    pass
                controller.command(["audio-add", audio, "select", "替换音频"])
                controller.command(["set_property", "audio-delay", offset])
                controller.command(["seek", current, "absolute+exact"])
                controller.command(["set_property", "pause", False])
                self.events.put(("editor_status", f"正在试听外部音频；偏移 {offset:+.3f} 秒。"))
            except Exception as exc:
                self.events.put(("editor_error", f"加载试听音频失败：{exc}"))
        threading.Thread(target=worker, daemon=True).start()

    def _export_replaced_audio(self) -> None:
        if self.working:
            return
        video = self.editor_video_var.get().strip()
        audio = self.editor_audio_var.get().strip()
        output = self.editor_output_var.get().strip()
        offset = self._editor_offset()
        if offset is None:
            return
        if not video or not Path(video).is_file() or not audio or not Path(audio).is_file():
            messagebox.showerror(APP_NAME, "请先选择有效的视频和外部音频。")
            return
        if not output or Path(output).resolve() == Path(video).resolve():
            messagebox.showerror(APP_NAME, "请选择不会覆盖原视频的输出文件。")
            return
        if not self.caps.ffmpeg_path:
            messagebox.showerror(APP_NAME, "未找到 ffmpeg.exe。")
            return
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        command = build_replace_audio_command(self.caps.ffmpeg_path, video, audio, output, offset)
        self.cancel_event.clear()
        self._set_working(True)
        self._begin_shared_progress("替换音频 0.0%")
        self.editor_status_var.set("正在复制视频流并编码替换音频…")
        report_progress = self._editor_progress_reporter(
            self.editor_duration, "替换音频",
            source_fps=float(self.editor_media.fps if self.editor_media else 0.0),
        )

        def worker() -> None:
            code, output_text = self._run_editor_process(command, report_progress)
            if self.cancel_event.is_set():
                self.events.put(("cancelled", None))
            elif code or not Path(output).is_file() or Path(output).stat().st_size == 0:
                self.events.put(("editor_audio_error", "替换音频失败：\n" + output_text[-2400:]))
            else:
                self.events.put(("editor_audio_done", output))
        threading.Thread(target=worker, daemon=True).start()

    def _build_footer(self, parent: ttk.Frame) -> None:
        footer = tk.Frame(parent, bg=SURFACE)
        footer.pack(side="bottom", fill="x")
        status = tk.Frame(footer, bg=SURFACE, padx=10, pady=3,
                          highlightthickness=1, highlightbackground="#303237")
        status.pack(fill="x")
        self.cancel_button = ttk.Button(status, text="取消任务", style="Danger.TButton", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=(8, 0))
        self.progress_font = tkfont.Font(family=UI_FONT, size=8)
        self.progress_canvas = tk.Canvas(status, width=180, height=18, bg=INPUT,
                                         highlightthickness=0, bd=0)
        self.progress_canvas.pack(side="right", padx=8)
        self.progress_fill = self.progress_canvas.create_rectangle(0, 0, 0, 0, fill=PURPLE, outline="")
        self.progress_caption = self.progress_canvas.create_text(0, 0, text="0.0%", fill=TEXT, font=self.progress_font)
        self.progress_canvas.bind("<Configure>", self._redraw_shared_progress)
        self.progress_var.trace_add("write", self._redraw_shared_progress)
        tk.Label(status, textvariable=self.progress_text_var, bg=SURFACE, fg=MUTED,
                 font=(UI_FONT, 9), anchor="w").pack(side="left", fill="x", expand=True)
        nav = tk.Frame(footer, bg=SURFACE, pady=4)
        nav.pack(fill="x")
        self.page_buttons = {}
        holder = tk.Frame(nav, bg=SURFACE)
        holder.pack()
        for name, label in (("basic", "转码与 GPU"), ("editor", "剪辑工具"),
                            ("multicam", "多机位剪辑"), ("log", "任务日志")):
            button = tk.Button(holder, text=label, command=lambda key=name: self._select_page(key),
                               bg=SURFACE, fg=MUTED, activebackground=PANEL_ALT, activeforeground=TEXT,
                               font=(UI_FONT, 9), width=15, padx=8, pady=6, bd=0,
                               highlightthickness=1, highlightbackground=SURFACE, relief="flat")
            button.pack(side="left", padx=2)
            self.page_buttons[name] = button
        menu = tk.Menu(self, tearoff=False, bg=PANEL_ALT, fg=TEXT, activebackground="#414852", activeforeground=TEXT)
        menu.add_command(label="硬件与任务状态", command=self._show_hardware_info)
        menu.add_command(label="10秒性能测试", command=self._benchmark)
        menu.add_command(label="复制 FFmpeg 命令", command=self._copy_command)
        more = tk.Menubutton(nav, text="更多 ▾", menu=menu, bg=SURFACE, fg=MUTED,
                             activebackground=PANEL_ALT, activeforeground=TEXT,
                             relief="flat", font=(UI_FONT, 8))
        more.place(relx=1, rely=0.5, anchor="e", x=-12)

    def _redraw_shared_progress(self, *_args) -> None:
        """Keep small percentages readable without exaggerating the fill."""
        canvas = self.progress_canvas
        width, height = canvas.winfo_width(), canvas.winfo_height()
        percent = min(100.0, max(0.0, float(self.progress_var.get())))
        canvas.coords(self.progress_fill, 0, 0, width * percent / 100.0, height)
        canvas.coords(self.progress_caption, width / 2, height / 2)
        canvas.itemconfigure(self.progress_caption, text=f"{percent:.1f}%")

    def _file_row(self, parent: tk.Frame, label: str, variable: tk.StringVar, command, row: int) -> None:
        holder = tk.Frame(parent, bg=PANEL_ALT)
        holder.grid(row=row, column=0, sticky="ew", pady=7)
        holder.columnconfigure(0, weight=1)
        tk.Label(holder, text=label, bg=PANEL_ALT, fg="#c8cdd3", font=(UI_FONT, 10, "bold")).grid(row=0, column=0, sticky="w")
        line = tk.Frame(holder, bg=PANEL_ALT)
        line.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        line.columnconfigure(0, weight=1)
        entry = tk.Entry(
            line, textvariable=variable, bg=INPUT, fg=TEXT, insertbackground=TEXT,
            relief="flat", highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=PINK, font=(UI_FONT, 11),
        )
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 9), ipady=10)
        ttk.Button(line, text="浏览…", command=command).grid(row=0, column=1)

    def _combo(
        self, parent: ttk.Frame, label: str, variable: tk.Variable, values: list[str],
        row: int, col: int, thumb: str,
    ) -> ttk.Combobox:
        holder = tk.Frame(parent, bg=PANEL_ALT, highlightthickness=1, highlightbackground=BORDER, padx=16, pady=14)
        holder.grid(row=row, column=col, sticky="nsew", padx=(0 if col == 0 else 9, 9 if col == 0 else 0), pady=9)
        self._add_glass_shine(holder)
        photo = self._photo_badge(holder, thumb, bg=PANEL_ALT)
        photo.pack(side="right", padx=(20, 0))
        content = tk.Frame(holder, bg=PANEL_ALT)
        content.pack(side="left", fill="x", expand=True)
        tk.Label(content, text=label, bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 12, "bold")).pack(anchor="w")
        combo = ttk.Combobox(content, textvariable=variable, values=values, state="readonly")
        combo.pack(fill="x", pady=(13, 0))
        return combo

    def _build_basic(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="渲染设置", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 7))
        self.basic_gpu_subtitle = ttk.Label(
            parent, text="正在识别显卡并检测实际可用编码器…", style="Muted.TLabel", font=(UI_FONT, 11),
        )
        self.basic_gpu_subtitle.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.codec_combo = self._combo(parent, "编码格式", self.codec_var, list(CODEC_LABELS.values()), 2, 0, "thumb_02_cat.png")
        self._combo(parent, "速度 / 画质预设", self.preset_var, ["最快 P1", "极速 P2", "均衡 P4", "高质量 P6", "最高质量 P7"], 2, 1, "thumb_03_blonde.png")
        self._combo(parent, "输出分辨率", self.resolution_var, ["保持原分辨率", "1080p", "1440p", "4K", "8K"], 3, 0, "thumb_05_feathers.png")
        self._combo(parent, "输出位深", self.depth_var, ["自动", "8-bit", "10-bit"], 3, 1, "thumb_07_ice.png")
        self._combo(parent, "音频", self.audio_var, ["AAC 320 kbps", "AAC 192 kbps", "复制原音频", "无音频"], 4, 0, "thumb_08_hanfu.png")
        qframe = tk.Frame(parent, bg=PANEL_ALT, highlightthickness=1, highlightbackground=BORDER, padx=16, pady=14)
        qframe.grid(row=4, column=1, sticky="nsew", padx=(9, 0), pady=9)
        self._add_glass_shine(qframe)
        self._photo_badge(qframe, "thumb_13_red.png", bg=PANEL_ALT).pack(side="right", padx=(20, 0))
        qcontent = tk.Frame(qframe, bg=PANEL_ALT)
        qcontent.pack(side="left", fill="x", expand=True)
        tk.Label(qcontent, text="恒定质量 CQ（越小画质越高）", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 12, "bold")).pack(anchor="w")
        line = tk.Frame(qcontent, bg=PANEL_ALT)
        line.pack(fill="x", pady=(13, 0))
        tk.Scale(line, from_=14, to=32, orient="horizontal", variable=self.quality_var, bg=PANEL_ALT, fg=TEXT,
                 troughcolor="#3c424a", highlightthickness=0, activebackground=PINK,
                 sliderlength=28, width=20, length=280).pack(side="left", fill="x", expand=True)
        tk.Label(line, textvariable=self.quality_var, bg=PANEL_ALT, fg=TEXT, width=3, font=(UI_FONT, 14, "bold")).pack(side="left")
        tip = tk.Frame(parent, bg="#292d32", highlightthickness=1, highlightbackground="#414852", padx=18, pady=15)
        tip.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self._add_glass_shine(tip)
        self.basic_recommendation_label = tk.Label(
            tip, text="推荐参数将在 GPU 检测完成后自动显示。", bg="#292d32", fg="#ccd4dc", font=(UI_FONT, 11),
        )
        self.basic_recommendation_label.pack(anchor="w")
        for col in (0, 1):
            parent.columnconfigure(col, weight=1)
        for row in (2, 3, 4):
            parent.rowconfigure(row, weight=1, uniform="export_settings")

    def _build_advanced(self, parent: ttk.Frame) -> None:
        self.advanced_title_label = ttk.Label(parent, text="显卡自动适配加速", style="Section.TLabel")
        self.advanced_title_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 7))
        ttk.Label(parent, text="硬件编码、兼容解码与并行设置", style="Muted.TLabel", font=(UI_FONT, 11)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.decode_combo = self._combo(parent, "解码路径", self.decode_var, ["NVIDIA CUDA（推荐）", "CPU 解码（兼容模式）"], 2, 0, "thumb_09_silver_portrait.png")
        self.split_combo = self._combo(
            parent, "多 NVENC 分帧", self.split_var, ["驱动自动选择（检测中）", "关闭"], 2, 1, "thumb_10_chin.png",
        )

        pframe = tk.Frame(parent, bg=PANEL_ALT, highlightthickness=1, highlightbackground=BORDER, padx=16, pady=14)
        pframe.grid(row=3, column=0, sticky="nsew", padx=(0, 9), pady=9)
        self._add_glass_shine(pframe)
        self._photo_badge(pframe, "thumb_11_purple.png", bg=PANEL_ALT).pack(side="right", padx=(20, 0))
        pcontent = tk.Frame(pframe, bg=PANEL_ALT)
        pcontent.pack(side="left", fill="x", expand=True)
        tk.Label(pcontent, text="并行分段任务数", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 12, "bold")).pack(anchor="w")
        line = tk.Frame(pcontent, bg=PANEL_ALT)
        line.pack(fill="x", pady=(13, 0))
        tk.Scale(line, from_=1, to=4, resolution=1, orient="horizontal", variable=self.parallel_var, bg=PANEL_ALT, fg=TEXT,
                 troughcolor="#3c424a", highlightthickness=0, activebackground=PINK,
                 sliderlength=28, width=20, length=300).pack(side="left", fill="x", expand=True)
        tk.Label(line, textvariable=self.parallel_var, bg=PANEL_ALT, fg=TEXT, width=3, font=(UI_FONT, 14, "bold")).pack(side="left")

        mframe = tk.Frame(parent, bg=PANEL_ALT, highlightthickness=1, highlightbackground=BORDER, padx=16, pady=14)
        mframe.grid(row=3, column=1, sticky="nsew", padx=(9, 0), pady=9)
        self._add_glass_shine(mframe)
        self._photo_badge(mframe, "thumb_12_silver_outfit.png", bg=PANEL_ALT).pack(side="right", padx=(20, 0))
        mcontent = tk.Frame(mframe, bg=PANEL_ALT)
        mcontent.pack(side="left", fill="x", expand=True)
        tk.Label(mcontent, text="最大码率（可留空，例如 80M）", bg=PANEL_ALT, fg="#d7dbe1", font=(UI_FONT, 12, "bold")).pack(anchor="w")
        tk.Entry(
            mcontent, textvariable=self.maxrate_var, bg=INPUT, fg=TEXT, insertbackground=TEXT,
            relief="flat", highlightthickness=1, highlightbackground=BORDER, highlightcolor=PINK,
            font=(UI_FONT, 12),
        ).pack(fill="x", pady=(13, 0), ipady=11)

        note = "正在检测硬件编码能力…"
        note_panel = tk.Frame(parent, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER, padx=24, pady=20)
        note_panel.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(16, 0))
        self._add_glass_shine(note_panel)
        tk.Label(
            note_panel, text="OKK GROUP · 性能建议", bg=SURFACE, fg=PINK,
            font=(UI_FONT, 13, "bold"),
        ).pack(anchor="w", pady=(0, 12))
        self.advanced_note_label = tk.Label(
            note_panel, text=note, bg=SURFACE, fg=MUTED, font=(UI_FONT, 11),
            wraplength=1320, justify="left",
        )
        self.advanced_note_label.pack(anchor="nw")
        for col in (0, 1):
            parent.columnconfigure(col, weight=1)
        for row in (2, 3):
            parent.rowconfigure(row, weight=2, uniform="advanced_settings")
        parent.rowconfigure(4, weight=1)

    def _build_log(self, parent: ttk.Frame) -> None:
        self.log = tk.Text(
            parent, bg="#0d0f18", fg="#c9c5db", insertbackground=TEXT, relief="flat",
            wrap="word", padx=12, pady=10, highlightthickness=1, highlightbackground=BORDER,
            font=(MONO_FONT, 11),
        )
        scroll = ttk.Scrollbar(parent, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._log("应用启动。正在检测硬件与 FFmpeg…")

    def _log(self, message: str) -> None:
        if not hasattr(self, "log"):
            return
        timestamp = time.strftime("%H:%M:%S")
        self.log.insert("end", f"[{timestamp}] {message.rstrip()}\n")
        self.log.see("end")

    def _detect_async(self) -> None:
        def worker() -> None:
            caps = detect_capabilities(app_directory())
            self.events.put(("caps", caps))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_gpu_profile(self) -> None:
        profile = self.caps.gpu_profile
        allowed = available_codecs(self.caps)
        codec_values = [label for codec, label in CODEC_LABELS.items() if codec in allowed]
        self.codec_combo.configure(values=codec_values)
        if self.codec_var.get() not in codec_values and codec_values:
            self.codec_var.set(codec_label(preferred_codec(self.caps)))
        if '_vendor_codec_trace' not in vars(self):
            self._vendor_codec_trace = self.codec_var.trace_add('write', self._configure_encoder_controls)
        self._configure_encoder_controls()
        try:
            selected_vendor = encoder_vendor(codec_from_label(self.codec_var.get()))
        except ValueError:
            selected_vendor = 'unknown'
        if selected_vendor != 'nvidia':
            backend = {'amd': 'AMD AMF', 'intel': 'Intel QSV', 'cpu': 'CPU 软件编码'}.get(selected_vendor, '无可用编码器')
            name = self._encoder_gpu_name(selected_vendor)
            if name in {'NVIDIA GPU not detected', 'GPU not detected', ''}:
                name = '未检测到可用硬件编码设备'
            self.gpu_status_var.set(f'{name}  |  {backend} · 按驱动检测结果适配')
            self.gpu_detail_var.set(name[:30])
            self.encode_detail_var.set(backend)
            self.decode_detail_var.set('D3D11 硬件监看 / 兼容解码导出')
            if self.caps.gpu_memory_mb:
                self.memory_detail_var.set(f'总显存 {self.caps.gpu_memory_mb / 1024:.1f} GB')
            for control_name in ('split_combo', 'editor_join_split_combo'):
                control = vars(self).get(control_name)
                if control is not None:
                    control.configure(values=['关闭'], state='disabled')
            self.split_var.set('关闭')
            proxy_values = ['同时 1 路（推荐）', '同时 2 路', '同时 3 路']
            self.proxy_parallel_combo.configure(values=proxy_values)
            self.editor_proxy_parallel_var.set(proxy_values[0])
            self.parallel_var.set(1)
            self.basic_gpu_subtitle.configure(text=f'已适配 {backend}；仅显示已检测可用的编码器')
            self.advanced_title_label.configure(text='跨显卡编码设置')
            self.proxy_specs_title_label.configure(text=f'{backend} 代理规格')
            self.basic_recommendation_label.configure(text='默认单任务。硬件编码不支持当前输入时可选择 CPU 编码；10-bit 取决于实际检测能力。')
            self.advanced_note_label.configure(text='AMD 使用 AMF，Intel 使用 QSV，软件回退使用 x264 / x265。\n\n导出通过兼容解码提供画面，监看可用 D3D11 硬件解码。CUDA、NVENC 多引擎分帧及其专属选项仅适用于 NVIDIA。')
            self._update_join_shared_settings_summary()
            return
        generation = f"第 {profile.nvenc_generation} 代" if profile.nvenc_generation else "未知代际"
        memory = f" · {self.caps.gpu_memory_mb / 1024:.1f} GB" if self.caps.gpu_memory_mb else ""
        support = "已自动适配" if profile.in_supported_range else "兼容模式"

        self.gpu_status_var.set(
            f"{profile.display_name}  |  {profile.architecture} · {profile.form_factor} · "
            f"{generation} NVENC ×{profile.nvenc_engines} · NVDEC ×{profile.nvdec_engines}"
            f"{memory} · Driver {self.caps.driver_version} · {support}"
        )
        self.gpu_detail_var.set(profile.display_name[:30])
        self.encode_detail_var.set(f"{generation} · NVENC ×{profile.nvenc_engines}")
        self.decode_detail_var.set(f"{profile.architecture} · NVDEC ×{profile.nvdec_engines}")
        if self.caps.gpu_memory_mb:
            self.memory_detail_var.set(f"总显存 {self.caps.gpu_memory_mb / 1024:.1f} GB")

        split_capable = bool(self.caps.split_encode & {"hevc_nvenc", "av1_nvenc"})
        if profile.nvenc_engines > 1 and split_capable:
            forced_label = f"强制 {profile.nvenc_engines} 路 NVENC（推荐）"
            split_values = [forced_label, "驱动自动选择", "关闭"]
            self.split_var.set(forced_label)
            split_advice = f"HEVC/AV1 已设为强制 {profile.nvenc_engines} 路分帧"
        elif profile.nvenc_engines == 1:
            split_values = ["单路 NVENC（此型号推荐）", "驱动自动选择", "关闭"]
            self.split_var.set(split_values[0])
            split_advice = "此型号为单 NVENC，已关闭无效的多引擎分帧"
        else:
            split_values = ["驱动自动选择（当前 FFmpeg）", "关闭"]
            self.split_var.set(split_values[0])
            split_advice = "当前 FFmpeg 未提供 split_encode_mode，交给驱动自动调度"
        self.split_combo.configure(values=split_values)
        if hasattr(self, "editor_join_split_combo"):
            self.editor_join_split_combo.configure(values=split_values)

        recommended = profile.recommended_proxy_parallel
        if recommended == 1:
            proxy_values = ["同时 1 路（推荐）", "同时 2 路", "同时 3 路"]
        elif recommended == 2:
            proxy_values = ["同时 1 路（稳妥）", "同时 2 路（推荐）", "同时 3 路（冲刺）"]
        else:
            proxy_values = ["同时 1 路（稳妥）", "同时 2 路", "同时 3 路（推荐）"]
        self.proxy_parallel_combo.configure(values=proxy_values)
        self.editor_proxy_parallel_var.set(proxy_values[recommended - 1])
        self.parallel_var.set(min(2, profile.nvdec_engines))
        self._update_join_shared_settings_summary()

        self.basic_gpu_subtitle.configure(
            text=f"已为 {profile.display_name} 生成配置：{profile.architecture} · {generation} NVENC ×{profile.nvenc_engines}"
        )
        self.advanced_title_label.configure(text=f"{profile.display_name} 自动适配加速")
        self.proxy_specs_title_label.configure(text=f"{profile.display_name} 快速代理规格")
        if profile.nvenc_engines > 1:
            recommendation = (
                f"推荐：HEVC/AV1 使用 P2、CQ 20–23、10-bit、{profile.nvenc_engines} 路 NVENC；"
                f"剪辑并行 {min(2, profile.nvdec_engines)} 路，批量代理同时 {recommended} 路。"
            )
        else:
            recommendation = "推荐：HEVC/AV1 使用 P2、CQ 20–23；单 NVENC 型号优先保持单路，避免并发争抢。"
        self.basic_recommendation_label.configure(text=recommendation)
        self.advanced_note_label.configure(
            text=(
                f"检测结果：{profile.display_name}，{profile.architecture}，{generation} NVENC，"
                f"共 {profile.nvenc_engines} 组编码引擎；{split_advice}。\n\n"
                f"硬件分帧只用于 HEVC/AV1；H.264 会自动忽略该选项。并行分段会启动最多 {profile.nvdec_engines} 个解码会话并在完成后按帧序无损合并。"
                "临时分段写入成片所在磁盘，空间不足时自动改用单路直出。任务管理器的 3D 百分比不代表 NVENC 是否跑满，"
                "请优先观察软件里的“视频编码”和实际导出倍速。"
            )
        )
        self._configure_encoder_controls()

    def _encoder_gpu_name(self, vendor):
        needles = {'nvidia': ('NVIDIA','GEFORCE','RTX','QUADRO'),
                   'amd': ('AMD','RADEON','ATI '), 'intel': ('INTEL',)}.get(vendor, ())
        names = getattr(self.caps, 'gpu_names', ()) or (self.caps.gpu_name,)
        return next((name for name in names if any(token in name.upper() for token in needles)),
                    {'nvidia':'NVIDIA GPU','amd':'AMD GPU','intel':'Intel GPU','cpu':'CPU 软件编码'}.get(vendor,'未检测到可用硬件编码设备'))

    def _configure_encoder_controls(self, *_):
        """Keep vendor-specific options out of other encoders' UI settings."""
        try:
            codec = codec_from_label(self.codec_var.get())
        except ValueError:
            return
        nvenc = codec.endswith('_nvenc')
        vendor = encoder_vendor(codec)
        backend = {'nvidia':'NVIDIA NVENC','amd':'AMD AMF','intel':'Intel QSV','cpu':'CPU 软件编码'}.get(vendor,codec)
        self._selected_encoder_vendor = vendor
        self.encode_detail_var.set(backend)
        self.gpu_detail_var.set(self._encoder_gpu_name(vendor)[:30])
        self.gpu_status_var.set(f'{self._encoder_gpu_name(vendor)}  |  {backend} · 按实际检测能力适配')
        if vendor in {'amd','intel','cpu'}:
            self.advanced_title_label.configure(text=f'{backend} 编码设置')
            self.basic_recommendation_label.configure(text=f'当前选择 {backend}。默认单任务；画质与位深依实际编码器能力设置。')
            for variable in (self.gpu_metric_var,self.encode_metric_var,self.decode_metric_var,self.memory_metric_var):
                variable.set('--')
            for variable in (self.gpu_meter_var,self.encode_meter_var,self.decode_meter_var,self.memory_meter_var):
                variable.set(0)
            self.monitor_var.set(f'{backend} · 当前不提供此设备的利用率遥测，请参考导出 FPS 和系统任务管理器。')
        decode_values = ['CPU 解码（兼容模式）']
        if nvenc and self.caps.cuda_decode:
            decode_values.insert(0, 'NVIDIA CUDA（推荐）')
        control = vars(self).get('decode_combo')
        if control is not None:
            control.configure(values=decode_values, state='readonly' if nvenc else 'disabled')
        if not nvenc or not self.caps.cuda_decode:
            if self.decode_var.get().startswith('NVIDIA'):
                self.decode_var.set('CPU 解码（兼容模式）')
        split_supported = bool(nvenc and codec in self.caps.split_encode and
            self.caps.gpu_profile.nvenc_engines > 1)
        for name in ('split_combo', 'editor_join_split_combo'):
            control = vars(self).get(name)
            if control is not None:
                control.configure(state='readonly' if split_supported else 'disabled')

    def _choose_input(self) -> None:
        path = filedialog.askopenfilename(title="选择输入视频", filetypes=[
            ("视频文件", "*.mp4 *.mov *.mkv *.mxf *.avi *.webm *.mts *.m2ts"),
            ("所有文件", "*.*"),
        ])
        if not path:
            return
        self._set_input_video(path)

    def _set_input_video(self, path: str) -> None:
        self.input_var.set(path)
        source = Path(path)
        self.output_var.set(str(source.with_name(f"{source.stem}_OKK_HEVC.mp4")))
        self.media_var.set("正在读取媒体信息…")

        def worker() -> None:
            try:
                if not self.caps.ffprobe_path:
                    raise RuntimeError("未找到 ffprobe")
                info = probe_media(self.caps.ffprobe_path, path)
                self.events.put(("media", info))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _choose_output(self) -> None:
        source = Path(self.input_var.get() or "output.mp4")
        path = filedialog.asksaveasfilename(
            title="选择输出文件",
            initialfile=f"{source.stem}_OKK_HEVC.mp4",
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4"), ("Matroska", "*.mkv"), ("MOV", "*.mov")],
        )
        if path:
            self.output_var.set(path)

    def _settings(self) -> ExportSettings:
        codec = codec_from_label(self.codec_var.get())
        preset = self.preset_var.get().split()[-1].lower()
        resolution = {
            "保持原分辨率": "original", "1080p": "1080p", "1440p": "1440p", "4K": "4k", "8K": "8k",
        }[self.resolution_var.get()]
        audio_value = self.audio_var.get()
        audio = "aac"
        bitrate = "320k"
        if audio_value == "复制原音频":
            audio = "copy"
        elif audio_value == "无音频":
            audio = "none"
        elif "192" in audio_value:
            bitrate = "192k"
        split_text = self.split_var.get()
        split_match = re.search(r"强制\s*(\d+)\s*路", split_text)
        if split_match:
            split = split_match.group(1)
        elif split_text.startswith("单路") or split_text == "关闭":
            split = "disabled"
        else:
            split = "auto"
        return ExportSettings(
            codec=codec,
            quality=self.quality_var.get(),
            preset=preset,
            multipass=("disabled" if preset in {"p1", "p2"} or not codec.endswith('_nvenc')
                       else "qres" if preset == "p4" else "fullres"),
            split_encode=split if codec.endswith('_nvenc') else 'disabled',
            resolution=resolution,
            bit_depth=self.depth_var.get().replace("自动", "auto"),
            audio=audio,
            audio_bitrate=bitrate,
            decode_mode="cuda" if codec.endswith('_nvenc') and self.decode_var.get().startswith("NVIDIA") else "cpu",
            maxrate=self.maxrate_var.get().strip(),
        )

    def _validate(self) -> tuple[str, str, ExportSettings, MediaInfo] | None:
        source = self.input_var.get().strip()
        output = self.output_var.get().strip()
        if not source or not Path(source).is_file():
            messagebox.showerror(APP_NAME, "请选择有效的输入视频。")
            return None
        if not output:
            messagebox.showerror(APP_NAME, "请选择输出文件。")
            return None
        if Path(source).resolve() == Path(output).resolve():
            messagebox.showerror(APP_NAME, "输出文件不能覆盖输入文件。")
            return None
        if not self.caps.ffmpeg_path or not self.caps.ffprobe_path:
            messagebox.showerror(APP_NAME, "未找到 FFmpeg/ffprobe。请把 ffmpeg.exe 和 ffprobe.exe 放入软件的 bin 文件夹。")
            return None
        try:
            settings = self._settings()
        except ValueError as error:
            messagebox.showerror(APP_NAME, str(error))
            return None
        if settings.codec not in available_codecs(self.caps):
            messagebox.showerror(APP_NAME, f"当前显卡、驱动或 FFmpeg 无法使用 {settings.codec}。请从编码格式中选择已检测可用的编码器。")
            return None
        if settings.codec.startswith('h264_') and settings.bit_depth == '10-bit':
            messagebox.showerror(APP_NAME, 'H.264 硬件编码仅支持 8-bit；10-bit 请改用可用的 HEVC / AV1 或 CPU 编码。')
            return None
        if (getattr(self.caps, 'hardware_probe_complete', False)
                and settings.codec in self.caps.hardware_codecs and settings.bit_depth == '10-bit'
                and settings.codec not in self.caps.ten_bit_codecs):
            messagebox.showerror(APP_NAME, '当前显卡或驱动未通过此编码器的 10-bit 检测，请选择 8-bit 或其他编码器。')
            return None
        try:
            media = self.media if self.media and self.media.path == source else probe_media(self.caps.ffprobe_path, source)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return None
        if settings.decode_mode == "cuda" and not self.caps.cuda_decode:
            messagebox.showerror(APP_NAME, "当前 FFmpeg 没有 CUDA 硬件加速支持。")
            return None
        if Path(output).suffix.lower() == ".mp4" and settings.audio == "copy" and media.audio_codec not in {"aac", "mp3", "ac3", "eac3", "alac", "none"}:
            if not messagebox.askyesno(APP_NAME, f"原音频 {media.audio_codec} 可能无法封装进 MP4。是否自动改为 AAC 320 kbps？"):
                return None
            settings.audio = "aac"
        return source, output, settings, media

    def _copy_command(self) -> None:
        validated = self._validate()
        if not validated:
            return
        source, output, settings, media = validated
        command = build_encode_command(self.caps.ffmpeg_path or "ffmpeg", source, output, settings, media, self.caps)
        self.clipboard_clear()
        self.clipboard_append(quote_command(command))
        self._log("已复制命令：\n" + quote_command(command))
        messagebox.showinfo(APP_NAME, "FFmpeg 命令已复制到剪贴板。")

    def _start_export(self) -> None:
        if self.working:
            return
        validated = self._validate()
        if not validated:
            return
        source, output, settings, media = validated
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        self._set_working(True)
        self.cancel_event.clear()
        self.progress_var.set(0)
        self.chunk_progress_state.clear()
        self.progress_text_var.set("准备导出…")
        threading.Thread(
            target=self._export_worker,
            args=(source, output, settings, media, self.parallel_var.get()),
            daemon=True,
        ).start()

    def _benchmark(self) -> None:
        if self.working:
            return
        validated = self._validate()
        if not validated:
            return
        source, _output, settings, media = validated
        self._set_working(True)
        self.cancel_event.clear()
        self.progress_var.set(0)
        self.progress_text_var.set("10 秒性能测试…")

        def worker() -> None:
            null_output = "NUL" if os.name == "nt" else "/dev/null"
            command = build_encode_command(
                self.caps.ffmpeg_path or "ffmpeg", source, null_output, settings, media, self.caps,
                start=0, duration=min(10.0, media.duration or 10.0), video_only=True,
            )
            # The null muxer makes this a pure decode/encode benchmark.
            command[-1:-1] = ["-f", "null"]
            started = time.perf_counter()
            try:
                self._run_ffmpeg(
                    command, min(10.0, media.duration or 10.0),
                    lambda p, fps, speed: self.events.put(("progress", (p, fps, speed))),
                    source_fps=media.fps,
                )
                elapsed = time.perf_counter() - started
                ratio = min(10.0, media.duration or 10.0) / elapsed if elapsed else 0
                self.events.put(("benchmark_done", (elapsed, ratio)))
            except Exception as exc:
                self.events.put(("job_error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _export_worker(self, source: str, output: str, settings: ExportSettings, media: MediaInfo, segments: int) -> None:
        try:
            if segments <= 1:
                command = build_encode_command(self.caps.ffmpeg_path or "ffmpeg", source, output, settings, media, self.caps)
                self.events.put(("log", "执行命令：\n" + quote_command(command)))
                self._run_ffmpeg(
                    command, media.duration,
                    lambda p, fps, speed: self.events.put(("progress", (p, fps, speed))),
                    source_fps=media.fps,
                )
            else:
                self._parallel_export(source, output, settings, media, segments)
            if self.cancel_event.is_set():
                raise ExportCancelled("导出已取消")
            if not Path(output).is_file() or Path(output).stat().st_size == 0:
                raise RuntimeError("FFmpeg 未生成有效输出文件")
            self.events.put(("done", output))
        except ExportCancelled:
            self.events.put(("cancelled", None))
        except Exception as exc:
            self.events.put(("job_error", str(exc)))

    def _parallel_export(self, source: str, output: str, settings: ExportSettings, media: MediaInfo, segments: int) -> None:
        if media.duration <= 0:
            raise RuntimeError("无法读取视频时长，不能使用并行分段模式")
        temp_dir = Path(tempfile.mkdtemp(prefix="rtx_export_", dir=cache_dir('exports')))
        self.events.put(("log", f"并行分段模式：{segments} 路；临时目录 {temp_dir}"))
        ranges = split_ranges(media.duration, segments)
        chunk_paths = [temp_dir / f"chunk_{index:03d}.mkv" for index in range(segments)]

        def encode_chunk(index: int) -> None:
            start, duration = ranges[index]
            command = build_encode_command(
                self.caps.ffmpeg_path or "ffmpeg", source, str(chunk_paths[index]), settings, media, self.caps,
                start=start, duration=duration, video_only=True,
            )
            self.events.put(("log", f"分段 {index + 1}/{segments}：\n{quote_command(command)}"))
            self._run_ffmpeg(
                command,
                duration,
                lambda p, fps, speed: self.events.put(
                    ("chunk_progress", (index, p, fps, speed, segments))
                ),
                source_fps=media.fps,
            )

        try:
            with ThreadPoolExecutor(max_workers=segments, thread_name_prefix="nvenc") as pool:
                futures = [pool.submit(encode_chunk, index) for index in range(segments)]
                for future in as_completed(futures):
                    future.result()
            if self.cancel_event.is_set():
                raise ExportCancelled()

            concat_file = temp_dir / "concat.txt"
            with concat_file.open("w", encoding="utf-8", newline="\n") as handle:
                for chunk in chunk_paths:
                    safe_path = str(chunk).replace("\\", "/").replace("'", "'\\''")
                    handle.write(f"file '{safe_path}'\n")
            merged = temp_dir / "merged_video.mkv"
            concat_command = [
                self.caps.ffmpeg_path or "ffmpeg", "-hide_banner", "-y", "-loglevel", "warning",
                "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(merged),
            ]
            self.events.put(("log", "拼接视频分段…"))
            self._run_simple(concat_command)
            completed = temp_dir / ('completed' + Path(output).suffix.lower())
            mux_command = build_mux_command(self.caps.ffmpeg_path or "ffmpeg", str(merged), source, str(completed), settings)
            self.events.put(("log", "封装原始音频：\n" + quote_command(mux_command)))
            self._run_ffmpeg(
                mux_command, media.duration,
                lambda p, fps, speed: self.events.put(("mux_progress", (p, fps, speed))),
                source_fps=media.fps,
            )
            publish_completed(completed, output, cancel_event=self.cancel_event,
                progress=lambda copied,total:self.events.put(('log',f'写入完成成片 {copied/max(1,total)*100:.1f}%')))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _run_simple(self, command: list[str]) -> None:
        if self.cancel_event.is_set():
            raise ExportCancelled()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
        self._register_process(process)
        output, _ = process.communicate()
        self._unregister_process(process)
        if self.cancel_event.is_set():
            raise ExportCancelled()
        if process.returncode:
            raise RuntimeError(f"FFmpeg 拼接失败：\n{output[-4000:]}")

    def _run_ffmpeg(
        self, command: list[str], duration: float, callback, *, source_fps: float = 0.0,
    ) -> None:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", bufsize=1, creationflags=CREATE_NO_WINDOW,
        )
        self._register_process(process)
        progress: dict[str, str] = {}
        errors: list[str] = []
        try:
            assert process.stdout is not None
            assert process.stderr is not None

            def drain_stderr() -> None:
                for error_line in process.stderr:
                    stripped = error_line.strip()
                    if stripped:
                        errors.append(stripped)
                        del errors[:-80]
                        self.events.put(("log", stripped))

            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            stderr_thread.start()
            callback(0.0, "0", "0x")
            for line in process.stdout:
                if self.cancel_event.is_set():
                    self._terminate(process)
                    raise ExportCancelled()
                pair = parse_progress_line(line)
                if pair:
                    progress[pair[0]] = pair[1]
                    if pair[0] == "progress":
                        seconds = progress_seconds(progress)
                        percent = min(100.0, seconds / duration * 100) if duration > 0 else 0.0
                        callback(
                            percent, f"{progress_fps_value(progress, source_fps):.3f}",
                            progress.get("speed", "--x")
                        )
                else:
                    stripped = line.strip()
                    if stripped:
                        errors.append(stripped)
                        self.events.put(("log", stripped))
                        errors = errors[-80:]
            return_code = process.wait()
            stderr_thread.join(timeout=2.0)
        finally:
            self._unregister_process(process)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        if self.cancel_event.is_set():
            raise ExportCancelled()
        if return_code:
            tail = "\n".join(errors[-30:]) or f"FFmpeg exit code {return_code}"
            if "No capable devices found" in tail or "CUDA_ERROR" in tail:
                tail += "\n\n建议：更新 NVIDIA Studio Driver；确认电源模式为最佳性能；或暂时改用 CPU 解码验证输入格式。"
            if "Unrecognized option 'split_encode_mode'" in tail:
                tail += "\n\n当前 FFmpeg 太旧，不支持分帧参数。请关闭“多 NVENC 分帧”或升级 FFmpeg。"
            raise RuntimeError(tail)

    def _register_process(self, process: subprocess.Popen) -> None:
        with self.process_lock:
            self.processes.append(process)
        # A cancellation may arrive between the launch and registration.
        if self.cancel_event.is_set():
            self._terminate(process)

    def _unregister_process(self, process: subprocess.Popen) -> None:
        with self.process_lock:
            if process in self.processes:
                self.processes.remove(process)

    def _terminate(self, process: subprocess.Popen) -> None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _cancel(self) -> None:
        if not self.working:
            return
        self.cancel_event.set()
        self.progress_text_var.set("正在取消…")
        with self.process_lock:
            processes = list(self.processes)
        for process in processes:
            self._terminate(process)

    def _set_working(self, working: bool) -> None:
        self.working = working
        if working and self.progress_text_var.get() in {"就绪", "完成", "已取消"}:
            self.progress_var.set(0)
            self.progress_text_var.set("任务启动 0.0% · 0.0 FPS · 0x")
        self.export_button.configure(state="disabled" if working else "normal")
        self.cancel_button.configure(state="normal" if working else "disabled")
        if hasattr(self, "subtitle_generate_button"):
            self.subtitle_generate_button.configure(state="disabled" if working else "normal")
        self._update_join_preview_button()
        if working:
            # Preview decoding competes with the export's NVDEC bandwidth.
            controller = self.mpv
            if controller:
                def pause_preview() -> None:
                    try:
                        controller.command(["set_property", "pause", True])
                    except Exception as exc:
                        self.events.put(("log", f"暂停预览未成功：{exc}"))
                threading.Thread(target=pause_preview, daemon=True).start()
            self.after(250, self._poll_gpu)

    def _poll_gpu(self) -> None:
        if not self.working:
            return
        if vars(self).get('_selected_encoder_vendor','nvidia') != 'nvidia':
            self.after(1000, self._poll_gpu)
            return
        def worker() -> None:
            sample = read_gpu_sample(gpu_index=0)
            self.events.put(("gpu_sample", sample))
        threading.Thread(target=worker, daemon=True).start()
        self.after(1000, self._poll_gpu)

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if self._handle_studio_event(kind, payload):
                    continue
                if kind == "caps":
                    self.caps = payload  # type: ignore[assignment]
                    if self.media_pool_workspace is not None:
                        self.media_pool_workspace.on_capabilities()
                    codecs = ", ".join(sorted(available_codecs(self.caps))) or "无可用编码器"
                    self.ffmpeg_status_var.set(f"{Path(self.caps.ffmpeg_path).name if self.caps.ffmpeg_path else '未找到'}  |  {codecs}")
                    self._apply_gpu_profile()
                    ffmpeg_name = Path(self.caps.ffmpeg_path).name if self.caps.ffmpeg_path else "未找到 FFmpeg"
                    self.ffmpeg_chip.configure(text=f"●  {ffmpeg_name}", fg=GREEN if self.caps.ffmpeg_path else RED)
                    self._log(f"GPU: {self.caps.gpu_name}; Driver: {self.caps.driver_version}")
                    profile = self.caps.gpu_profile
                    self._log(
                        f"Profile: {profile.architecture}; {profile.form_factor}; NVENC gen "
                        f"{profile.nvenc_generation or 'unknown'} ×{profile.nvenc_engines}; "
                        f"NVDEC ×{profile.nvdec_engines}; support: "
                        f"{'native' if profile.in_supported_range else 'compatibility'}"
                    )
                    self._log(f"可用编码器: {codecs}; CUDA decode: {self.caps.cuda_decode}; scale_cuda: {self.caps.scale_cuda}; split encode: {sorted(self.caps.split_encode)}")
                    self._request_join_timeline_probe()
                elif kind == "media":
                    self.media = payload  # type: ignore[assignment]
                    m = self.media
                    self.media_var.set(f"{m.width}×{m.height}  {m.fps:.3f} fps  {m.video_codec}/{m.pixel_format}  {m.duration:.1f}s  音频 {m.audio_codec}")
                    self._log("已读取媒体：" + self.media_var.get())
                elif kind == "join_timeline_media":
                    key, media_items, audio_duration, failures = payload  # type: ignore[misc]
                    current_key = (tuple(self.editor_join_files), self.editor_audio_var.get().strip())
                    if key == current_key:
                        for path, info in media_items:
                            self.editor_join_media_cache[str(path)] = info
                        self.editor_join_audio_duration = max(0.0, float(audio_duration or 0.0))
                        self._draw_join_timeline()
                        if failures:
                            self.editor_join_timeline_hint_var.set(
                                "部分素材时长读取失败；可继续操作，导出前会再次校验。"
                            )
                            self._log("时间线素材读取提示：" + " | ".join(str(item) for item in failures))
                        else:
                            self.editor_join_timeline_hint_var.set(
                                "拖动顶部时间标尺横移 · Ctrl+滚轮缩放 · Shift+滚轮横移"
                            )
                elif kind == "editor_loaded":
                    if len(payload) == 4:
                        generation, controller, info, playback_backend = payload
                        if generation != self.editor_load_generation:
                            threading.Thread(target=controller.stop, daemon=True).start()
                            continue
                    else:
                        controller, info, playback_backend = payload  # legacy aligned preview
                    self.mpv = controller
                    self.editor_media = info
                    if self.lossless_workspace.state.source != info.path:
                        self.lossless_workspace.open_media(info.path)
                    self.after_idle(self._resize_editor_preview_surface)
                    self.editor_duration = float(info.duration or 0)
                    self.editor_current_time = 0.0
                    self.editor_timeline_var.set(0)
                    self.editor_time_var.set(f"{format_time(0)} / {format_time(self.editor_duration)}")
                    self.editor_seek_var.set(format_time(0))
                    self.editor_trim_in_var.set(format_time(0))
                    self.editor_trim_out_var.set(format_time(self.editor_duration))
                    source_path = Path(info.path)
                    self.editor_trim_output_var.set(str(source_path.with_name(f"{source_path.stem}_裁切{source_path.suffix}")))
                    self.editor_split_dir_var.set(str(source_path.parent / "Separated_AV"))
                    if not self.editor_proxy_dir_var.get().strip():
                        self.editor_proxy_dir_var.set(str(cache_dir('proxies')))
                    self.editor_status_var.set(
                        f"已载入：{info.width}×{info.height} · {info.fps:.3f} fps · {info.video_codec} · 音频 {info.audio_codec}"
                    )
                    hwdec = str(playback_backend.get("hwdec") or "GPU").upper()
                    interop = str(playback_backend.get("interop") or "GPU DIRECT")
                    self.mpv_status_label.configure(text=f"●  GPU {hwdec} / 硬件渲染", fg=GREEN)
                    self._log(
                        f"剪辑工具已载入：{info.path}; 监看后端：{hwdec}; "
                        f"interop: {interop}; renderer: {playback_backend.get('renderer')}"
                    )
                elif kind == "editor_player_state":
                    current, duration, _paused = payload  # type: ignore[misc]
                    self.editor_poll_busy = False
                    self.editor_current_time = max(0.0, float(current))
                    self.lossless_workspace.on_player_time(self.editor_current_time)
                    self.editor_duration = max(0.0, float(duration))
                    if not self.editor_dragging and self.editor_duration > 0:
                        self.editor_timeline_var.set(self.editor_current_time / self.editor_duration * 1000.0)
                    self.editor_time_var.set(
                        f"{format_time(self.editor_current_time)} / {format_time(self.editor_duration)}"
                    )
                    if self.editor_join_preview_context is not None:
                        preview_path, offset = self.editor_join_preview_context
                        if os.path.normcase(preview_path) == os.path.normcase(self.editor_video_var.get().strip()):
                            self._update_join_timeline_playhead(self.editor_current_time - float(offset))
                elif kind == "editor_poll_end":
                    self.editor_poll_busy = False
                elif kind == "editor_status":
                    self.editor_status_var.set(str(payload))
                elif kind == "alignment_done":
                    result = payload
                    self.editor_offset_var.set(f"{result.offset_seconds:.3f}")
                    quality = "匹配清晰" if result.confidence >= 0.35 else "匹配较弱，建议试听确认"
                    self.editor_status_var.set(
                        f"自动对齐完成：外部音频偏移 {result.offset_seconds:+.3f} 秒；"
                        f"相关度 {result.confidence:.3f}（{quality}）· {result.engine}引擎。"
                    )
                    self._log("音频自动对齐：" + self.editor_status_var.get())
                elif kind == "audio_sync_progress":
                    percent, phase = payload
                    self.progress_var.set(float(percent))
                    self.progress_text_var.set(f"音频同步 {float(percent):.1f}% · {phase}")
                    self.editor_status_var.set(str(phase))
                elif kind in {"join_alignment_done", "join_alignment_cached"}:
                    audio, pairs = payload  # type: ignore[misc]
                    self.editor_join_alignment_audio = str(audio)
                    self.editor_join_alignments = {str(path): result for path, result in pairs}
                    self._refresh_join_list()
                    weakest = min((result.confidence for _path, result in pairs), default=0.0)
                    self.editor_join_alignment_var.set(
                        f"已对齐 {len(pairs)} 段 · 最低可信度 {weakest:.3f}"
                    )
                    if kind == "join_alignment_done":
                        self._set_working(False)
                        self.progress_var.set(100)
                        self.progress_text_var.set("同步完成，已对齐到工程时间线")
                        self._fit_join_timeline()
                        if pairs:
                            self.editor_join_listbox.selection_clear(0, "end")
                            self.editor_join_listbox.selection_set(0)
                            self.editor_join_listbox.see(0)
                        self.editor_status_var.set(
                            "全部片段已分别定位到连续主音频；选择列表片段后点击“试听对齐结果”确认同步。"
                        )
                        self._log("演唱会多段音频对齐完成。")
                elif kind == "join_preview_started":
                    (
                        index, name, master_start, confidence, video_trim, leading_silence,
                        preview_start, preview_end, preview_path, offset,
                    ) = payload  # type: ignore[misc]
                    self.editor_join_preview_context = (str(preview_path), float(offset))
                    self._update_join_timeline_playhead(float(master_start))
                    if float(leading_silence) > 0.0005:
                        detail = f"保留开头 {format_time(float(leading_silence))}，期间主音频静音"
                    else:
                        detail = (
                            f"视频 {format_time(float(video_trim))} 对应主音频 "
                            f"{format_signed_time(float(master_start))}"
                        )
                    self.editor_status_var.set(
                        f"正在试听第 {index} 段 {name}：{detail} · 成片范围 "
                        f"{format_time(float(preview_start))}–{format_time(float(preview_end))} · "
                        f"可信度 {float(confidence):.3f}。"
                    )
                    self._log("多段音频对齐试听：" + self.editor_status_var.get())
                elif kind == "editor_frame_done":
                    self.editor_status_var.set(f"当前帧已保存：{payload}")
                    self._log(self.editor_status_var.get())
                    if messagebox.askyesno(APP_NAME, "截帧完成。是否打开图片所在文件夹？"):
                        self._open_folder(str(payload))
                elif kind == "editor_error":
                    self.editor_poll_busy = False
                    self.editor_status_var.set("操作失败")
                    self._log("剪辑工具错误：" + str(payload))
                    messagebox.showerror(APP_NAME, str(payload))
                elif kind == "editor_audio_error":
                    self._set_working(False)
                    self.progress_text_var.set("替换音频失败")
                    self.editor_status_var.set("替换音频失败")
                    self._log(str(payload))
                    messagebox.showerror(APP_NAME, str(payload))
                elif kind == "editor_audio_done":
                    self._set_working(False)
                    self.progress_var.set(100)
                    self.progress_text_var.set("替换音频完成")
                    self.editor_status_var.set(f"替换音频完成：{payload}")
                    self._log(self.editor_status_var.get())
                    if messagebox.askyesno(APP_NAME, "替换音频完成。是否打开输出文件所在文件夹？"):
                        self._open_folder(str(payload))
                elif kind == "editor_job_error":
                    self._set_working(False)
                    self.progress_text_var.set("剪辑导出失败")
                    self.editor_status_var.set("剪辑任务失败")
                    self._log(str(payload))
                    messagebox.showerror(APP_NAME, str(payload))
                elif kind == "editor_job_done":
                    self._set_working(False)
                    operation, outputs = payload  # type: ignore[misc]
                    self.progress_var.set(100)
                    self.progress_text_var.set(f"{operation}完成")
                    output_text = "；".join(str(path) for path in outputs)
                    self.editor_status_var.set(f"{operation}完成：{output_text}")
                    self._log(self.editor_status_var.get())
                    if messagebox.askyesno(APP_NAME, f"{operation}完成。是否打开输出文件夹？"):
                        self._open_folder(str(outputs[0]))
                elif kind == "editor_export_progress":
                    overall, label, task_percent, fps, speed = payload  # type: ignore[misc]
                    self.progress_var.set(float(overall))
                    fps_value = parse_fps_value(str(fps))
                    speed_text = str(speed).strip() or "--x"
                    self.progress_text_var.set(
                        f"{label} {float(overall):4.1f}% · 近3秒 {fps_value:.1f} FPS · {speed_text}"
                    )
                elif kind == "editor_join_parallel_init":
                    durations, workers, source_fps = payload  # type: ignore[misc]
                    self.editor_join_parallel_source_fps = float(source_fps or 0.0)
                    self.editor_join_chunk_state = {
                        index: (0.0, float(duration), 0.0, False)
                        for index, duration in enumerate(durations)
                    }
                    self.progress_var.set(0.0)
                    self.progress_text_var.set(
                        f"剪辑并行 {int(workers)} 路 0.0% · 等待首帧…"
                    )
                elif kind == "editor_join_parallel_progress":
                    index, seconds, duration, recent_fps, active, workers, job_count = payload  # type: ignore[misc]
                    self.editor_join_chunk_state[int(index)] = (
                        max(0.0, min(float(duration), float(seconds))),
                        float(duration), max(0.0, float(recent_fps)), bool(active),
                    )
                    total_duration = sum(item[1] for item in self.editor_join_chunk_state.values())
                    processed = sum(item[0] for item in self.editor_join_chunk_state.values())
                    encode_percent = min(100.0, processed / total_duration * 100.0) if total_duration else 0.0
                    combined_fps = sum(
                        item[2] for item in self.editor_join_chunk_state.values() if item[3]
                    )
                    combined_speed = (
                        combined_fps / self.editor_join_parallel_source_fps
                        if self.editor_join_parallel_source_fps > 0 else 0.0
                    )
                    finished = sum(
                        1 for item in self.editor_join_chunk_state.values()
                        if not item[3] and item[0] >= item[1] - 0.001
                    )
                    self.progress_var.set(encode_percent * 0.95)
                    self.progress_text_var.set(
                        f"剪辑并行 {int(workers)} 路 {encode_percent:4.1f}% · "
                        f"近3秒合计 {combined_fps:.1f} FPS · {combined_speed:.2f}x"
                    )
                    self.editor_status_var.set(
                        f"双路分段进度 {finished}/{int(job_count)}；完成后自动无损合并并加入主音频。"
                    )
                elif kind == "editor_proxy_live":
                    source, total, item_percent, fps, speed, parallel_count = payload  # type: ignore[misc]
                    self.editor_proxy_progress_state[source] = item_percent
                    percent = sum(self.editor_proxy_progress_state.values()) / max(1, total)
                    self.progress_var.set(percent)
                    fps_value = parse_fps_value(str(fps))
                    speed_text = str(speed).strip() or "--x"
                    self.progress_text_var.set(
                        f"批量代理 {percent:4.1f}% · 单路近3秒 {fps_value:.1f} FPS · {speed_text} · 同时 {parallel_count} 路"
                    )
                    self.editor_status_var.set(
                        f"正在代理 {Path(source).name}：{item_percent:4.1f}%"
                    )
                elif kind == "editor_proxy_progress":
                    completed, total, source, succeeded, parallel_count = payload  # type: ignore[misc]
                    self.editor_proxy_progress_state[source] = 100.0
                    percent = sum(self.editor_proxy_progress_state.values()) / max(1, total)
                    source_name = Path(source).name
                    result = "完成" if succeeded else "失败"
                    self.progress_var.set(percent)
                    self.progress_text_var.set(
                        f"批量代理 {completed}/{total} · 同时 {parallel_count} 路 · {source_name} {result}"
                    )
                    self.editor_status_var.set(f"代理进度 {completed}/{total}：{source_name} {result}")
                    self._log(self.editor_status_var.get())
                elif kind == "editor_proxy_batch_done":
                    self._set_working(False)
                    successful, failures, total, output_dir = payload  # type: ignore[misc]
                    self.progress_var.set(100)
                    self.progress_text_var.set(f"批量代理完成 · 成功 {len(successful)}/{total}")
                    self.editor_status_var.set(
                        f"批量代理完成：成功 {len(successful)} 个，失败 {len(failures)} 个；{output_dir}"
                    )
                    self._log(self.editor_status_var.get())
                    if failures:
                        lines = [f"• {name}：{detail[-500:]}" for name, detail in failures[:6]]
                        if len(failures) > 6:
                            lines.append(f"另有 {len(failures) - 6} 个失败任务，请查看命令与日志。")
                        messagebox.showwarning(
                            APP_NAME,
                            f"批量代理已结束：成功 {len(successful)} 个，失败 {len(failures)} 个。\n\n"
                            + "\n".join(lines),
                        )
                    if successful and messagebox.askyesno(APP_NAME, "批量代理完成。是否打开 ProxyMedia 文件夹？"):
                        self._open_folder(successful[0])
                elif kind in {"error", "job_error"}:
                    self._set_working(False)
                    self.progress_text_var.set("失败")
                    self._log("错误：" + str(payload))
                    messagebox.showerror(APP_NAME, str(payload))
                elif kind == "log":
                    self._log(str(payload))
                elif kind == "progress":
                    percent, fps, speed = payload  # type: ignore[misc]
                    self.progress_var.set(percent)
                    self.progress_text_var.set(
                        f"{percent:5.1f}% · {parse_fps_value(str(fps)):.1f} FPS · {speed}"
                    )
                elif kind == "chunk_progress":
                    index, percent, fps, speed, count = payload  # type: ignore[misc]
                    self.chunk_progress_state[index] = (
                        percent, parse_fps_value(str(fps)), parse_speed_multiplier(speed),
                    )
                    combined_percent = sum(item[0] for item in self.chunk_progress_state.values()) / count
                    combined_fps = sum(item[1] for item in self.chunk_progress_state.values())
                    combined_speed = sum(item[2] for item in self.chunk_progress_state.values())
                    self.progress_var.set(combined_percent * 0.9)
                    self.progress_text_var.set(
                        f"并行{count}路 {combined_percent:4.1f}% · {combined_fps:.1f} FPS · {combined_speed:.2f}x"
                    )
                elif kind == "mux_progress":
                    percent, fps, speed = payload  # type: ignore[misc]
                    self.progress_var.set(90 + percent * 0.1)
                    self.progress_text_var.set(
                        f"封装音频 {percent:4.1f}% · {parse_fps_value(str(fps)):.1f} FPS · {speed}"
                    )
                elif kind == "gpu_sample":
                    if vars(self).get('_selected_encoder_vendor','nvidia') != 'nvidia':
                        continue
                    sample = payload  # type: ignore[assignment]
                    if sample:
                        gpu_value = float(sample.get("utilization.gpu", "0") or 0)
                        encode_value = float(sample.get("utilization.encoder", "0") or 0)
                        decode_value = float(sample.get("utilization.decoder", "0") or 0)
                        memory_value = float(sample.get("memory.used", "0") or 0)
                        self.gpu_meter_var.set(gpu_value)
                        self.encode_meter_var.set(encode_value)
                        self.decode_meter_var.set(decode_value)
                        total_memory = float(self.caps.gpu_memory_mb or 24_000)
                        self.memory_meter_var.set(min(100.0, memory_value / total_memory * 100.0))
                        self.gpu_metric_var.set(f"{gpu_value:.0f}%")
                        self.encode_metric_var.set(f"{encode_value:.0f}%")
                        self.decode_metric_var.set(f"{decode_value:.0f}%")
                        self.memory_metric_var.set(f"{memory_value:.0f} MB")
                        self.gpu_detail_var.set(f"{sample.get('temperature.gpu', '--')}°C · {sample.get('power.draw', '--')} W")
                        self.monitor_var.set(
                            f"GPU {sample.get('utilization.gpu', '--')}%   编码 {sample.get('utilization.encoder', '--')}%   "
                            f"解码 {sample.get('utilization.decoder', '--')}%   显存 {sample.get('memory.used', '----')} MB   "
                            f"温度 {sample.get('temperature.gpu', '--')}°C   功耗 {sample.get('power.draw', '--')} W"
                        )
                elif kind == "done":
                    self._set_working(False)
                    self.progress_var.set(100)
                    self.progress_text_var.set("完成")
                    self._log(f"导出完成：{payload}")
                    if messagebox.askyesno(APP_NAME, "导出完成。是否打开输出文件所在文件夹？"):
                        self._open_folder(str(payload))
                elif kind == "cancelled":
                    self._set_working(False)
                    self.progress_text_var.set("已取消")
                    self._log("导出已取消。")
                elif kind == "benchmark_done":
                    self._set_working(False)
                    elapsed, ratio = payload  # type: ignore[misc]
                    self.progress_var.set(100)
                    self.progress_text_var.set("测试完成")
                    self._log(f"性能测试：耗时 {elapsed:.2f}s，约 {ratio:.2f}× 实时。")
                    messagebox.showinfo(APP_NAME, f"10 秒测试完成\n耗时：{elapsed:.2f} 秒\n速度：约 {ratio:.2f}× 实时\n\n请结合窗口底部的“编码/解码”峰值判断瓶颈。")
        except queue.Empty:
            pass
        self.after(80, self._drain_events)

    def _open_folder(self, output: str) -> None:
        folder = str(Path(output).resolve().parent)
        if os.name == "nt":
            subprocess.Popen(["explorer", folder])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])

    def _close(self) -> None:
        if self.working and not messagebox.askyesno(APP_NAME, "任务仍在进行。确定取消并退出吗？"):
            return
        if not self._project_confirm_discard():
            return
        self._write_project_autosave()
        self._closing = True
        self._invalidate_editor_player()
        self.lossless_workspace.close()
        if self.media_pool_workspace is not None:
            self.media_pool_workspace.close()
        self.multicam_workspace.close()
        self._cancel()
        if self.mpv:
            self.mpv.stop()
        self.destroy()


if __name__ == "__main__":
    if "--headless-export" in sys.argv:
        from okk_cli import run_headless_cli

        raise SystemExit(run_headless_cli(sys.argv[1:], app_directory()))
    enable_dpi_awareness()
    ExporterApp().mainloop()
