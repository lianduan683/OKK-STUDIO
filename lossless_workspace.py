from __future__ import annotations

import os
import math
import shutil
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk, font as tkfont
from typing import Callable, Iterable, Sequence

import lossless_detect as detect
import lossless_edl as edl
import lossless_ffmpeg as lf
from lossless_core import CutSegment, HistoryError, KeyframeIndex, SegmentModel, clone_segment
from ui_theme import BG, BORDER, GREEN, INPUT, MONO_FONT, MUTED, PANEL, PANEL_ALT, PURPLE, TEXT, UI_FONT


APP_TITLE = "RTX 视频渲染工坊"
MEDIA_TYPES = [
    ("视频与音频", "*.mp4 *.mov *.mkv *.mxf *.avi *.webm *.mts *.m2ts *.m4v *.wav *.flac *.mp3 *.m4a *.aac *.ogg *.opus *.wma *.aif *.aiff *.mka"),
    ("所有文件", "*.*"),
]
CUT_TYPES = [
    ("LosslessCut 工程", "*.llc"), ("CSV", "*.csv"), ("MPlayer EDL", "*.edl"),
    ("SubRip", "*.srt"), ("YouTube 章节", "*.txt *.chapters"), ("所有文件", "*.*"),
]


def _time(value: float) -> str:
    return edl.format_timecode(max(0.0, float(value)))


def _tags_text(tags: dict[str, str]) -> str:
    return ";".join(f"{key}={value}" if value else key for key, value in tags.items())


def _parse_tags(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in text.replace("|", ";").split(";"):
        item = item.strip()
        if not item:
            continue
        key, separator, value = item.partition("=")
        key = key.strip()
        if not key:
            raise ValueError("标签键不能为空。")
        result[key] = value.strip() if separator else ""
    return result


def _safe_remove_tree(path: str) -> None:
    if path:
        shutil.rmtree(path, ignore_errors=True)


@dataclass
class LosslessWorkspaceState:
    model: SegmentModel = field(default_factory=SegmentModel)
    source: str = ""
    probe: lf.MediaProbe | None = None
    keyframes: lf.KeyframeIndex | None = None
    current_time: float = 0.0
    active_id: str | None = None
    output_dir: str = ""
    merged_output: str = ""
    output_mode: str = "separate"
    extension: str = ".mkv"
    name_template: str = lf.DEFAULT_SEGMENT_TEMPLATE
    overwrite: bool = False
    snap_start: bool = True
    auto_chapters: bool = False
    auto_save: bool = True
    track_selection: dict[int, bool] = field(default_factory=dict)
    external_tracks: list[lf.ExternalTrack] = field(default_factory=list)
    metadata_title: str = ""
    metadata_language: str = ""
    metadata_tags: str = ""
    rotation: str = "不修改"
    hvc1: bool = False
    detection_kind: str = ""
    detection_ranges: list[detect.TimeRange] = field(default_factory=list)
    detection_cuts: list[float] = field(default_factory=list)
    generation: int = 0
    edit_generation: int = 0
    llc_save_error: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.probe.duration if self.probe else self.model.media_duration or 0.0)

    @property
    def keyframe_times(self) -> tuple[float, ...]:
        return self.keyframes.times if self.keyframes else ()

    def active(self) -> CutSegment:
        if self.active_id is None:
            raise ValueError("请先选择一个片段。")
        return self.model.get(self.active_id)

    def replace_segments(self, segments: Iterable[CutSegment], active_id: str | None = None) -> None:
        items = list(segments)
        self.edit_generation += 1
        self.model.history.reset(items)
        if self.duration > 0:
            self.model.set_media_duration(self.duration)
        self.active_id = active_id or (items[0].id if items else None)

    def llc_candidates(self) -> tuple[Path, ...]:
        if not self.source:
            return ()
        source = Path(self.source)
        return source.with_suffix(".llc"), Path(str(source) + ".llc")


class LosslessWorkspace:
    """Dock-rebuildable LosslessCut workflow; all long jobs use the host queue."""

    def __init__(self, app) -> None:
        self.app = app
        self.state = LosslessWorkspaceState()
        self.view: tk.Widget | None = None
        self.closed = False
        self.playback_items: tuple = ()
        self.playback_index = -1
        self.playback_loop = False
        self._redraw_after: str | None = None
        self._timeline_draw_after: str | None = None
        self._timeline_zoom = 1.0
        self._timeline_selection: set[str] = set()
        self._timeline_gesture = None
        self._timeline_preview = None
        self._timeline_items: dict[int, str] = {}
        self._timeline_syncing_selection = False
        self._track_vars: dict[int, tk.BooleanVar] = {}
        self._job_counter = 0
        self._active_job = 0
        self._create_variables()

    def _create_variables(self) -> None:
        root = getattr(self.app, "_root", self.app)
        if callable(root):
            root = root()
        self.source_var = tk.StringVar(root, value="尚未打开素材")
        self.summary_var = tk.StringVar(root, value="打开视频或导入片段工程开始")
        self.time_var = tk.StringVar(root, value=_time(0))
        self.start_var = tk.StringVar(root, value=_time(0))
        self.end_var = tk.StringVar(root, value=_time(0))
        self.name_var = tk.StringVar(root)
        self.tags_var = tk.StringVar(root)
        self.selected_var = tk.BooleanVar(root, value=True)
        self.mode_var = tk.StringVar(root, value="separate")
        self.output_dir_var = tk.StringVar(root)
        self.merged_output_var = tk.StringVar(root)
        self.extension_var = tk.StringVar(root, value=".mkv")
        self.template_var = tk.StringVar(root, value=lf.DEFAULT_SEGMENT_TEMPLATE)
        self.overwrite_var = tk.BooleanVar(root, value=False)
        self.snap_var = tk.BooleanVar(root, value=True)
        self.chapters_var = tk.BooleanVar(root, value=False)
        self.auto_save_var = tk.BooleanVar(root, value=True)
        self.title_var = tk.StringVar(root)
        self.language_var = tk.StringVar(root)
        self.meta_tags_var = tk.StringVar(root)
        self.rotation_var = tk.StringVar(root, value="不修改")
        self.hvc1_var = tk.BooleanVar(root, value=False)
        self.external_path_var = tk.StringVar(root)
        self.external_kind_var = tk.StringVar(root, value="audio")
        self.external_language_var = tk.StringVar(root)
        self.external_title_var = tk.StringVar(root)
        self.external_default_var = tk.BooleanVar(root, value=False)
        self.external_forced_var = tk.BooleanVar(root, value=False)
        self.detect_status_var = tk.StringVar(root, value="检测后可添加到片段、扣除区间或按场景拆分")
        self.black_duration_var = tk.StringVar(root, value="0.3")
        self.black_picture_var = tk.StringVar(root, value="0.98")
        self.black_pixel_var = tk.StringVar(root, value="0.10")
        self.silence_db_var = tk.StringVar(root, value="-30")
        self.silence_duration_var = tk.StringVar(root, value="0.5")
        self.scene_threshold_var = tk.StringVar(root, value="0.4")
        self.utility_source_var = tk.StringVar(root)
        self.utility_output_var = tk.StringVar(root)
        self.loop_count_var = tk.StringVar(root, value="1")
        self.loop_duration_var = tk.StringVar(root)
        self.fps_var = tk.StringVar(root, value="30")
        self.decimate_mode_var = tk.StringVar(root, value="copy")
        self.frame_interval_var = tk.StringVar(root, value="1.0")
        self.frame_mode_var = tk.StringVar(root, value="每 N 秒")
        self.frame_format_var = tk.StringVar(root, value="jpg")

    def build(self, parent: tk.Widget) -> None:
        self.closed = False
        self.view = parent
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        notebook = ttk.Notebook(parent)
        notebook.grid(row=0, column=0, sticky="nsew")
        self.notebook = notebook
        pages = {}
        for key, title in (
            ("edit", "片段与时间线"), ("output", "输出"), ("tracks", "轨道与元数据"),
            ("detect", "智能检测"), ("tools", "帧与实用工具"),
        ):
            page = tk.Frame(notebook, bg=PANEL_ALT)
            notebook.add(page, text=f"  {title}  ")
            pages[key] = page
        self._build_edit_page(pages["edit"])
        self._build_output_page(pages["output"])
        self._build_tracks_page(pages["tracks"])
        self._build_detect_page(pages["detect"])
        self._build_tools_page(pages["tools"])
        selected = getattr(self.app, "lossless_subtab", 0)
        notebook.select(min(selected, len(notebook.tabs()) - 1))
        notebook.bind("<<NotebookTabChanged>>", self._subtab_changed)
        self.refresh()

    def _subtab_changed(self, _event=None) -> None:
        if hasattr(self, "notebook"):
            self.app.lossless_subtab = self.notebook.index(self.notebook.select())

    def unmount(self) -> None:
        self._timeline_cancel()
        self._cancel_timeline_draw()
        self.stop_playback(pause=True)
        if self._redraw_after:
            try:
                self.app.after_cancel(self._redraw_after)
            except tk.TclError:
                pass
            self._redraw_after = None
        self.view = None

    def close(self) -> None:
        self.closed = True
        self.state.generation += 1
        self.unmount()

    def reset_session(self) -> None:
        """Drop every trace of the previous media/project before a new one."""
        self._timeline_cancel()
        self._timeline_selection.clear()
        self._timeline_zoom = 1.0
        self.stop_playback(pause=True)
        state = self.state
        state.generation += 1
        state.edit_generation += 1
        state.llc_save_error = ""
        state.source = ""
        state.probe = None
        state.keyframes = None
        state.model = SegmentModel()
        state.current_time = 0.0
        state.active_id = None
        state.output_dir = ""
        state.merged_output = ""
        state.track_selection.clear()
        state.external_tracks.clear()
        state.detection_kind = ""
        state.detection_ranges.clear()
        state.detection_cuts.clear()
        self._track_vars.clear()
        self.source_var.set("尚未打开素材")
        self.summary_var.set("打开视频或导入片段工程开始")
        self.time_var.set(_time(0))
        self.start_var.set(_time(0))
        self.end_var.set(_time(0))
        self.name_var.set("")
        self.tags_var.set("")
        self.utility_source_var.set("")
        self.utility_output_var.set("")
        self.detect_status_var.set("检测后可添加到片段、扣除区间或按场景拆分")
        self._refresh_detection()
        self.refresh()

    def sync_options(self) -> LosslessWorkspaceState:
        """Copy live control values into ``state`` for project collection.

        Safe to call before :meth:`build` and after :meth:`unmount`; a
        destroyed Tk root is ignored instead of failing project saving.
        Returns the (now current) state so callers can collect it directly.
        """
        self._sync_options_from_vars()
        return self.state

    def _begin_job(self) -> tuple[int, int]:
        """Allocate a generation/job id pair for one async lossless task."""
        self._job_counter += 1
        self._active_job = self._job_counter
        return self.state.generation, self._active_job

    def _job_is_current(self, generation: int, job_id: int) -> bool:
        """True when *job_id* still owns the busy flag.

        ``generation`` is accepted for call-site symmetry; media/session
        changes are handled by the callers so a finished task can always
        release the busy flag without overwriting fresher media state.
        """
        return not self.closed and bool(job_id) and job_id == self._active_job

    def _alive_view(self, widget: tk.Widget | None = None) -> bool:
        target = widget or self.view
        try:
            return bool(target is not None and target.winfo_exists() and not self.closed)
        except tk.TclError:
            return False

    def _error(self, detail: object) -> None:
        self.app._log(f"无损分段：{detail}")
        messagebox.showerror(APP_TITLE, str(detail), parent=self.app)

    def _require_source(self) -> str:
        source = self.state.source
        if not source or not Path(source).is_file():
            raise ValueError("请先打开有效的视频或音频素材。")
        return source

    def _ffmpeg(self) -> str:
        path = self.app.caps.ffmpeg_path
        if not path:
            raise ValueError("未找到 ffmpeg.exe，请放入 bin 文件夹。")
        return path

    def _ffprobe(self) -> str:
        path = self.app.caps.ffprobe_path
        if not path:
            raise ValueError("未找到 ffprobe.exe，请放入 bin 文件夹。")
        return path

    def open_dialog(self) -> None:
        path = filedialog.askopenfilename(title="打开无损分段素材", filetypes=MEDIA_TYPES, parent=self.app)
        if path:
            self.app._load_editor_video(path)

    def open_media(self, path: str, *, probe_keyframes: bool = True, preserve: bool = False) -> None:
        if not path or not Path(path).is_file():
            return
        self._timeline_cancel()
        self.stop_playback(pause=True)
        state = self.state
        state.generation += 1
        generation = state.generation
        source = str(Path(path).resolve())
        state.source = source
        state.probe = None
        state.keyframes = None
        if not preserve:
            self._timeline_selection.clear()
            self._timeline_zoom = 1.0
            state.current_time = 0.0
            state.active_id = None
            state.llc_save_error = ""
            state.track_selection.clear()
            state.external_tracks.clear()
            state.detection_ranges.clear()
            state.detection_cuts.clear()
            state.model = SegmentModel()
            state.output_dir = str(Path(path).resolve().parent / "Lossless")
            state.merged_output = str(Path(path).resolve().with_name(f"{Path(path).stem}_merged.mkv"))
            self.utility_source_var.set(state.source)
            self.utility_output_var.set(str(Path(path).resolve().with_name(f"{Path(path).stem}_utility.mkv")))
            self._sync_options_to_vars()
        # Snapshot *before* the worker starts: edits made while ffprobe runs
        # must win over this stale copy (edit generation), and the sidecar
        # candidates must belong to the source being probed.
        snapshot_segments = list(state.model.segments) if preserve else None
        snapshot_edit = state.edit_generation
        candidates = () if preserve else state.llc_candidates()
        self.source_var.set(state.source)
        self.summary_var.set("正在重新读取轨道与关键帧…" if preserve else "正在读取轨道、时长和旁路 LLC…")
        self.refresh()

        def worker() -> None:
            try:
                ffprobe = self._ffprobe()
                probe = lf.probe_media(ffprobe, source)
                keyframes = None
                keyframe_error = ""
                if probe_keyframes and probe.primary_video is not None:
                    try:
                        keyframes = lf.probe_keyframes(ffprobe, source)
                    except Exception as exc:
                        keyframe_error = str(exc)
                loaded_segments: list[CutSegment] | None = (
                    list(snapshot_segments) if snapshot_segments is not None else None
                )
                llc_path = None
                load_error = ""
                for candidate in candidates:
                    if not candidate.is_file():
                        continue
                    try:
                        project = edl.load_document(candidate, "llc", media_duration=probe.duration or None)
                        if Path(project.media_file_name).name == Path(source).name:
                            loaded_segments = list(project.segments)
                            llc_path = candidate
                            break
                    except Exception as exc:
                        load_error = f"{candidate.name}: {exc}"
                if loaded_segments is None:
                    loaded_segments = [CutSegment(start=0.0, end=probe.duration, name="完整素材", selected=True)] if probe.duration > 0 else []
                self.app.events.put(("lossless_media", (
                    generation, probe, keyframes, loaded_segments, llc_path,
                    keyframe_error, load_error, preserve, snapshot_edit,
                )))
            except Exception as exc:
                self.app.events.put(("lossless_media_error", (generation, str(exc))))

        threading.Thread(target=worker, daemon=True, name="lossless_probe").start()

    def handle_media(self, payload) -> None:
        values = tuple(payload)
        if len(values) == 8:
            (generation, probe, keyframes, segments, llc_path, keyframe_error,
             load_error, preserve) = values
            snapshot_edit = None
        else:
            (generation, probe, keyframes, segments, llc_path, keyframe_error,
             load_error, preserve, snapshot_edit) = values
        if self.closed or generation != self.state.generation:
            return
        active_id = self.state.active_id
        edited = snapshot_edit is not None and snapshot_edit != self.state.edit_generation
        self.state.probe = probe
        self.state.keyframes = keyframes
        if edited:
            # The user changed the cut list while ffprobe was running.  Keep
            # that newer model and only refresh media metadata.
            segments = list(self.state.model.segments)
            if probe.duration > 0:
                try:
                    self.state.model.set_media_duration(probe.duration)
                except ValueError as exc:
                    self.app._log(f"无损分段时长更新提示：{exc}")
            if active_id is None or all(item.id != active_id for item in segments):
                active_id = segments[0].id if segments else None
        else:
            self.state.model = SegmentModel(segments, media_duration=probe.duration or None)
            active_id = active_id if preserve and any(item.id == active_id for item in segments) else (segments[0].id if segments else None)
        self.state.active_id = active_id
        if not preserve:
            self.state.track_selection = {stream.index: True for stream in probe.streams}
        else:
            known = {stream.index for stream in probe.streams}
            self.state.track_selection = {index: value for index, value in self.state.track_selection.items() if index in known}
            for index in known:
                self.state.track_selection.setdefault(index, True)
        if self.app.caps.ffmpeg_path and keyframe_error:
            self.app._log(f"无损分段关键帧读取提示：{keyframe_error}")
        if load_error:
            self.app._log(f"旁路 LLC 未加载：{load_error}")
        if edited:
            detail = "保留探测期间的新编辑"
        else:
            detail = "轨道与关键帧已刷新" if preserve else (f"已自动加载 {llc_path.name}" if llc_path else "已创建完整素材片段")
        self.summary_var.set(f"{len(segments)} 段 · {_time(probe.duration)} · {detail}")
        self.refresh()

    def handle_media_error(self, payload) -> None:
        generation, detail = payload
        if generation == self.state.generation and not self.closed:
            self.summary_var.set("素材读取失败")
            self._error(detail)

    def _sync_options_to_vars(self) -> None:
        state = self.state
        self.mode_var.set(state.output_mode)
        self.output_dir_var.set(state.output_dir)
        self.merged_output_var.set(state.merged_output)
        self.extension_var.set(state.extension)
        self.template_var.set(state.name_template)
        self.overwrite_var.set(state.overwrite)
        self.snap_var.set(state.snap_start)
        self.chapters_var.set(state.auto_chapters)
        self.auto_save_var.set(state.auto_save)
        self.title_var.set(state.metadata_title)
        self.language_var.set(state.metadata_language)
        self.meta_tags_var.set(state.metadata_tags)
        self.rotation_var.set(state.rotation)
        self.hvc1_var.set(state.hvc1)

    def _sync_options_from_vars(self) -> None:
        state = self.state
        try:
            state.output_mode = self.mode_var.get()
            state.output_dir = self.output_dir_var.get().strip()
            state.merged_output = self.merged_output_var.get().strip()
            state.extension = self.extension_var.get().strip() or ".mkv"
            state.name_template = self.template_var.get()
            state.overwrite = bool(self.overwrite_var.get())
            state.snap_start = bool(self.snap_var.get())
            state.auto_chapters = bool(self.chapters_var.get())
            state.auto_save = bool(self.auto_save_var.get())
            state.metadata_title = self.title_var.get()
            state.metadata_language = self.language_var.get().strip()
            state.metadata_tags = self.meta_tags_var.get()
            state.rotation = self.rotation_var.get()
            state.hvc1 = bool(self.hvc1_var.get())
        except tk.TclError:
            # Tk root already gone (application shutdown); keep last values.
            return

    def _changed(self, *, save: bool = True) -> None:
        self.state.edit_generation += 1
        if save and self.state.auto_save:
            try:
                self.save_llc(automatic=True)
            except Exception as exc:
                self._set_llc_error(exc)
        self.refresh()

    def _set_llc_error(self, detail: object) -> None:
        message = str(detail)
        self.state.llc_save_error = message[:300]
        self.app._log(f"自动保存 LLC 失败：{message}")
        try:
            self.summary_var.set(f"自动保存 LLC 失败：{message[:300]}")
        except tk.TclError:
            pass

    def refresh(self) -> None:
        if not self._alive_view():
            return
        self._refresh_list()
        self._refresh_active_fields()
        self._refresh_tracks()
        self._refresh_external()
        self._draw_timeline()
        summary = (
            f"{len(self.state.model.segments)} 段 · 选中 {len(self.state.model.selected_clips)} 段 · "
            f"合计 {_time(self.state.model.total_duration)} / 素材 {_time(self.state.duration)}"
        )
        if self.state.llc_save_error:
            summary += f" · 自动保存 LLC 失败：{self.state.llc_save_error}"
        self.summary_var.set(summary)

    def _refresh_list(self) -> None:
        if not hasattr(self, "segment_tree"):
            return
        tree = self.segment_tree
        existing = {item.id for item in self.state.model.segments}
        selected_ids = self._timeline_selection & existing
        if self.state.active_id not in existing:
            self.state.active_id = None
        if not selected_ids and self.state.active_id:
            selected_ids = {self.state.active_id}
        self._timeline_selection = selected_ids
        tree.delete(*tree.get_children())
        for index, segment in enumerate(self.state.model.segments, 1):
            marker = "标记" if segment.is_marker else _time(segment.duration)
            tree.insert("", "end", iid=segment.id, values=(
                "✓" if segment.selected else "", index, segment.name or f"片段 {index}",
                _time(segment.start), "" if segment.end is None else _time(segment.end), marker,
                _tags_text(segment.tags),
            ))
        selected_ids = [item for item in tree.get_children() if item in selected_ids]
        if selected_ids:
            tree.selection_set(*selected_ids)
        if self.state.active_id and tree.exists(self.state.active_id):
            tree.focus(self.state.active_id)
            tree.see(self.state.active_id)

    def _refresh_active_fields(self) -> None:
        try:
            segment = self.state.active()
        except (ValueError, KeyError):
            return
        self.start_var.set(_time(segment.start))
        self.end_var.set("" if segment.end is None else _time(segment.end))
        self.name_var.set(segment.name)
        self.tags_var.set(_tags_text(segment.tags))
        self.selected_var.set(segment.selected)
        self.time_var.set(_time(self.state.current_time))

    def _refresh_tracks(self) -> None:
        if not hasattr(self, "track_tree"):
            return
        tree = self.track_tree
        tree.delete(*tree.get_children())
        probe = self.state.probe
        if not probe:
            return
        for stream in probe.streams:
            selected = self.state.track_selection.get(stream.index, True)
            detail = f"{stream.width}×{stream.height}" if stream.is_video else (
                f"{stream.channels}ch {stream.sample_rate}Hz" if stream.is_audio else stream.title
            )
            tree.insert("", "end", iid=str(stream.index), values=(
                "✓" if selected else "", stream.index, stream.codec_type, stream.codec_name,
                stream.language, detail,
            ))
        self._track_vars = {}

    def _refresh_external(self) -> None:
        if not hasattr(self, "external_tree"):
            return
        self.external_tree.delete(*self.external_tree.get_children())
        for index, track in enumerate(self.state.external_tracks):
            self.external_tree.insert("", "end", iid=str(index), values=(
                track.kind, Path(track.path).name, track.language or "", track.title or "",
            ))

    def _build_edit_page(self, page: tk.Frame) -> None:
        page.columnconfigure(0, weight=1)
        page.rowconfigure(3, weight=1)
        header = tk.Frame(page, bg=PANEL_ALT)
        header.grid(row=0, column=0, sticky="ew", padx=8, pady=6)
        ttk.Button(header, text="打开素材", command=self.open_dialog).pack(side="left")
        ttk.Button(header, text="使用当前剪辑素材", command=self.use_current).pack(side="left", padx=5)
        ttk.Button(header, text="导入", command=self.import_segments).pack(side="left", padx=5)
        ttk.Button(header, text="导出", command=self.export_segments).pack(side="left")
        ttk.Button(header, text="保存 LLC", command=self.save_llc).pack(side="left", padx=5)
        tk.Checkbutton(header, text="自动保存旁路 LLC", variable=self.auto_save_var,
                       command=self._auto_save_changed, bg=PANEL_ALT, fg=TEXT, selectcolor=INPUT,
                       activebackground=PANEL_ALT, activeforeground=TEXT).pack(side="right")
        tk.Label(page, textvariable=self.summary_var, bg=PANEL_ALT, fg=GREEN,
                 font=(UI_FONT, 9), anchor="w").grid(row=1, column=0, sticky="ew", padx=8)
        timeline_holder = tk.Frame(page, bg=PANEL_ALT)
        timeline_holder.grid(row=2, column=0, sticky="ew", padx=8, pady=5)
        canvas = tk.Canvas(timeline_holder, height=108, bg="#12101c", highlightthickness=1,
                           highlightbackground=BORDER, cursor="hand2")
        canvas.pack(fill="x", expand=True)
        scroll = ttk.Scrollbar(timeline_holder, orient="horizontal", command=self._timeline_scroll)
        scroll.pack(fill="x")
        canvas.configure(xscrollcommand=scroll.set)
        canvas.bind("<Configure>", lambda _event: self._queue_timeline_draw())
        canvas.bind("<ButtonPress-1>", self._timeline_click)
        canvas.bind("<B1-Motion>", self._timeline_motion)
        canvas.bind("<ButtonRelease-1>", self._timeline_release)
        canvas.bind("<Button-3>", self._timeline_context)
        canvas.bind("<Control-MouseWheel>", self._timeline_zoom_wheel)
        canvas.bind("<MouseWheel>", self._timeline_wheel)
        canvas.bind("<Shift-MouseWheel>", self._timeline_wheel)
        canvas.bind("<ButtonPress-2>", self._timeline_pan_press)
        canvas.bind("<B2-Motion>", self._timeline_pan_motion)
        for sequence in ('<Delete>', '<BackSpace>', '<Escape>', '<Control-a>', '<Control-z>', '<Control-y>'):
            canvas.bind(sequence, self._timeline_key)
        self.timeline = canvas
        body = ttk.Panedwindow(page, orient="horizontal")
        body.grid(row=3, column=0, sticky="nsew", padx=8, pady=4)
        left = tk.Frame(body, bg=PANEL_ALT)
        right = self._scrollable(body)
        body.add(left, weight=3)
        body.add(right, weight=2)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        columns = ("selected", "index", "name", "start", "end", "duration", "tags")
        tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="extended")
        headings = {"selected": "选", "index": "#", "name": "名称", "start": "入点", "end": "出点", "duration": "时长", "tags": "标签"}
        widths = {"selected": 34, "index": 38, "name": 120, "start": 100, "end": 100, "duration": 90, "tags": 110}
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], minwidth=32, stretch=column in {"name", "tags"})
        scroll = ttk.Scrollbar(left, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        tree.bind("<<TreeviewSelect>>", self._list_selected)
        tree.bind("<Double-Button-1>", lambda _event: self.play_current_loop())
        for sequence in ('<Delete>', '<BackSpace>', '<Escape>', '<Control-a>', '<Control-z>', '<Control-y>'):
            tree.bind(sequence, self._timeline_key)
        self.segment_tree = tree
        self._build_editor_controls(right)
        transport = tk.Frame(page, bg=PANEL_ALT)
        transport.grid(row=4, column=0, sticky="ew", padx=8, pady=6)
        for text, command in (
            ("上一关键帧", lambda: self.step_keyframe(-1)), ("下一关键帧", lambda: self.step_keyframe(1)),
            ("吸附当前段", self.snap_active), ("定位", self.seek_from_entry),
            ("循环当前段", self.play_current_loop), ("顺序播放选中", self.play_selected_sequence),
            ("停止", self.stop_playback),
        ):
            ttk.Button(transport, text=text, command=command).pack(side="left", padx=2)
        tk.Entry(transport, textvariable=self.time_var, width=15, bg=INPUT, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=(MONO_FONT, 10)).pack(side="right", ipady=4)

    def _scrollable(self, parent: tk.Widget) -> tk.Frame:
        holder = tk.Frame(parent, bg=PANEL_ALT)
        canvas = tk.Canvas(holder, bg=PANEL_ALT, highlightthickness=0, width=430)
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        content = tk.Frame(canvas, bg=PANEL_ALT)
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        holder.content = content  # type: ignore[attr-defined]
        return holder

    def _build_editor_controls(self, holder: tk.Frame) -> None:
        panel = holder.content  # type: ignore[attr-defined]
        for label, variable in (("入点", self.start_var), ("出点", self.end_var), ("名称", self.name_var), ("标签（键=值;标签）", self.tags_var)):
            tk.Label(panel, text=label, bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 9, "bold")).pack(anchor="w")
            tk.Entry(panel, textvariable=variable, bg=INPUT, fg=TEXT, insertbackground=TEXT,
                     relief="flat", font=(MONO_FONT if label in {"入点", "出点"} else UI_FONT, 10)).pack(fill="x", ipady=5, pady=(2, 5))
        tk.Checkbutton(panel, text="选中此段用于导出/顺序播放", variable=self.selected_var,
                       bg=PANEL_ALT, fg=TEXT, selectcolor=INPUT, activebackground=PANEL_ALT,
                       activeforeground=TEXT).pack(anchor="w")
        ttk.Button(panel, text="应用片段属性", command=self.apply_active).pack(fill="x", pady=5)
        rows = [
            (("添加当前入出点", self.add_current), ("播放头添加标记", self.add_marker)),
            (("在播放头拆分", self.split_active), ("复制当前段", self.duplicate_active)),
            (("合并已勾选", self.merge_selected), ("反转选区", self.invert_segments)),
            (("补空隙", self.fill_gaps), ("删除列表所选", self.delete_active)),
            (("上移并紧排", lambda: self.reorder_active(-1)), ("下移并紧排", lambda: self.reorder_active(1))),
            (("全选", self.select_all), ("清除勾选", self.clear_selection)),
            (("撤销", self.undo), ("重做", self.redo)),
        ]
        for left, right in rows:
            row = tk.Frame(panel, bg=PANEL_ALT)
            row.pack(fill="x", pady=2)
            ttk.Button(row, text=left[0], command=left[1]).pack(side="left", fill="x", expand=True)
            ttk.Button(row, text=right[0], command=right[1]).pack(side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Button(panel, text="清空全部片段", command=self.clear_segments).pack(fill="x", pady=4)

    def _list_selected(self, _event=None) -> None:
        if self._timeline_syncing_selection:
            return
        selection = self.segment_tree.selection()
        self._timeline_selection = set(selection)
        if selection:
            focused = self.segment_tree.focus()
            self.state.active_id = focused if focused in selection else selection[0]
            self._refresh_active_fields()
        else:
            self.state.active_id = None
        self._queue_timeline_draw()

    def _timeline_select(self, identifiers, *, active=None) -> None:
        existing = {segment.id for segment in self.state.model.segments}
        self._timeline_selection = set(identifiers) & existing
        if active not in self._timeline_selection:
            active = next((item.id for item in self.state.model.segments
                           if item.id in self._timeline_selection), None)
        self.state.active_id = active
        tree = getattr(self, 'segment_tree', None)
        if tree is not None and self._alive_view(tree):
            self._timeline_syncing_selection = True
            try:
                tree.selection_set(*[item for item in tree.get_children() if item in self._timeline_selection])
                if active:
                    tree.focus(active)
            finally:
                self._timeline_syncing_selection = False
        self._refresh_active_fields()
        self._queue_timeline_draw()

    def _timeline_pps(self) -> float:
        return max(1., self.timeline.winfo_width()-20.) / max(.001, self.state.duration) * self._timeline_zoom

    def _timeline_time(self, x) -> float:
        return max(0., min(self.state.duration, (self.timeline.canvasx(x)-10.) / self._timeline_pps()))

    def _timeline_hit(self, x, y):
        x, y = self.timeline.canvasx(x), self.timeline.canvasy(y)
        return next((self._timeline_items[item] for item in reversed(
            self.timeline.find_overlapping(x-2., y-2., x+2., y+2.)) if item in self._timeline_items), None)

    def _queue_timeline_draw(self) -> None:
        if self._timeline_draw_after is None and self._alive_view(getattr(self, 'timeline', None)):
            self._timeline_draw_after = self.app.after(16, self._flush_timeline_draw)

    def _flush_timeline_draw(self) -> None:
        self._timeline_draw_after = None
        self._draw_timeline()

    def _cancel_timeline_draw(self) -> None:
        if self._timeline_draw_after is not None:
            try:
                self.app.after_cancel(self._timeline_draw_after)
            except tk.TclError:
                pass
            self._timeline_draw_after = None

    def _timeline_scroll(self, *args) -> None:
        self.timeline.xview(*args)
        self._queue_timeline_draw()

    def _timeline_wheel(self, event):
        self.timeline.xview_scroll(-3 if event.delta > 0 else 3, 'units')
        self._queue_timeline_draw()
        return 'break'

    def _timeline_pan_press(self, event):
        self.timeline.scan_mark(event.x, event.y)
        return 'break'

    def _timeline_pan_motion(self, event):
        self.timeline.scan_dragto(event.x, event.y, gain=1)
        self._queue_timeline_draw()
        return 'break'

    def _timeline_zoom_wheel(self, event):
        if self._timeline_gesture is not None or self.state.duration <= 0:
            return 'break'
        anchor = self._timeline_time(event.x)
        self._timeline_zoom = max(1., min(4096., self._timeline_zoom * (2. if event.delta > 0 else .5)))
        width = max(float(self.timeline.winfo_width()), 20.+self.state.duration*self._timeline_pps())
        self.timeline.configure(scrollregion=(0, 0, width, max(80, self.timeline.winfo_height())))
        self.timeline.xview_moveto(max(0., (10.+anchor*self._timeline_pps()-event.x)/width))
        self._queue_timeline_draw()
        return 'break'

    def _timeline_click(self, event) -> None:
        if self.state.duration <= 0 or getattr(self.app, 'working', False):
            return
        self._timeline_cancel()
        self.timeline.focus_set()
        self.stop_playback(pause=True)
        snapshot = self.state.model.segments
        previous = set(self._timeline_selection)
        before_active = self.state.active_id
        x, y = self.timeline.canvasx(event.x), self.timeline.canvasy(event.y)
        hit = self._timeline_hit(event.x, event.y)
        additive = bool(getattr(event, 'state', 0) & (0x4 | 0x1))
        kind = 'scrub' if y < 26 else 'move' if hit else 'marquee'
        if kind == 'move':
            selected = previous | {hit} if additive else previous if hit in previous else {hit}
            if additive and hit in previous:
                selected.remove(hit)
            self._timeline_select(selected, active=hit)
        elif kind == 'marquee' and not additive:
            self._timeline_select(())
        self._timeline_gesture = dict(kind=kind, x=x, y=y, end=(x, y),
            model=self.state.model, generation=self.state.generation, edit_generation=self.state.edit_generation,
            snapshot=snapshot, before=previous, before_active=before_active,
            selected=set(self._timeline_selection), additive=additive, scale=self._timeline_pps(),
            delta=0., moved=False)
        self.seek(self._timeline_time(event.x))

    def _timeline_gesture_valid(self, gesture) -> bool:
        return (gesture['model'] is self.state.model and gesture['generation'] == self.state.generation
                and gesture['edit_generation'] == self.state.edit_generation
                and not getattr(self.app, 'working', False))

    def _timeline_motion(self, event, *, autoscroll=True):
        gesture = self._timeline_gesture
        if gesture is None:
            return
        if not self._timeline_gesture_valid(gesture):
            self._timeline_cancel()
            return 'break'
        if gesture['kind'] == 'scrub':
            gesture['seek'] = self._timeline_time(event.x)
            self._queue_timeline_draw()
            return 'break'
        if autoscroll and self._timeline_zoom > 1 and (event.x < 12 or event.x > self.timeline.winfo_width()-12):
            self.timeline.xview_scroll(-1 if event.x < 12 else 1, 'units')
        x, y = self.timeline.canvasx(event.x), self.timeline.canvasy(event.y)
        gesture['end'] = (x, y)
        if not gesture['moved'] and max(abs(x-gesture['x']), abs(y-gesture['y'])) < 4:
            return 'break'
        gesture['moved'] = True
        if gesture['kind'] == 'marquee':
            left, right = sorted((gesture['x'], x))
            top, bottom = sorted((gesture['y'], y))
            hit = set()
            track_bottom = max(80, self.timeline.winfo_height())-24
            if bottom >= 26 and top <= track_bottom:
                for segment in gesture['snapshot']:
                    first = 10.+segment.start*gesture['scale']
                    last = 10.+(segment.end if segment.end is not None else segment.start)*gesture['scale']
                    if last+2 >= left and first-2 <= right:
                        hit.add(segment.id)
            self._timeline_select(hit | gesture['before'] if gesture['additive'] else hit)
        else:
            selected = [item for item in gesture['snapshot'] if item.id in gesture['selected']]
            if not selected:
                return 'break'
            delta = (x-gesture['x'])/gesture['scale']
            stream = self.state.probe.primary_video if self.state.probe else None
            fps = stream.fps if stream is not None else 0.
            delta = math.floor(delta*fps+.5)/fps if fps > 0 else round(delta, 6)
            lower = -min(item.start for item in selected)
            upper = self.state.duration-max(item.end if item.end is not None else item.start for item in selected)
            gesture['delta'] = max(lower, min(upper, delta))
            self._timeline_preview = tuple(clone_segment(item,
                start=item.start+gesture['delta'],
                end=None if item.end is None else item.end+gesture['delta'])
                if item.id in gesture['selected'] else item for item in gesture['snapshot'])
        self._queue_timeline_draw()
        return 'break'

    def _timeline_release(self, event):
        gesture = self._timeline_gesture
        if gesture is None:
            return
        if not self._timeline_gesture_valid(gesture):
            self._timeline_cancel()
            return 'break'
        if gesture['moved'] or gesture['kind'] == 'scrub':
            self._timeline_motion(event, autoscroll=False)
        preview = self._timeline_preview
        self._timeline_gesture = None
        self._timeline_preview = None
        self._cancel_timeline_draw()
        if gesture['kind'] == 'move' and preview is not None and abs(gesture['delta']) > 1e-9:
            self.state.model._replace(preview)
            self._changed()
        else:
            if gesture['kind'] == 'scrub':
                self.seek(self._timeline_time(event.x))
            self._draw_timeline()
        return 'break'

    def _timeline_cancel(self, _event=None):
        gesture, self._timeline_gesture = self._timeline_gesture, None
        self._timeline_preview = None
        if gesture is not None:
            if self._timeline_gesture_valid(gesture):
                self._timeline_select(gesture['before'], active=gesture['before_active'])
            self._queue_timeline_draw()
        return 'break'

    def _timeline_key(self, event):
        if getattr(event, 'state', 0) & 0x4:
            key = event.keysym.lower()
            if key == 'a':
                self._timeline_select(item.id for item in self.state.model.segments)
            elif key in {'z', 'y'}:
                if self._timeline_gesture is not None:
                    self._timeline_cancel()
                elif not getattr(self.app, 'working', False):
                    self.undo() if key == 'z' else self.redo()
            else:
                return
            return 'break'
        if event.keysym == 'Escape':
            if self._timeline_gesture is not None:
                return self._timeline_cancel()
            self._timeline_select(())
            return 'break'
        if event.keysym in {'Delete', 'BackSpace'}:
            if not getattr(self.app, 'working', False):
                self._timeline_cancel()
                if self._timeline_selection:
                    self.delete_active()
            return 'break'

    def _timeline_context(self, event):
        if getattr(self.app, 'working', False):
            return 'break'
        self._timeline_cancel()
        self.timeline.focus_set()
        hit = self._timeline_hit(event.x, event.y)
        if hit and hit not in self._timeline_selection:
            self._timeline_select({hit}, active=hit)
        menu = tk.Menu(self.timeline, tearoff=False, bg=PANEL, fg=TEXT, activebackground=PURPLE)
        menu.add_command(label='删除所选片段', command=self.delete_active,
            state='normal' if self._timeline_selection else 'disabled', accelerator='Delete')
        menu.add_separator()
        menu.add_command(label='撤销', command=self.undo,
            state='normal' if self.state.model.history.can_undo else 'disabled', accelerator='Ctrl+Z')
        menu.add_command(label='重做', command=self.redo,
            state='normal' if self.state.model.history.can_redo else 'disabled', accelerator='Ctrl+Y')
        self.timeline_context_menu = menu
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return 'break'

    def _draw_timeline(self) -> None:
        if not hasattr(self, "timeline") or not self._alive_view(self.timeline):
            return
        canvas = self.timeline
        canvas.delete("all")
        self._timeline_items = {}
        width = max(100, canvas.winfo_width())
        height = max(80, canvas.winfo_height())
        duration = self.state.duration
        if duration <= 0:
            canvas.configure(scrollregion=(0, 0, width, height))
            canvas.xview_moveto(0)
            canvas.create_text(width / 2, height / 2, text="时间线等待素材", fill=MUTED, font=(UI_FONT, 10))
            return
        pps = self._timeline_pps()
        content_width = max(width, 20.+duration*pps)
        canvas.configure(scrollregion=(0, 0, content_width, height))
        left, right = canvas.canvasx(0)-4, canvas.canvasx(width)+4
        label_font = getattr(self, '_timeline_label_font', None)
        if label_font is None:
            self._timeline_label_font = label_font = tkfont.Font(root=canvas, family=UI_FONT, size=8)
        colors = ("#55407f", "#315f73", "#705035", "#3f6d51")
        for segment in self._timeline_preview if self._timeline_preview is not None else self.state.model.segments:
            x0 = 10 + segment.start * pps
            end = segment.start if segment.end is None else segment.end
            x1 = 10 + end * pps
            if x1 < left or x0 > right:
                continue
            active = segment.id in self._timeline_selection
            if segment.is_marker:
                item = canvas.create_line(x0, 26, x0, height - 20,
                    fill='#ffffff' if active else '#ffd166', width=3 if active else 2)
                self._timeline_items[item] = segment.id
                continue
            item = canvas.create_rectangle(max(left, x0), 30, min(right, max(x0 + 2, x1)), height - 24,
                                    fill="#9a67c7" if active else colors[hash(segment.id) % len(colors)],
                                    outline="#ffffff" if active else "#81738d", width=2 if active else 1,
                                    tags=(f"segment:{segment.id}",))
            self._timeline_items[item] = segment.id
            if x1 - x0 > 60:
                title = (segment.name or '片段')[:60]
                room = min(right, x1)-max(left, x0)-8
                low, high = 0, len(title)
                while low < high:
                    middle = (low+high+1)//2
                    if label_font.measure(title[:middle]) <= room:
                        low = middle
                    else:
                        high = middle-1
                if low:
                    item = canvas.create_text(max(left, x0) + 4, 40, text=title[:low],
                        anchor='nw', fill=TEXT, font=label_font)
                    self._timeline_items[item] = segment.id
        gesture = self._timeline_gesture
        if gesture and gesture['kind'] == 'marquee' and gesture['moved']:
            canvas.create_rectangle(gesture['x'], gesture['y'], *gesture['end'],
                outline='#72b9ec', dash=(3, 2), tags='timeline-marquee')
        if gesture and gesture['kind'] == 'scrub' and 'seek' in gesture:
            value = gesture.pop('seek')
            self.seek(value)
        self._draw_timeline_playhead()
        canvas.create_text(canvas.canvasx(0)+10, 8, anchor='nw',
            text=f'0.000  →  {_time(duration)}   拖动调整取段 · 空白框选 · Ctrl+滚轮缩放',
            fill=MUTED, font=(MONO_FONT, 8))

    def _draw_timeline_playhead(self) -> None:
        if not hasattr(self, 'timeline') or not self._alive_view(self.timeline):
            return
        canvas = self.timeline
        canvas.delete('timeline-head')
        x = 10.+self.state.current_time*self._timeline_pps()
        height = max(80, canvas.winfo_height())
        canvas.create_line(x, 5, x, height-8, fill='#ff4f70', width=2, tags='timeline-head')
        canvas.create_polygon(x-5, 5, x+5, 5, x, 14, fill='#ff4f70', tags='timeline-head')

    def use_current(self) -> None:
        path = self.app.editor_video_var.get().strip()
        if not path or not Path(path).is_file():
            self._error("当前剪辑工作台还没有有效素材。")
            return
        self.open_media(path)

    def _auto_save_changed(self) -> None:
        self.state.auto_save = bool(self.auto_save_var.get())
        if self.state.auto_save:
            try:
                self.save_llc(automatic=True)
            except Exception as exc:
                self._set_llc_error(exc)
            self.refresh()

    def _parse_time(self, variable: tk.StringVar, label: str, allow_empty: bool = False) -> float | None:
        text = variable.get().strip()
        if allow_empty and not text:
            return None
        try:
            value = edl.parse_timecode(text)
        except ValueError as exc:
            raise ValueError(f"{label}：{exc}") from exc
        if self.state.duration > 0 and value > self.state.duration + 0.001:
            raise ValueError(f"{label}超过素材时长。")
        variable.set(_time(value))
        return value

    def apply_active(self) -> None:
        try:
            segment = self.state.active()
            start = self._parse_time(self.start_var, "入点")
            end = self._parse_time(self.end_var, "出点", allow_empty=True)
            updated = clone_segment(segment, start=start, end=end, name=self.name_var.get(),
                                    tags=_parse_tags(self.tags_var.get()), selected=bool(self.selected_var.get()))
            self.state.model._replace([updated if item.id == segment.id else item for item in self.state.model.segments])
            self._changed()
        except Exception as exc:
            self._error(exc)

    def add_current(self) -> None:
        try:
            self._require_source()
            start = self._parse_time(self.start_var, "入点")
            end = self._parse_time(self.end_var, "出点")
            if start is None or end is None:
                raise ValueError("添加片段需要入点和出点。")
            segment = self.state.model.add(start, end, name=self.name_var.get(), tags=_parse_tags(self.tags_var.get()), selected=True)
            self.state.active_id = segment.id
            self._changed()
        except Exception as exc:
            self._error(exc)

    def add_marker(self) -> None:
        try:
            self._require_source()
            segment = self.state.model.add(self.state.current_time, None, name=self.name_var.get(), selected=False)
            self.state.active_id = segment.id
            self._changed()
        except Exception as exc:
            self._error(exc)

    def split_active(self) -> None:
        try:
            left, _right = self.state.model.split(self.state.active().id, self.state.current_time)
            self.state.active_id = left.id
            self._changed()
        except Exception as exc:
            self._error(exc)

    def duplicate_active(self) -> None:
        try:
            duplicate = self.state.model.duplicate(self.state.active().id)
            self.state.active_id = duplicate.id
            self._changed()
        except Exception as exc:
            self._error(exc)

    def merge_selected(self) -> None:
        try:
            merged = self.state.model.merge_selected()
            self.state.active_id = merged.id
            self._changed()
        except Exception as exc:
            self._error(exc)

    def invert_segments(self) -> None:
        try:
            self.state.model.invert(selected_only=bool(self.state.model.selected_clips), keep_markers=True)
            self.state.active_id = self.state.model.segments[0].id if self.state.model.segments else None
            self._changed()
        except Exception as exc:
            self._error(exc)

    def fill_gaps(self) -> None:
        try:
            gaps = self.state.model.fill_gaps(name="补空隙")
            if not gaps:
                raise ValueError("当前没有空隙。")
            self._changed()
        except Exception as exc:
            self._error(exc)

    def delete_active(self) -> None:
        try:
            if getattr(self.app, 'working', False):
                return
            ids = list(self.segment_tree.selection()) if hasattr(self, "segment_tree") else [self.state.active().id]
            if not ids:
                raise ValueError("请先在列表中选择片段。")
            self.state.model.remove_many(ids)
            self._timeline_selection.difference_update(ids)
            self.state.active_id = self.state.model.segments[0].id if self.state.model.segments else None
            self._changed()
        except Exception as exc:
            self._error(exc)

    def reorder_active(self, direction: int) -> None:
        try:
            active = self.state.active()
            clips = [segment for segment in self.state.model.segments if not segment.is_marker]
            index = next(index for index, segment in enumerate(clips) if segment.id == active.id)
            target = index + direction
            if not 0 <= target < len(clips):
                return
            clips[index], clips[target] = clips[target], clips[index]
            self.state.model.reorder([segment.id for segment in clips])
            self._changed()
        except Exception as exc:
            self._error(exc)

    def select_all(self) -> None:
        self.state.model.select_all()
        self._changed(save=False)

    def clear_selection(self) -> None:
        self.state.model.clear_selection()
        self._changed(save=False)

    def clear_segments(self) -> None:
        self.state.model.clear()
        self.state.active_id = None
        self._changed()

    def undo(self) -> None:
        try:
            self._timeline_cancel()
            self.state.model.undo()
            if self.state.active_id and all(item.id != self.state.active_id for item in self.state.model.segments):
                self.state.active_id = None
            self._changed()
        except (HistoryError, ValueError) as exc:
            self._error(exc)

    def redo(self) -> None:
        try:
            self._timeline_cancel()
            self.state.model.redo()
            self._changed()
        except HistoryError as exc:
            self._error(exc)

    def import_segments(self) -> None:
        path = filedialog.askopenfilename(title="导入片段", filetypes=CUT_TYPES, parent=self.app)
        if not path:
            return
        try:
            document = edl.load_document(path, media_duration=self.state.duration or None,
                                         media_file_name=Path(self.state.source).name if self.state.source else "")
            self.state.replace_segments(document.segments)
            self._changed()
            self.summary_var.set(f"已导入 {Path(path).name} · {len(document.segments)} 段")
        except Exception as exc:
            self._error(exc)

    def export_segments(self) -> None:
        path = filedialog.asksaveasfilename(title="导出片段", defaultextension=".csv", filetypes=CUT_TYPES, parent=self.app)
        if not path:
            return
        try:
            if Path(path).suffix.lower() in {".txt", ".chapters"}:
                fmt = "chapters"
            else:
                fmt = edl.format_from_path(path)
            clips = [segment for segment in self.state.model.segments if not segment.is_marker]
            edl.save_segments(path, clips, fmt,
                              media_file_name=Path(self.state.source).name if self.state.source else None,
                              media_duration=self.state.duration or None)
            self.app._log(f"无损片段已导出：{path}")
            self.summary_var.set(f"已导出 {Path(path).name}")
        except Exception as exc:
            self._error(exc)

    def _existing_llc_has_segments(self, path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            project = edl.load_document(path, "llc")
        except Exception:
            return False
        return bool(project.segments)

    def save_llc(self, automatic: bool = False) -> None:
        try:
            source = self._require_source()
            path = Path(source).with_suffix(".llc")
            if automatic:
                probe = self.state.probe
                if probe is None or os.path.normcase(str(Path(probe.path).resolve())) != os.path.normcase(str(Path(source).resolve())):
                    raise ValueError("素材尚未读取完成，已暂停自动保存，请重新打开素材后再编辑。")
            clips = [segment for segment in self.state.model.segments if not segment.is_marker]
            if automatic and not clips and self._existing_llc_has_segments(path):
                raise ValueError(
                    f"当前没有可保存的片段，已保留现有 LLC 不被覆盖：{path}"
                )
            edl.save_llc(path, Path(source).name, clips,
                         media_duration=self.state.duration or None)
            self.state.llc_save_error = ""
            if not automatic:
                self.summary_var.set(f"LLC 已保存：{path}")
                self.app._log(f"LosslessCut 工程已保存：{path}")
        except Exception as exc:
            self.state.llc_save_error = str(exc)[:300]
            if automatic:
                raise
            self._error(exc)

    def seek(self, seconds: float) -> None:
        seconds = min(max(0.0, float(seconds)), self.state.duration or float(seconds))
        self.state.current_time = seconds
        self.time_var.set(_time(seconds))
        self.app._mpv_command_async(["seek", seconds, "absolute+exact"], error_prefix="无损分段定位失败")
        self._draw_timeline_playhead()

    def seek_from_entry(self) -> None:
        try:
            value = self._parse_time(self.time_var, "定位时间")
            if value is not None:
                self.seek(value)
        except Exception as exc:
            self._error(exc)

    def step_keyframe(self, direction: int) -> None:
        times = self.state.keyframe_times
        if not times:
            self._error("关键帧尚未读取或素材没有关键帧索引。")
            return
        index = KeyframeIndex(times)
        target = index.previous(self.state.current_time - 0.0005) if direction < 0 else index.next(self.state.current_time + 0.0005)
        if target is None:
            self._error("已经位于关键帧边界。")
            return
        self.seek(target)

    def snap_active(self) -> None:
        try:
            segment = self.state.model.snap(self.state.active().id, self.state.keyframe_times,
                                            start_direction="previous", end_direction="nearest")
            self.state.active_id = segment.id
            self._changed()
        except Exception as exc:
            self._error(exc)

    def play_current_loop(self) -> None:
        try:
            segment = self.state.active()
            if segment.end is None:
                raise ValueError("标记点不能循环播放。")
            self.playback_items = (segment,)
            self.playback_index = 0
            self.playback_loop = True
            self.seek(segment.start)
            self.app.editor_paused = False
            self.app._mpv_command_async(["set_property", "pause", False])
            self.summary_var.set(f"循环播放：{segment.name or '当前段'}")
        except Exception as exc:
            self._error(exc)

    def play_selected_sequence(self) -> None:
        items = self.state.model.selected_clips
        if not items:
            self._error("请先勾选需要顺序播放的片段。")
            return
        self.playback_items = items
        self.playback_index = 0
        self.playback_loop = False
        self.state.active_id = items[0].id
        self.seek(items[0].start)
        self.app.editor_paused = False
        self.app._mpv_command_async(["set_property", "pause", False])
        self.summary_var.set(f"顺序播放 {len(items)} 个选中片段")
        self.refresh()

    def stop_playback(self, pause: bool = True) -> None:
        self.playback_items = ()
        self.playback_index = -1
        self.playback_loop = False
        if pause and getattr(self.app, "mpv", None) is not None:
            self.app.editor_paused = True
            self.app._mpv_command_async(["set_property", "pause", True])

    def on_player_time(self, current: float) -> None:
        self.state.current_time = max(0.0, float(current))
        if self._alive_view():
            self.time_var.set(_time(current))
            if self._redraw_after is None:
                self._redraw_after = self.app.after(100, self._redraw_playhead)
        if not self.playback_items or self.playback_index < 0:
            return
        item = self.playback_items[self.playback_index]
        if current + 0.04 < item.end:
            return
        if self.playback_loop:
            self.seek(item.start)
            return
        following = self.playback_index + 1
        if following >= len(self.playback_items):
            self.stop_playback(pause=True)
            self.summary_var.set("选中片段顺序播放完成")
            return
        self.playback_index = following
        item = self.playback_items[following]
        self.state.active_id = item.id
        self.seek(item.start)
        self.refresh()

    def _redraw_playhead(self) -> None:
        self._redraw_after = None
        self._draw_timeline_playhead()

    def _build_output_page(self, page: tk.Frame) -> None:
        panel = self._scrollable(page)
        panel.pack(fill="both", expand=True)
        content = panel.content  # type: ignore[attr-defined]
        tk.Label(content, text="无损输出", bg=PANEL_ALT, fg=TEXT, font=(UI_FONT, 15, "bold")).pack(anchor="w", padx=8, pady=6)
        modes = tk.Frame(content, bg=PANEL_ALT)
        modes.pack(fill="x", padx=8)
        for value, label in (("separate", "separate 分段"), ("merge", "merge 合并"), ("both", "both 两者")):
            ttk.Radiobutton(modes, text=label, value=value, variable=self.mode_var).pack(side="left", padx=3)
        self._path_row(content, "分段输出目录", self.output_dir_var, self.choose_output_dir)
        self._path_row(content, "合并输出文件", self.merged_output_var, self.choose_merged_output)
        grid = tk.Frame(content, bg=PANEL_ALT)
        grid.pack(fill="x", padx=8, pady=5)
        tk.Label(grid, text="封装", bg=PANEL_ALT, fg=MUTED).grid(row=0, column=0, sticky="w")
        ttk.Combobox(grid, textvariable=self.extension_var, state="readonly", width=10,
                     values=[".mkv", ".mp4", ".mov", ".m4v", ".ts", ".m2ts", ".webm"]).grid(row=1, column=0, sticky="ew")
        tk.Label(grid, text="命名模板", bg=PANEL_ALT, fg=MUTED).grid(row=0, column=1, sticky="w", padx=6)
        tk.Entry(grid, textvariable=self.template_var, bg=INPUT, fg=TEXT, insertbackground=TEXT,
                 relief="flat", font=(MONO_FONT, 10)).grid(row=1, column=1, sticky="ew", padx=6, ipady=5)
        grid.columnconfigure(1, weight=1)
        tk.Label(content, text="可用：{stem} {name} {index:03d} {start} {end} {duration} {ext} {tag.KEY}",
                 bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 8), wraplength=650, justify="left").pack(anchor="w", padx=8)
        checks = tk.Frame(content, bg=PANEL_ALT)
        checks.pack(fill="x", padx=8, pady=6)
        for text, variable in (("覆盖已有输出", self.overwrite_var), ("起点吸附关键帧", self.snap_var),
                               ("合并时写入片段章节", self.chapters_var)):
            tk.Checkbutton(checks, text=text, variable=variable, bg=PANEL_ALT, fg=TEXT,
                           selectcolor=INPUT, activebackground=PANEL_ALT, activeforeground=TEXT).pack(anchor="w")
        actions = tk.Frame(content, bg=PANEL_ALT)
        actions.pack(fill="x", padx=8, pady=8)
        ttk.Button(actions, text="执行片段导出", style="Accent.TButton", command=self.export_cut_plan).pack(fill="x", pady=3)
        ttk.Button(actions, text="整片 remux（不重编码）", command=self.remux).pack(fill="x", pady=3)
        ttk.Button(actions, text="整片写入片段 chapters", command=self.export_chapters).pack(fill="x", pady=3)
        ttk.Button(actions, text="打开输出目录", command=self.open_output_dir).pack(fill="x", pady=3)
        tk.Label(content, text="片段导出只处理列表中已勾选片段。流复制不重编码；非关键帧起点会按 GOP 限制提前。merge 的临时分段在结束后清理。",
                 bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 9), wraplength=650, justify="left").pack(anchor="w", padx=8, pady=8)

    def _path_row(self, parent: tk.Widget, label: str, variable: tk.StringVar, command: Callable) -> None:
        tk.Label(parent, text=label, bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 9, "bold")).pack(anchor="w", padx=8)
        row = tk.Frame(parent, bg=PANEL_ALT)
        row.pack(fill="x", padx=8, pady=(2, 6))
        tk.Entry(row, textvariable=variable, bg=INPUT, fg=TEXT, insertbackground=TEXT,
                 relief="flat", font=(UI_FONT, 9)).pack(side="left", fill="x", expand=True, ipady=6)
        ttk.Button(row, text="浏览…", command=command).pack(side="left", padx=(5, 0))

    def choose_output_dir(self) -> None:
        path = filedialog.askdirectory(title="无损分段输出目录", parent=self.app)
        if path:
            self.output_dir_var.set(path)

    def choose_merged_output(self) -> None:
        source = Path(self.state.source or "merged.mkv")
        path = filedialog.asksaveasfilename(title="合并输出", initialfile=f"{source.stem}_merged{self.extension_var.get()}",
                                            defaultextension=self.extension_var.get(), parent=self.app,
                                            filetypes=[("Matroska", "*.mkv"), ("MP4", "*.mp4"), ("MOV", "*.mov"), ("所有文件", "*.*")])
        if path:
            self.merged_output_var.set(path)

    def open_output_dir(self) -> None:
        self._sync_options_from_vars()
        path = self.state.output_dir or str(Path(self.state.merged_output).parent)
        if not path or not Path(path).is_dir():
            self._error("输出目录尚不存在。")
            return
        self.app._open_folder(str(Path(path) / "dummy"))

    def _selection(self) -> lf.TrackSelection:
        selected = self.state.track_selection
        probe = self.state.probe
        if not probe:
            return lf.TrackSelection.all()
        values: dict[str, list[int]] = {kind: [] for kind in ("video", "audio", "subtitle", "attachment", "data")}
        for stream in probe.streams:
            if stream.codec_type in values and selected.get(stream.index, True):
                values[stream.codec_type].append(stream.index)
        return lf.TrackSelection(**values)

    def _metadata(self) -> lf.MetadataOptions:
        rotation = self.rotation_var.get()
        return lf.MetadataOptions(
            title=self.title_var.get() or None,
            language=self.language_var.get().strip() or None,
            rotate=None if rotation == "不修改" else int(rotation),
            hvc1=bool(self.hvc1_var.get()),
            global_tags=_parse_tags(self.meta_tags_var.get()),
        )

    def _ensure_distinct(self, outputs: Iterable[str]) -> None:
        source = Path(self._require_source()).resolve()
        if any(Path(path).resolve() == source for path in outputs):
            raise ValueError("输出不能覆盖原素材，请使用独立文件名或目录。")

    def export_cut_plan(self) -> None:
        if self.app.working:
            return
        try:
            self._sync_options_from_vars()
            source = self._require_source()
            state = self.state
            output_dir = Path(state.output_dir or str(Path(source).parent / "Lossless"))
            output_dir.mkdir(parents=True, exist_ok=True)
            output = state.merged_output or str(output_dir / f"{Path(source).stem}_merged{state.extension}")
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            plan = lf.plan_lossless_export(
                self._ffmpeg(), source, state.model.segments, mode=state.output_mode,
                output_dir=output_dir, output=output, template=state.name_template,
                extension=state.extension, probe=state.probe, keyframes=state.keyframes,
                selection=self._selection(), metadata=self._metadata(), overwrite=state.overwrite,
                snap_start=state.snap_start, chapters=state.auto_chapters,
            )
            outputs = [*plan.segment_outputs, *([plan.merged_output] if plan.merged_output else [])]
            self._ensure_distinct(outputs)
            for warning in plan.warnings:
                self.app._log(f"无损导出提示：{warning}")
            self._run_plan(plan, "无损片段导出", state.model.total_duration)
        except Exception as exc:
            self._error(exc)

    def _run_plan(self, plan: lf.LosslessExportPlan, label: str, duration: float) -> None:
        from lossless_runner import LosslessExportCancelled, run_lossless_export

        generation, job_id = self._begin_job()
        self.app.cancel_event.clear()
        self.app._set_working(True)
        self.app._begin_shared_progress(f"{label} 0.0%")

        def progress(update) -> None:
            if job_id != self._active_job:
                return
            overall = 100.0 * (update.overall_fraction or 0.0)
            self.app.events.put(("editor_export_progress", (
                overall, f"{label} {update.command_index}/{update.command_count}",
                100.0 * (update.fraction or 0.0), str(update.fps or 0.0),
                f"{update.speed or 0.0}x",
            )))

        def worker() -> None:
            try:
                result = run_lossless_export(
                    plan, cancel_event=self.app.cancel_event, progress=progress,
                    log=lambda text: self.app.events.put(("log", str(text))),
                )
                self.app.events.put(("lossless_job_done", (generation, job_id, label, list(result.outputs))))
            except LosslessExportCancelled as exc:
                self.app.events.put(("lossless_job_error", (generation, job_id, label, str(exc))))
            except Exception as exc:
                self.app.events.put(("lossless_job_error", (generation, job_id, label, str(exc))))

        threading.Thread(target=worker, daemon=True, name="lossless_plan").start()

    def remux(self) -> None:
        if self.app.working:
            return
        try:
            self._sync_options_from_vars()
            source = self._require_source()
            output = self.state.merged_output or str(Path(source).with_name(f"{Path(source).stem}_remux{self.state.extension}"))
            output = str(Path(output).with_suffix(self.state.extension))
            self._ensure_distinct([output])
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            command = lf.build_remux_command(self._ffmpeg(), source, output, probe=self.state.probe,
                                             selection=self._selection(), metadata=self._metadata(),
                                             overwrite=self.state.overwrite)
            self._start_commands("整片 remux", [command], [self.state.duration], [output])
        except Exception as exc:
            self._error(exc)

    def export_chapters(self) -> None:
        if self.app.working:
            return
        try:
            self._sync_options_from_vars()
            source = self._require_source()
            if not self.state.model.selected_clips:
                raise ValueError("没有可生成章节的选中片段。")
            output = self.state.merged_output or str(Path(source).with_name(f"{Path(source).stem}_chapters{self.state.extension}"))
            output = str(Path(output).with_suffix(self.state.extension))
            self._ensure_distinct([output])
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            chapters = lf.chapters_from_segments(
                self.state.model.segments, timeline="source",
                keyframes=self.state.keyframes, snap_start=self.state.snap_start,
            )
            chapter_path = Path(output).parent / f".{Path(output).stem}.ffmetadata"
            lf.write_chapters_file(chapter_path, chapters, title=Path(output).stem)
            command = lf.build_segments_chapters_command(
                self._ffmpeg(), source, output, str(chapter_path), probe=self.state.probe,
                selection=self._selection(), metadata=self._metadata(), overwrite=self.state.overwrite,
            )
            self._start_commands("写入章节", [command], [self.state.duration], [output], cleanup_paths=[str(chapter_path)])
        except Exception as exc:
            self._error(exc)

    def _build_tracks_page(self, page: tk.Frame) -> None:
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        toolbar = tk.Frame(page, bg=PANEL_ALT)
        toolbar.grid(row=0, column=0, sticky="ew", padx=8, pady=5)
        ttk.Button(toolbar, text="重新读取轨道", command=self.reprobe).pack(side="left")
        ttk.Button(toolbar, text="全选轨道", command=lambda: self.set_all_tracks(True)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="清空轨道", command=lambda: self.set_all_tracks(False)).pack(side="left")
        ttk.Button(toolbar, text="切换列表所选", command=self.toggle_tree_tracks).pack(side="left", padx=4)
        ttk.Button(toolbar, text="提取勾选轨道", command=self.extract_tracks).pack(side="left")
        columns = ("selected", "index", "kind", "codec", "language", "detail")
        tree = ttk.Treeview(page, columns=columns, show="headings", selectmode="extended", height=6)
        for column, text, width in zip(columns, ("选", "流", "类型", "编码", "语言", "详情"), (35, 45, 75, 100, 70, 200)):
            tree.heading(column, text=text)
            tree.column(column, width=width)
        tree.grid(row=1, column=0, sticky="nsew", padx=8)
        tree.bind("<Double-Button-1>", lambda _event: self.toggle_tree_tracks())
        self.track_tree = tree
        lower = ttk.Panedwindow(page, orient="horizontal")
        lower.grid(row=2, column=0, sticky="nsew", padx=8, pady=6)
        external = tk.Frame(lower, bg=PANEL_ALT)
        metadata = tk.Frame(lower, bg=PANEL_ALT)
        lower.add(external, weight=3)
        lower.add(metadata, weight=2)
        tk.Label(external, text="外部音频 / 字幕（用于独立混流）", bg=PANEL_ALT, fg=TEXT,
                 font=(UI_FONT, 10, "bold")).pack(anchor="w")
        self.external_tree = ttk.Treeview(external, columns=("kind", "path", "language", "title"), show="headings", height=4)
        for column, text, width in zip(("kind", "path", "language", "title"), ("类型", "文件", "语言", "标题"), (65, 220, 65, 100)):
            self.external_tree.heading(column, text=text)
            self.external_tree.column(column, width=width)
        self.external_tree.pack(fill="both", expand=True, pady=4)
        row = tk.Frame(external, bg=PANEL_ALT)
        row.pack(fill="x")
        tk.Entry(row, textvariable=self.external_path_var, bg=INPUT, fg=TEXT, insertbackground=TEXT,
                 relief="flat").pack(side="left", fill="x", expand=True, ipady=5)
        ttk.Button(row, text="浏览", command=self.choose_external).pack(side="left", padx=3)
        ttk.Button(row, text="添加", command=self.add_external).pack(side="left")
        fields = tk.Frame(external, bg=PANEL_ALT)
        fields.pack(fill="x", pady=4)
        ttk.Combobox(fields, textvariable=self.external_kind_var, values=["audio", "subtitle"], state="readonly", width=8).pack(side="left")
        for label, variable, width in (("语言", self.external_language_var, 7), ("标题", self.external_title_var, 12)):
            tk.Label(fields, text=label, bg=PANEL_ALT, fg=MUTED).pack(side="left", padx=(4, 2))
            tk.Entry(fields, textvariable=variable, width=width, bg=INPUT, fg=TEXT, insertbackground=TEXT, relief="flat").pack(side="left")
        tk.Checkbutton(fields, text="默认", variable=self.external_default_var, bg=PANEL_ALT, fg=TEXT, selectcolor=INPUT).pack(side="left", padx=3)
        tk.Checkbutton(fields, text="强制", variable=self.external_forced_var, bg=PANEL_ALT, fg=TEXT, selectcolor=INPUT).pack(side="left")
        buttons = tk.Frame(external, bg=PANEL_ALT)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="移除所选外部轨", command=self.remove_external).pack(side="left")
        ttk.Button(buttons, text="混流外部轨并导出", command=self.mux_external).pack(side="left", padx=5)
        tk.Label(metadata, text="输出元数据", bg=PANEL_ALT, fg=TEXT, font=(UI_FONT, 10, "bold")).pack(anchor="w")
        for label, variable in (("标题", self.title_var), ("音轨/字幕语言", self.language_var), ("额外标签 key=value;…", self.meta_tags_var)):
            tk.Label(metadata, text=label, bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 8)).pack(anchor="w")
            tk.Entry(metadata, textvariable=variable, bg=INPUT, fg=TEXT, insertbackground=TEXT,
                     relief="flat").pack(fill="x", ipady=4, pady=2)
        row = tk.Frame(metadata, bg=PANEL_ALT)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="旋转", bg=PANEL_ALT, fg=MUTED).pack(side="left")
        ttk.Combobox(row, textvariable=self.rotation_var, values=["不修改", "0", "90", "180", "270"],
                     state="readonly", width=8).pack(side="left", padx=4)
        tk.Checkbutton(row, text="HEVC 使用 hvc1", variable=self.hvc1_var, bg=PANEL_ALT, fg=TEXT,
                       selectcolor=INPUT, activebackground=PANEL_ALT, activeforeground=TEXT).pack(side="left")
        tk.Label(metadata, text="这些设置同时用于片段、remux、章节及外部轨混流。旋转只写显示元数据，不重编码像素。",
                 bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 8), wraplength=360, justify="left").pack(anchor="w", pady=5)

    def reprobe(self) -> None:
        try:
            self.open_media(self._require_source(), preserve=True)
        except Exception as exc:
            self._error(exc)

    def set_all_tracks(self, selected: bool) -> None:
        if self.state.probe:
            for stream in self.state.probe.streams:
                self.state.track_selection[stream.index] = selected
        self._refresh_tracks()

    def toggle_tree_tracks(self) -> None:
        for item in self.track_tree.selection():
            index = int(item)
            self.state.track_selection[index] = not self.state.track_selection.get(index, True)
        self._refresh_tracks()

    def choose_external(self) -> None:
        path = filedialog.askopenfilename(title="外部音频或字幕", parent=self.app,
                                          filetypes=[("音频与字幕", "*.wav *.flac *.mp3 *.m4a *.aac *.ogg *.opus *.srt *.ass *.ssa *.vtt"), ("所有文件", "*.*")])
        if path:
            self.external_path_var.set(path)
            if Path(path).suffix.lower() in {".srt", ".ass", ".ssa", ".vtt"}:
                self.external_kind_var.set("subtitle")

    def add_external(self) -> None:
        try:
            path = self.external_path_var.get().strip()
            if not path or not Path(path).is_file():
                raise ValueError("请选择有效的外部音频或字幕文件。")
            self.state.external_tracks.append(lf.ExternalTrack(
                path=path, kind=self.external_kind_var.get(), language=self.external_language_var.get().strip() or None,
                title=self.external_title_var.get() or None, default=bool(self.external_default_var.get()),
                forced=bool(self.external_forced_var.get()),
            ))
            self.external_path_var.set("")
            self._refresh_external()
        except Exception as exc:
            self._error(exc)

    def remove_external(self) -> None:
        for item in sorted((int(value) for value in self.external_tree.selection()), reverse=True):
            if 0 <= item < len(self.state.external_tracks):
                del self.state.external_tracks[item]
        self._refresh_external()

    def mux_external(self) -> None:
        if self.app.working:
            return
        try:
            self._sync_options_from_vars()
            source = self._require_source()
            if not self.state.external_tracks:
                raise ValueError("请先添加至少一条外部音频或字幕。")
            output = self.state.merged_output or str(Path(source).with_name(f"{Path(source).stem}_external{self.state.extension}"))
            output = str(Path(output).with_suffix(self.state.extension))
            self._ensure_distinct([output])
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            command = lf.build_external_mux_command(
                self._ffmpeg(), source, self.state.external_tracks, output, probe=self.state.probe,
                selection=self._selection(), metadata=self._metadata(), overwrite=self.state.overwrite,
            )
            self._start_commands("外部轨混流", [command], [self.state.duration], [output])
        except Exception as exc:
            self._error(exc)

    def extract_tracks(self) -> None:
        if self.app.working:
            return
        try:
            self._sync_options_from_vars()
            source = self._require_source()
            probe = self.state.probe
            if not probe:
                raise ValueError("轨道信息尚未读取。")
            folder = Path(self.state.output_dir or str(Path(source).parent / "Extracted"))
            folder.mkdir(parents=True, exist_ok=True)
            commands: list[list[str]] = []
            outputs: list[str] = []
            selected_streams = [stream for stream in probe.streams if self.state.track_selection.get(stream.index, True)]
            if not selected_streams:
                raise ValueError("请先勾选要提取的轨道。")
            for stream in selected_streams:
                kind = stream.codec_type
                if kind not in lf.STREAM_TYPE_LETTERS:
                    continue
                ordinal = sum(1 for item in probe.streams if item.codec_type == kind and item.index < stream.index)
                extension = {"video": ".mkv", "audio": ".mka", "subtitle": ".mks", "data": ".bin"}.get(kind, ".bin")
                if kind == "attachment":
                    extension = Path(stream.filename).suffix or ".bin"
                output = folder / f"{Path(source).stem}_stream_{stream.index}_{kind}{extension}"
                self._ensure_distinct([str(output)])
                commands.append(lf.build_extract_command(self._ffmpeg(), source, str(output), kind,
                                                         stream=ordinal, probe=probe, overwrite=self.state.overwrite))
                outputs.append(str(output))
            if not commands:
                raise ValueError("勾选项没有可提取的轨道。")
            self._start_commands("提取轨道", commands, [self.state.duration] * len(commands), outputs)
        except Exception as exc:
            self._error(exc)

    def _build_detect_page(self, page: tk.Frame) -> None:
        content = self._scrollable(page)
        content.pack(fill="both", expand=True)
        panel = content.content  # type: ignore[attr-defined]
        tk.Label(panel, text="黑场 / 静音 / 场景检测", bg=PANEL_ALT, fg=TEXT,
                 font=(UI_FONT, 14, "bold")).pack(anchor="w", padx=8, pady=6)
        grid = tk.Frame(panel, bg=PANEL_ALT)
        grid.pack(fill="x", padx=8)
        variables = [
            ("黑场最短秒", self.black_duration_var), ("黑画面比例", self.black_picture_var),
            ("黑像素阈值", self.black_pixel_var), ("静音 dB", self.silence_db_var),
            ("静音最短秒", self.silence_duration_var), ("场景阈值 0–1", self.scene_threshold_var),
        ]
        for index, (label, variable) in enumerate(variables):
            row, column = divmod(index, 3)
            tk.Label(grid, text=label, bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 8)).grid(row=row * 2, column=column, sticky="w", padx=3)
            tk.Entry(grid, textvariable=variable, bg=INPUT, fg=TEXT, insertbackground=TEXT,
                     relief="flat", width=12).grid(row=row * 2 + 1, column=column, sticky="ew", padx=3, ipady=5)
            grid.columnconfigure(column, weight=1)
        actions = tk.Frame(panel, bg=PANEL_ALT)
        actions.pack(fill="x", padx=8, pady=7)
        ttk.Button(actions, text="检测黑场", command=lambda: self.start_detection("black")).pack(side="left")
        ttk.Button(actions, text="检测静音", command=lambda: self.start_detection("silence")).pack(side="left", padx=4)
        ttk.Button(actions, text="检测场景", command=lambda: self.start_detection("scene")).pack(side="left")
        columns = ("index", "start", "end", "duration")
        self.detect_tree = ttk.Treeview(panel, columns=columns, show="headings", height=10)
        for column, text in zip(columns, ("#", "开始", "结束", "时长")):
            self.detect_tree.heading(column, text=text)
            self.detect_tree.column(column, width=110)
        self.detect_tree.pack(fill="both", expand=True, padx=8, pady=4)
        self.detect_tree.bind("<<TreeviewSelect>>", self._detect_selected)
        tk.Label(panel, textvariable=self.detect_status_var, bg=PANEL_ALT, fg=GREEN,
                 wraplength=650, justify="left").pack(fill="x", padx=8)
        buttons = tk.Frame(panel, bg=PANEL_ALT)
        buttons.pack(fill="x", padx=8, pady=6)
        ttk.Button(buttons, text="检测区间作为片段", command=self.add_detected_segments).pack(side="left")
        ttk.Button(buttons, text="从当前片段扣除检测区间", command=self.subtract_detected).pack(side="left", padx=4)
        ttk.Button(buttons, text="按场景点拆分", command=self.split_detected_scenes).pack(side="left")

    def start_detection(self, kind: str) -> None:
        if self.app.working:
            return
        try:
            source = self._require_source()
            ffmpeg = self._ffmpeg()
            if kind == "black":
                command = detect.build_blackdetect_command(ffmpeg, source,
                    min_duration=float(self.black_duration_var.get()),
                    picture_threshold=float(self.black_picture_var.get()), pixel_threshold=float(self.black_pixel_var.get()))
            elif kind == "silence":
                command = detect.build_silencedetect_command(ffmpeg, source,
                    noise_db=float(self.silence_db_var.get()), min_duration=float(self.silence_duration_var.get()))
            else:
                command = detect.build_scene_detect_command(ffmpeg, source, threshold=float(self.scene_threshold_var.get()))
            self.app.cancel_event.clear()
            self.app._set_working(True)
            self.app._begin_shared_progress(f"{kind} 检测中…")
            self.detect_status_var.set(f"正在检测 {kind}；可用底部“取消任务”停止。")
            generation, job_id = self._begin_job()

            def worker() -> None:
                code, log = self.app._run_editor_process(command)
                self.app.events.put(("lossless_detection", (generation, job_id, kind, code, log)))

            threading.Thread(target=worker, daemon=True, name="lossless_detect").start()
        except Exception as exc:
            self._error(exc)

    def handle_detection(self, payload) -> None:
        values = tuple(payload)
        legacy = len(values) == 4
        if legacy:
            generation, kind, code, log = values
            job_id = None
        else:
            generation, job_id, kind, code, log = values
        if self.closed:
            return
        if not legacy and not self._job_is_current(generation, job_id):
            self.app._log(f"忽略过期的无损检测事件：{kind}")
            return
        self.app._set_working(False)
        if generation != self.state.generation:
            self.app._log(f"{kind} 检测完成于旧素材会话，未更新当前状态。")
            return
        if self.app.cancel_event.is_set():
            self.app.progress_text_var.set("已取消")
            self.detect_status_var.set("检测已取消")
            return
        if code:
            self.app.progress_text_var.set("检测失败")
            self._error(log[-3000:] or "FFmpeg 检测失败")
            return
        self.app.progress_var.set(100)
        self.app.progress_text_var.set("检测完成")
        self.state.detection_kind = kind
        self.state.detection_ranges = []
        self.state.detection_cuts = []
        if kind == "black":
            self.state.detection_ranges = detect.parse_blackdetect(log, duration=self.state.duration)
        elif kind == "silence":
            self.state.detection_ranges = detect.parse_silencedetect(log, duration=self.state.duration)
        else:
            self.state.detection_cuts = detect.parse_scene_cuts(log, duration=self.state.duration)
        self.app._log(f"无损 {kind} 检测日志：\n{log[-6000:]}")
        self._refresh_detection()

    def _refresh_detection(self) -> None:
        tree = getattr(self, "detect_tree", None)
        if tree is None or not self._alive_view(tree):
            return
        tree.delete(*tree.get_children())
        state = self.state
        if not state.detection_kind:
            self.detect_status_var.set("检测后可添加到片段、扣除区间或按场景拆分")
            return
        if state.detection_kind == "scene":
            ranges = [(value, value) for value in state.detection_cuts]
        else:
            ranges = [(item.start, item.end) for item in state.detection_ranges]
        for index, (start, end) in enumerate(ranges, 1):
            tree.insert("", "end", iid=str(index - 1), values=(index, _time(start), _time(end), _time(end - start)))
        self.detect_status_var.set(f"{state.detection_kind}：得到 {len(ranges)} 个结果；点击结果可定位。")

    def _detect_selected(self, _event=None) -> None:
        selection = self.detect_tree.selection()
        if not selection:
            return
        index = int(selection[0])
        if self.state.detection_kind == "scene" and index < len(self.state.detection_cuts):
            self.seek(self.state.detection_cuts[index])
        elif index < len(self.state.detection_ranges):
            self.seek(self.state.detection_ranges[index].start)

    def add_detected_segments(self) -> None:
        try:
            if not self.state.detection_ranges:
                raise ValueError("请先执行黑场或静音检测。")
            name = "黑场" if self.state.detection_kind == "black" else "静音"
            for item in self.state.detection_ranges:
                self.state.model.add(item.start, item.end, name=name, tags={self.state.detection_kind: "1"}, selected=True)
            self._changed()
        except Exception as exc:
            self._error(exc)

    def subtract_detected(self) -> None:
        try:
            if not self.state.detection_ranges:
                raise ValueError("请先执行黑场或静音检测。")
            result = detect.cut_out_ranges(self.state.model.segments, self.state.detection_ranges,
                                           duration=self.state.duration, add_tags={self.state.detection_kind: "removed"})
            self.state.replace_segments(result)
            self._changed()
        except Exception as exc:
            self._error(exc)

    def split_detected_scenes(self) -> None:
        try:
            if not self.state.detection_cuts:
                raise ValueError("请先执行场景检测并得到有效切点。")
            result = detect.split_segments_by_scene(self.state.model.segments, self.state.detection_cuts,
                                                    duration=self.state.duration, add_tags={"scene": "split"})
            self.state.replace_segments(result)
            self._changed()
        except Exception as exc:
            self._error(exc)

    def _build_tools_page(self, page: tk.Frame) -> None:
        content = self._scrollable(page)
        content.pack(fill="both", expand=True)
        panel = content.content  # type: ignore[attr-defined]
        tk.Label(panel, text="帧导出与 FFmpeg 实用命令", bg=PANEL_ALT, fg=TEXT,
                 font=(UI_FONT, 14, "bold")).pack(anchor="w", padx=8, pady=6)
        self._path_row(panel, "工具输入（HLS 可填 .m3u8 或网址）", self.utility_source_var, self.choose_utility_source)
        self._path_row(panel, "输出文件 / 抽帧目录", self.utility_output_var, self.choose_utility_output)
        frame = tk.LabelFrame(panel, text="帧导出", bg=PANEL_ALT, fg=TEXT)
        frame.pack(fill="x", padx=8, pady=6)
        row = tk.Frame(frame, bg=PANEL_ALT)
        row.pack(fill="x", pady=4)
        ttk.Combobox(row, textvariable=self.frame_mode_var, values=["每 N 秒", "每 N 帧"], state="readonly", width=10).pack(side="left")
        tk.Entry(row, textvariable=self.frame_interval_var, bg=INPUT, fg=TEXT, insertbackground=TEXT,
                 relief="flat", width=10).pack(side="left", padx=4)
        ttk.Combobox(row, textvariable=self.frame_format_var, values=["jpg", "png"], state="readonly", width=6).pack(side="left")
        ttk.Button(row, text="按间隔抽帧", command=self.export_interval_frames).pack(side="left", padx=4)
        ttk.Button(row, text="导出播放头当前帧", command=self.export_current_frame).pack(side="left")
        loop = tk.LabelFrame(panel, text="loop 循环（流复制）", bg=PANEL_ALT, fg=TEXT)
        loop.pack(fill="x", padx=8, pady=6)
        row = tk.Frame(loop, bg=PANEL_ALT)
        row.pack(fill="x", pady=4)
        tk.Label(row, text="额外循环次数（-1 无限，需填时长）", bg=PANEL_ALT, fg=MUTED).pack(side="left")
        tk.Entry(row, textvariable=self.loop_count_var, width=6, bg=INPUT, fg=TEXT, insertbackground=TEXT, relief="flat").pack(side="left", padx=4)
        tk.Label(row, text="限制时长秒（可空）", bg=PANEL_ALT, fg=MUTED).pack(side="left")
        tk.Entry(row, textvariable=self.loop_duration_var, width=10, bg=INPUT, fg=TEXT, insertbackground=TEXT, relief="flat").pack(side="left", padx=4)
        ttk.Button(row, text="执行 loop", command=self.run_loop).pack(side="left")
        other = tk.LabelFrame(panel, text="decimate / FPS / HLS", bg=PANEL_ALT, fg=TEXT)
        other.pack(fill="x", padx=8, pady=6)
        row = tk.Frame(other, bg=PANEL_ALT)
        row.pack(fill="x", pady=4)
        ttk.Combobox(row, textvariable=self.decimate_mode_var, values=["copy", "reencode"], state="readonly", width=10).pack(side="left")
        ttk.Button(row, text="只保留关键帧 decimate", command=self.run_decimate).pack(side="left", padx=4)
        tk.Label(row, text="FPS", bg=PANEL_ALT, fg=MUTED).pack(side="left")
        tk.Entry(row, textvariable=self.fps_var, width=10, bg=INPUT, fg=TEXT, insertbackground=TEXT, relief="flat").pack(side="left", padx=4)
        ttk.Button(row, text="改变 FPS（重编码）", command=self.run_fps).pack(side="left")
        ttk.Button(other, text="HLS 无损封装为 MKV", command=self.run_hls).pack(anchor="w", pady=4)
        tk.Label(panel, text="decimate=copy 不重编码；reencode 与 FPS 使用 libx264 CRF 18。抽帧和当前帧导出是图像操作，不属于无损视频流复制。",
                 bg=PANEL_ALT, fg=MUTED, font=(UI_FONT, 8), wraplength=680, justify="left").pack(anchor="w", padx=8, pady=8)

    def choose_utility_source(self) -> None:
        path = filedialog.askopenfilename(title="工具输入", parent=self.app,
                                          filetypes=[("媒体与 HLS", "*.*"), ("HLS", "*.m3u8")])
        if path:
            self.utility_source_var.set(path)

    def choose_utility_output(self) -> None:
        path = filedialog.asksaveasfilename(title="工具输出文件", defaultextension=".mkv", parent=self.app,
                                            filetypes=[("Matroska", "*.mkv"), ("MP4", "*.mp4"), ("MOV", "*.mov"), ("所有文件", "*.*")])
        if path:
            self.utility_output_var.set(path)

    def _utility_paths(self, *, hls: bool = False) -> tuple[str, str]:
        source = self.utility_source_var.get().strip() or self._require_source()
        if not hls and (not source or not Path(source).is_file()):
            raise ValueError("工具输入文件不存在。")
        output = self.utility_output_var.get().strip()
        if not output:
            raise ValueError("请填写工具输出路径。")
        if not hls and Path(source).resolve() == Path(output).resolve():
            raise ValueError("工具输出不能覆盖输入文件。")
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        return source, output

    def export_current_frame(self) -> None:
        if self.app.working:
            return
        try:
            source, _ = self._utility_paths()
            output = filedialog.asksaveasfilename(title="导出当前帧", defaultextension=f".{self.frame_format_var.get()}",
                                                  initialfile=f"{Path(source).stem}_{_time(self.state.current_time).replace(':', '-')}.{self.frame_format_var.get()}",
                                                  filetypes=[("JPEG", "*.jpg"), ("PNG", "*.png")], parent=self.app)
            if not output:
                return
            command = detect.build_thumbnail_command(self._ffmpeg(), source, self.state.current_time, output,
                                                     width=1920, height=-2, quality=2)
            self._start_commands("导出当前帧", [command], [0.0], [output])
        except Exception as exc:
            self._error(exc)

    def export_interval_frames(self) -> None:
        if self.app.working:
            return
        try:
            source, folder = self._utility_paths()
            Path(folder).mkdir(parents=True, exist_ok=True)
            interval = float(self.frame_interval_var.get())
            kwargs = {"every_n_seconds": interval} if self.frame_mode_var.get() == "每 N 秒" else {"every_n_frames": int(interval)}
            command = lf.build_interval_stills_command(self._ffmpeg(), source, folder,
                                                       image_format=self.frame_format_var.get(),
                                                       overwrite=bool(self.overwrite_var.get()), **kwargs)
            self._start_commands("间隔抽帧", [command], [self.state.duration], [folder])
        except Exception as exc:
            self._error(exc)

    def run_loop(self) -> None:
        if self.app.working:
            return
        try:
            source, output = self._utility_paths()
            duration_text = self.loop_duration_var.get().strip()
            command = lf.build_loop_command(self._ffmpeg(), source, output, loops=int(self.loop_count_var.get()),
                                            duration=float(duration_text) if duration_text else None,
                                            probe=self.state.probe if source == self.state.source else None,
                                            overwrite=bool(self.overwrite_var.get()))
            self._start_commands("loop 流复制", [command], [float(duration_text) if duration_text else self.state.duration], [output])
        except Exception as exc:
            self._error(exc)

    def run_decimate(self) -> None:
        if self.app.working:
            return
        try:
            source, output = self._utility_paths()
            command = lf.build_keyframe_only_command(self._ffmpeg(), source, output,
                                                     mode=self.decimate_mode_var.get(),
                                                     probe=self.state.probe if source == self.state.source else None,
                                                     overwrite=bool(self.overwrite_var.get()))
            self._start_commands("关键帧 decimate", [command], [self.state.duration], [output])
        except Exception as exc:
            self._error(exc)

    def run_fps(self) -> None:
        if self.app.working:
            return
        try:
            source, output = self._utility_paths()
            command = lf.build_fps_change_command(self._ffmpeg(), source, output, self.fps_var.get(),
                                                  overwrite=bool(self.overwrite_var.get()))
            self._start_commands("FPS 重编码", [command], [self.state.duration], [output])
        except Exception as exc:
            self._error(exc)

    def run_hls(self) -> None:
        if self.app.working:
            return
        try:
            source, output = self._utility_paths(hls=True)
            if Path(output).suffix.lower() != ".mkv":
                raise ValueError("HLS 无损封装输出必须使用 .mkv。")
            command = lf.build_hls_to_mkv_command(self._ffmpeg(), source, output,
                                                  overwrite=bool(self.overwrite_var.get()))
            self._start_commands("HLS 转 MKV", [command], [self.state.duration], [output])
        except Exception as exc:
            self._error(exc)

    def _start_commands(self, label: str, commands: Sequence[Sequence[str]], durations: Sequence[float],
                        outputs: Sequence[str], cleanup_paths: Sequence[str] = ()) -> None:
        if self.app.working:
            raise ValueError("已有任务正在运行，请等待完成或先取消。")
        if not commands:
            raise ValueError("没有可执行的 FFmpeg 命令。")
        generation, job_id = self._begin_job()
        self.app.cancel_event.clear()
        self.app._set_working(True)
        self.app._begin_shared_progress(f"{label} 0.0%")
        command_lists = [list(command) for command in commands]
        output_list = [str(path) for path in outputs]
        cleanup = list(cleanup_paths)
        total = max(0.0, sum(max(0.0, value) for value in durations))
        preexisting = {
            os.path.normcase(str(Path(path).resolve()))
            for path in output_list
            if Path(path).exists() or Path(path).is_symlink()
        }
        current_output: Path | None = None

        def remove_new_partial(path: Path) -> None:
            if not path.suffix and not path.is_file():
                return
            if os.path.normcase(str(path.resolve())) in preexisting:
                return
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink()
            except OSError:
                pass

        def worker() -> None:
            nonlocal current_output
            completed_duration = 0.0
            details: list[str] = []
            try:
                for index, command in enumerate(command_lists):
                    if self.app.cancel_event.is_set():
                        raise RuntimeError("任务已取消")
                    current_output = Path(output_list[index]) if index < len(output_list) else None
                    task_duration = max(0.0, float(durations[index])) if index < len(durations) else 0.0
                    self.app.events.put(("log", f"{label} 命令 {index + 1}/{len(command_lists)}：" + lf.quote_command(command)))

                    def report(progress: dict[str, str], base=completed_duration, length=task_duration, number=index) -> None:
                        if job_id != self._active_job:
                            return
                        from exporter_core import progress_seconds
                        if length > 0:
                            task_percent = min(100.0, progress_seconds(progress) / length * 100.0)
                        else:
                            task_percent = 100.0 if progress.get("progress") == "end" else 0.0
                        overall = (base + length * task_percent / 100.0) / total * 100.0 if total > 0 else (
                            (number + task_percent / 100.0) / len(command_lists) * 100.0
                        )
                        self.app.events.put(("editor_export_progress", (overall, f"{label} {number + 1}/{len(command_lists)}", task_percent,
                                                                       progress.get("fps", "0"), progress.get("speed", "0x"))))

                    code, detail = self.app._run_editor_process(command, report)
                    details.append(detail)
                    if code:
                        raise RuntimeError(f"{label} 第 {index + 1} 条命令失败：\n{detail[-3000:]}")
                    current_output = None
                    completed_duration += task_duration
                missing = [path for path in output_list if Path(path).suffix and (not Path(path).is_file() or Path(path).stat().st_size <= 0)]
                if missing:
                    for path in missing:
                        remove_new_partial(Path(path))
                    raise RuntimeError(f"任务结束但没有有效输出：{missing[0]}")
                if self.app.cancel_event.is_set():
                    raise RuntimeError("任务已取消")
                self.app.events.put(("lossless_job_done", (generation, job_id, label, output_list)))
            except Exception as exc:
                if current_output is not None:
                    remove_new_partial(current_output)
                self.app.events.put(("lossless_job_error", (generation, job_id, label, str(exc))))
            finally:
                for path in cleanup:
                    if Path(path).is_dir():
                        _safe_remove_tree(path)
                    else:
                        try:
                            Path(path).unlink(missing_ok=True)
                        except OSError:
                            pass

        threading.Thread(target=worker, daemon=True, name="lossless_ffmpeg").start()

    def handle_job_done(self, payload) -> None:
        legacy = len(payload) == 3
        if legacy:
            generation, label, outputs = payload
            job_id = self._active_job
        else:
            generation, job_id, label, outputs = payload
        if not legacy and not self._job_is_current(generation, job_id):
            self.app._log(f"忽略过期的无损任务完成事件：{label}")
            return
        self.app._set_working(False)
        if generation != self.state.generation:
            self.app._log(f"{label}完成于旧素材会话，未更新当前状态。")
            return
        self.app.progress_var.set(100)
        self.app.progress_text_var.set(f"{label}完成")
        self.summary_var.set(f"{label}完成：{len(outputs)} 个输出")
        self.app._log(f"{label}完成：" + "；".join(outputs))

    def handle_job_error(self, payload) -> None:
        legacy = len(payload) == 3
        if legacy:
            generation, label, detail = payload
            job_id = self._active_job
        else:
            generation, job_id, label, detail = payload
        if not legacy and not self._job_is_current(generation, job_id):
            self.app._log(f"忽略过期的无损任务失败事件：{label}")
            return
        self.app._set_working(False)
        if generation != self.state.generation:
            self.app._log(f"{label}失败于旧素材会话：{detail}")
            return
        cancelled = self.app.cancel_event.is_set() or "任务已取消" in detail
        self.app.progress_text_var.set("已取消" if cancelled else f"{label}失败")
        self.app._log(f"{label}{'取消' if cancelled else '失败'}：{detail}")
        if not cancelled:
            self._error(detail)


def build_lossless_tab(app, parent: tk.Widget) -> LosslessWorkspace:
    workspace = getattr(app, "lossless_workspace", None)
    if workspace is None:
        workspace = LosslessWorkspace(app)
        app.lossless_workspace = workspace
    workspace.build(parent)
    return workspace


__all__ = ["LosslessWorkspace", "LosslessWorkspaceState", "build_lossless_tab"]
