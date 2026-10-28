"""A non-destructive, frame-based multicamera programme and FFmpeg renderer.

All positions are seconds on the continuous master-audio clock.  A clip's
``start`` may be negative; exported programmes start at ``range_start``.  Proxy
paths belong to preview only: rendering always reads ``CameraClip.path``.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Callable

from exporter_core import (
    CREATE_NO_WINDOW, Capabilities, ExportCancelled, ExportSettings, MediaInfo, RollingProgressRate,
    _resolution_height, _target_depth, build_video_args, probe_media,
)
from editor_core import build_audio_encode_args
from multicam_frame_clock import canonical_frame_rate, probe_partition_rates
from multicam_render_plan import partition_render_segments, coalesce_render_segments
from creative_core import (ColorGrade, Transition, effects_to_dict, effects_from_dict,
                           validate_grade, validate_transitions, grade_filter)
from subtitle_core import (SubtitleTrack, subtitle_track_to_dict, subtitle_track_from_dict,
                           validate_subtitle_track, write_ass, subtitle_filter)
from creative_render import (TransitionSegment, plan_creative_segments, has_pixel_effects,
                             normal_effect_filter, build_transition_command)
from program_edits import (ClipGrade, program_edits_to_dict, program_edits_from_dict,
                           validate_program_edits)
from gyroflow_core import (StabilizedRange, StabilizationSettings, validate_range,
                           range_from_dict, cache_matches)
from cache_paths import cache_dir
from cache_publish import publish_completed
from hardware_encoders import HARDWARE_ENCODERS, encoder_vendor, is_hardware_codec
from proxy_cache import find_proxy
from program_edits import grade_at
from gpu_export import (GpuExportUnsupported, find_gpu_export_ffmpeg,
                        build_gpu_video_plan, build_gpu_transition_command)
from export_parallel import choose_export_workers
from library_overlay import (LibraryOverlayRange, validate_library_overlay,
                             overlay_cache_matches, overlay_from_dict)
from video_stack import ordered_cameras, validate_stack


def _number(value, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须是数字")
    value = float(value)
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError(f"{label}无效")
    return value


def _camera(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("机位编号必须是正整数")
    return value


def _frame(seconds: float, fps: float) -> int:
    return math.floor(float(seconds) * fps + 0.5)


@dataclass(slots=True)
class CameraClip:
    id: str
    camera: int
    path: str
    start: float
    duration: float
    proxy_path: str = ""
    camera_make: str = ""
    camera_model: str = ""
    camera_metadata_source: str = ""
    has_alpha: bool = False

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass(slots=True)
class Cut:
    time: float
    camera: int


@dataclass(frozen=True, slots=True)
class MulticamSegment:
    camera: int
    path: str | None
    source_start: float
    start: float
    duration: float
    frames: int
    # Additional edit/grade boundaries retain the original fps sampling phase.
    # None keeps the original fast path for programmes with no local edits.
    phase_origin: float | None = None

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass(slots=True)
class MulticamProject:
    clips: list[CameraClip] = field(default_factory=list)
    cuts: list[Cut] = field(default_factory=list)
    master_audio: str = ""
    master_duration: float = 0.0
    fps: float = 30.0
    range_start: float = 0.0
    range_end: float | None = None
    global_grade: ColorGrade = field(default_factory=ColorGrade)
    camera_grades: dict[int, ColorGrade] = field(default_factory=dict)
    transitions: list[Transition] = field(default_factory=list)
    subtitles: SubtitleTrack = field(default_factory=SubtitleTrack)
    edit_points: list[float] = field(default_factory=list)
    clip_grades: list[ClipGrade] = field(default_factory=list)
    stabilizations: list[StabilizedRange] = field(default_factory=list)
    library_overlays: list[LibraryOverlayRange] = field(default_factory=list)
    alignment_reference_camera: int | None = None
    color_reference_camera: int | None = None
    video_stack_enabled: bool = False
    video_stack_order: list[int] = field(default_factory=list)
    video_stack_hidden: list[int] = field(default_factory=list)
    timeline_empty_tracks: list[int] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, float(self.master_duration), *(clip.end for clip in self.clips))

    @property
    def cameras(self) -> list[int]:
        return sorted({clip.camera for clip in self.clips})

    @property
    def timeline_cameras(self) -> list[int]:
        return sorted(set(self.cameras) | set(self.timeline_empty_tracks))

    def stacked_cameras(self, *, include_hidden=False) -> list[int]:
        """Timeline layers ordered from the topmost picture to the bottom."""
        return ordered_cameras(self, include_hidden=include_hidden)

    def clip_at(self, camera: int, time: float) -> CameraClip | None:
        # If imported files overlap within one camera, the latest recording
        # wins.  This is deterministic and does not discard either source.
        candidates = [clip for clip in self.clips
                      if clip.camera == camera and clip.start <= time < clip.end]
        return max(candidates, key=lambda clip: (clip.start, clip.id), default=None)

    def camera_at(self, time: float) -> int | None:
        camera = min(self.cameras, default=None)
        position = _frame(time, self.fps)
        for cut in sorted(self.cuts, key=lambda item: item.time):
            if _frame(cut.time, self.fps) > position:
                break
            camera = cut.camera
        return camera

    def stabilization_at(self, camera: int, time: float, source_id: str = ''):
        return next((item for item in reversed(self.stabilizations)
                     if item.camera == camera and (not source_id or item.source_id == source_id)
                     and _frame(item.start, self.fps) <= _frame(time, self.fps) < _frame(item.end, self.fps)), None)

    def media_at(self, camera: int, time: float, use_proxies: bool = False):
        """Resolve original/proxy/stabilized pictures on the same master clock."""
        clip = self.clip_at(camera, time)
        if clip is None: return None
        source_time = max(0.0, time - clip.start)
        overlay = self.library_overlay_at(camera, time, clip.id)
        if overlay is not None:
            if not overlay_cache_matches(overlay, self):
                raise ValueError('素材叠加缓存已失效，请重新应用或移除所选素材特效。')
            return (overlay.cache_path, max(0., source_time-overlay.source_start),
                    ('library-overlay', overlay.cache_path, overlay.signature, clip.id, clip.start))
        stabilized = self.stabilization_at(camera, time, clip.id)
        if (stabilized is not None and cache_matches(stabilized,clip.path)
                and stabilized.source_start - 1e-6 <= source_time < stabilized.source_end + 1e-6):
            return (stabilized.cache_path, max(0.0, source_time-stabilized.source_start),
                    ('stabilized', stabilized.cache_path, stabilized.signature, clip.id, clip.start))
        path = clip.path
        if use_proxies:
            proxy = clip.proxy_path
            if not proxy:
                proxy = find_proxy(clip.path) or str(Path(clip.path).parent/'ProxyMedia'/(Path(clip.path).stem+'.mp4'))
            if Path(proxy).is_file(): path = proxy
        return path, source_time, (path, clip.id, clip.start)

    def library_overlay_at(self, camera: int, time: float, source_id: str = ''):
        return next((item for item in reversed(self.library_overlays)
                     if item.camera == camera and (not source_id or item.source_id == source_id)
                     and item.start <= time < item.end), None)

    def set_cut(self, time: float, camera: int) -> None:
        time = _number(time, "切换时间", minimum=0.0)
        _number(self.fps, "工程帧率", minimum=0.001)
        camera = _camera(camera)
        if camera not in self.cameras:
            raise ValueError("切换引用了不存在的机位")
        if time > self.duration + 1e-6:
            raise ValueError("切换时间超过工程时长")
        frame = _frame(time, self.fps)
        by_frame = {_frame(cut.time, self.fps): cut.camera for cut in self.cuts}
        by_frame.setdefault(0, min(self.cameras))
        by_frame[frame] = camera
        normalized: list[Cut] = []
        previous = None
        for index, selected in sorted(by_frame.items()):
            if selected != previous:
                normalized.append(Cut(index / self.fps, selected))
                previous = selected
        self.cuts = normalized

    def validate(self) -> None:
        fps = _number(self.fps, "工程帧率", minimum=0.001)
        if fps > 1000:
            raise ValueError("工程帧率不能超过 1000")
        _number(self.master_duration, "主音频时长", minimum=0.0)
        _number(self.range_start, "成片开始", minimum=0.0)
        if not isinstance(self.master_audio, str) or "\x00" in self.master_audio:
            raise ValueError("主音频路径无效")
        if not isinstance(self.clips, list) or not isinstance(self.cuts, list):
            raise ValueError("工程素材和切点必须是列表")
        if len(self.clips) > 100_000 or len(self.cuts) > 1_000_000:
            raise ValueError("工程素材或切点数量过多")
        identifiers: set[str] = set()
        for clip in self.clips:
            if not isinstance(clip, CameraClip):
                raise ValueError("素材结构无效")
            if not isinstance(clip.id, str) or not clip.id or clip.id in identifiers:
                raise ValueError("素材 ID 不能为空或重复")
            identifiers.add(clip.id)
            _camera(clip.camera)
            if type(clip.has_alpha) is not bool:
                raise ValueError('素材透明通道标记必须是布尔值')
            for value in (clip.camera_make,clip.camera_model,clip.camera_metadata_source):
                if not isinstance(value,str) or len(value) > 1000 or any(char in value for char in '\x00\r\n'):
                    raise ValueError('相机厂家或型号信息无效')
            for name, value in (("素材", clip.path), ("代理", clip.proxy_path)):
                if not isinstance(value, str) or "\x00" in value or (name == "素材" and not value):
                    raise ValueError(f"{name}路径无效")
            _number(clip.start, "素材起点")
            if _number(clip.duration, "素材时长", minimum=0.0) <= 0:
                raise ValueError("素材时长必须大于零")
            if not math.isfinite(clip.end):
                raise ValueError("素材终点无效")
        available = set(self.cameras)
        validate_stack(self)
        if self.alignment_reference_camera is not None:
            if type(self.alignment_reference_camera) is not int or self.alignment_reference_camera not in available:
                raise ValueError('同步参考机位不存在')
        if self.color_reference_camera is not None:
            if type(self.color_reference_camera) is not int or self.color_reference_camera not in available:
                raise ValueError('色彩参考机位不存在')
        for cut in self.cuts:
            if not isinstance(cut, Cut):
                raise ValueError("切点结构无效")
            _number(cut.time, "切换时间", minimum=0.0)
            if _camera(cut.camera) not in available:
                raise ValueError("切点引用了不存在的机位")
            if cut.time > self.duration + 1.0 / fps:
                raise ValueError("切点超过工程时长")
        if self.range_end is not None:
            end = _number(self.range_end, "成片结束", minimum=0.0)
            if end <= self.range_start:
                raise ValueError("成片结束必须晚于开始")
            if end > self.duration + 1e-6:
                raise ValueError("成片结束超过工程时长")
        if self.duration > 0 and self.range_start >= self.duration:
            raise ValueError("成片开始超过工程时长")
        validate_grade(self.global_grade, check_lut=False)
        if not isinstance(self.camera_grades, dict):
            raise ValueError("机位调色设置无效")
        for camera, grade in self.camera_grades.items():
            _camera(camera)
            validate_grade(grade, check_lut=False)
        validate_program_edits(self)
        if not isinstance(self.stabilizations, list) or len(self.stabilizations) > 100_000:
            raise ValueError('防抖片段设置无效')
        for item in self.stabilizations:
            validate_range(item, check_files=False)
            clip = next((clip for clip in self.clips if clip.id == item.source_id), None)
            if clip is None or clip.camera != item.camera:
                raise ValueError('防抖片段引用了不存在的机位或源素材')
            if item.start < 0 or item.end > self.duration + 1/self.fps:
                raise ValueError('防抖片段超出工程范围')
            if (item.start-clip.start < item.source_start-1/self.fps
                    or item.end-clip.start > item.source_end+1/self.fps):
                raise ValueError('防抖缓存的源时间没有覆盖所选片段，请重新生成防抖')
        validate_transitions(self.transitions, self)
        validate_subtitle_track(self.subtitles)
        if not isinstance(self.library_overlays, list) or len(self.library_overlays) > 100000:
            raise ValueError('素材叠加列表无效')
        for item in self.library_overlays:
            validate_library_overlay(item)
            clip = next((clip for clip in self.clips if clip.id == item.source_id), None)
            if clip is None or clip.camera != item.camera:
                raise ValueError('素材叠加引用了不存在的机位或原片')
            if (item.end > self.duration+1/fps or item.start < clip.start-1/fps
                    or item.end > clip.end+1/fps or item.start-clip.start < item.source_start-1/fps
                    or item.end-clip.start > item.source_end+1/fps):
                raise ValueError('素材叠加缓存未覆盖所选范围，请重新应用或移除素材特效。')
            for effect in self.transitions:
                for sample in (effect.time-1/fps, effect.time):
                    if (item.start <= sample < item.end and self.camera_at(sample) == item.camera
                            and (effect.time-effect.duration/2-clip.start < item.source_start-1/fps
                                 or effect.time+effect.duration/2-clip.start > item.source_end+1/fps)):
                        raise ValueError('素材叠加缓存未覆盖新转场的余量，请重新应用素材特效。')

    def segments(self, start: float | None = None, end: float | None = None) -> list[MulticamSegment]:
        self.validate()
        if not self.clips:
            return []
        start = self.range_start if start is None else _number(start, "成片开始", minimum=0.0)
        end = (self.range_end if self.range_end is not None else self.duration) if end is None else _number(end, "成片结束", minimum=0.0)
        if end <= start or end > self.duration + 1e-6:
            raise ValueError("成片范围无效或超过工程时长")
        first, last = _frame(start, self.fps), _frame(end, self.fps)
        if first >= last:
            raise ValueError("成片范围不足一帧")
        # All cumulative boundaries share one frame clock, preventing drift
        # across hundreds of fractional clip edges and programme cuts.
        edges = {first, last}
        edges.update(_frame(cut.time, self.fps) for cut in self.cuts)
        protected_edges = {_frame(value, self.fps) for value in self.edit_points}
        for item in self.clip_grades:
            protected_edges.update((_frame(item.start, self.fps), _frame(item.end, self.fps)))
        for item in self.stabilizations:
            protected_edges.update((_frame(item.start, self.fps), _frame(item.end, self.fps)))
        for item in self.library_overlays:
            protected_edges.update((_frame(item.start, self.fps), _frame(item.end, self.fps)))
        edges.update(protected_edges)
        for clip in self.clips:
            edges.update((_frame(clip.start, self.fps), _frame(clip.end, self.fps)))
        boundaries = sorted(edge for edge in edges if first <= edge <= last)
        by_frame = {_frame(cut.time, self.fps): cut.camera
                    for cut in sorted(self.cuts, key=lambda item: item.time)}
        camera = min(self.cameras)
        cut_frames = sorted(by_frame)
        cut_index = 0
        result: list[MulticamSegment] = []
        previous_clip: CameraClip | None = None
        previous_camera: int | None = None
        phase_origin: float | None = None
        for left, right in zip(boundaries, boundaries[1:]):
            while cut_index < len(cut_frames) and cut_frames[cut_index] <= left:
                camera = by_frame[cut_frames[cut_index]]
                cut_index += 1
            # Select using quantized coverage: off-grid starts cannot produce
            # a spurious missing frame at an otherwise valid clip boundary.
            candidates = [clip for clip in self.clips if clip.camera == camera
                          and _frame(clip.start, self.fps) <= left
                          and _frame(clip.end, self.fps) >= right]
            clip = max(candidates, key=lambda item: (item.start, item.id), default=None)
            position = left / self.fps
            source = max(0.0, position - clip.start) if clip else 0.0
            picture_path = clip.path if clip else None
            stabilized = self.stabilization_at(camera, position, clip.id) if clip else None
            if stabilized is not None:
                if not Path(stabilized.cache_path).is_file():
                    raise ValueError('防抖缓存缺失，请重新生成防抖或移除该片段防抖：' + stabilized.cache_path)
                if not cache_matches(stabilized,clip.path):
                    raise ValueError('原素材或防抖设置已变化，请重新生成或移除所选片段防抖')
                source = max(0.0, source-stabilized.source_start)
                picture_path = stabilized.cache_path
            overlay = self.library_overlay_at(camera, position, clip.id) if clip else None
            if overlay is not None:
                if not overlay_cache_matches(overlay, self, strict=True):
                    raise ValueError('素材叠加缓存缺失或原片/特效/防抖已变化，请重新应用或移除素材特效。')
                source = max(0.0, position-clip.start-overlay.source_start)
                picture_path = overlay.cache_path
            if not result or camera != previous_camera or clip is not previous_clip:
                phase_origin = position
            preserve_phase = phase_origin if protected_edges else None
            if stabilized is not None:
                preserve_phase = max(0.0, clip.start+stabilized.source_start)
            if overlay is not None:
                preserve_phase = max(0.0, clip.start+overlay.source_start)
            if (result and result[-1].camera == camera and clip is previous_clip
                    and result[-1].path == picture_path
                    and left not in protected_edges):
                previous = result[-1]
                frames = previous.frames + right - left
                result[-1] = replace(previous, duration=frames / self.fps, frames=frames)
            else:
                result.append(MulticamSegment(camera, picture_path,
                                             source, position, (right - left) / self.fps,
                                             right - left, preserve_phase))
            previous_clip = clip
            previous_camera = camera
        return result

    def to_payload(self, base_directory: str | Path) -> dict:
        """Serialize the complete edit for a standalone or enclosing project.

        File paths are relative to the directory containing that project;
        no temporary project file or filesystem write is needed.
        """
        self.validate()
        base = Path(base_directory).resolve()

        def relative(value: str) -> str:
            if not value:
                return ""
            try:
                return os.path.relpath(Path(value).resolve(), base)
            except ValueError:  # Different Windows drive letters.
                return str(Path(value).resolve())

        payload = asdict(self)
        payload.update(format="okk-multicam", version=9 if self.timeline_empty_tracks else 8)
        if not self.timeline_empty_tracks: payload.pop("timeline_empty_tracks")
        payload.update(effects_to_dict(self.global_grade, self.camera_grades, self.transitions))
        payload.update(program_edits_to_dict(self))
        payload["subtitles"] = subtitle_track_to_dict(self.subtitles)
        payload["master_audio"] = relative(self.master_audio)
        for clip in payload["clips"]:
            clip["path"] = relative(clip["path"])
            clip["proxy_path"] = relative(clip["proxy_path"])
        for grade in [payload["global_grade"], *payload["camera_grades"].values(),
                      *(item["grade"] for item in payload["clip_grades"])]:
            grade["lut_path"] = relative(grade["lut_path"])
        payload["subtitles"]["source_path"] = relative(self.subtitles.source_path)
        for item in payload['stabilizations']:
            item['cache_path'] = relative(item['cache_path'])
            for name in ('gyro_path', 'lens_path', 'project_path', 'gyroflow_path'):
                item['settings'][name] = relative(item['settings'][name])
        for item in payload['library_overlays']:
            for name in ('asset_path', 'cache_path'):
                item[name] = relative(item[name])
        for item in payload['transitions']:
            if 'mask_path' in item:
                item['mask_path'] = relative(item['mask_path'])
        return payload

    def save(self, path: str | Path) -> None:
        destination = Path(path).resolve()
        payload = self.to_payload(destination.parent)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                             prefix=".okk_project_", suffix=".json", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @classmethod
    def load(cls, path: str | Path) -> MulticamProject:
        source = Path(path).resolve()
        if source.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("工程文件过大")
        try:
            payload = json.loads(source.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ValueError(f"工程文件损坏：{exc}") from exc
        return cls.from_payload(payload, source.parent)

    @classmethod
    def from_payload(cls, payload: dict, base_directory: str | Path) -> MulticamProject:
        """Restore and validate an embedded edit without mutating its payload."""
        base = Path(base_directory).resolve()
        try:
            if (not isinstance(payload, dict) or payload.get("format") != "okk-multicam"
                    or type(payload.get("version")) is not int or payload.get("version") not in {1, 2, 3, 4, 5, 6, 7, 8, 9}):
                raise ValueError("不是受支持的多机位工程")
            allowed = {"format", "version", "clips", "cuts", "master_audio", "master_duration", "fps",
                       "range_start", "range_end", "global_grade", "camera_grades", "transitions", "subtitles",
                       "edit_points", "clip_grades", "stabilizations", "library_overlays", "alignment_reference_camera", "color_reference_camera",
                       "video_stack_enabled", "video_stack_order", "video_stack_hidden", "timeline_empty_tracks"}
            if set(payload) - allowed:
                raise ValueError("工程包含未知字段")

            def absolute(value):
                if not isinstance(value, str) or "\x00" in value:
                    raise ValueError("工程中的文件路径无效")
                return str((base / value).resolve()) if value else ""

            raw_clips = payload.get("clips", [])
            raw_cuts = payload.get("cuts", [])
            if not isinstance(raw_clips, list) or not isinstance(raw_cuts, list):
                raise ValueError("工程素材或切点结构无效")
            clips = []
            for item in raw_clips:
                if not isinstance(item, dict):
                    raise ValueError("工程素材结构无效")
                values = dict(item)
                values["path"] = absolute(values.get("path"))
                values["proxy_path"] = absolute(values.get("proxy_path", ""))
                clips.append(CameraClip(**values))
            cuts = [Cut(**item) for item in raw_cuts]
            grade, camera_grades, transitions = effects_from_dict({
                key: payload[key] for key in ("global_grade", "camera_grades", "transitions") if key in payload})
            edit_points, clip_grades = program_edits_from_dict({
                key: payload[key] for key in ("edit_points", "clip_grades") if key in payload})
            for item in [grade, *camera_grades.values(), *(item.grade for item in clip_grades)]:
                item.lut_path = absolute(item.lut_path)
            for item in transitions:
                if hasattr(item, 'mask_path'):
                    item.mask_path = absolute(item.mask_path)
            subtitles = subtitle_track_from_dict(payload.get("subtitles", {}))
            subtitles.source_path = absolute(subtitles.source_path)
            stabilizations = []
            raw_stabilizations = payload.get('stabilizations', [])
            if not isinstance(raw_stabilizations, list): raise ValueError('防抖设置必须是列表')
            for item in raw_stabilizations:
                stabilizations.append(range_from_dict(item,absolute=absolute))
            raw_overlays = payload.get('library_overlays', [])
            if not isinstance(raw_overlays, list):
                raise ValueError('素材叠加必须是列表')
            overlays = [overlay_from_dict(item, absolute) for item in raw_overlays]
            project = cls(clips=clips, cuts=cuts,
                          master_audio=absolute(payload.get("master_audio", "")),
                          master_duration=payload.get("master_duration", 0.0),
                          fps=payload.get("fps", 30.0),
                          range_start=payload.get("range_start", 0.0),
                          range_end=payload.get("range_end"), global_grade=grade,
                          camera_grades=camera_grades, transitions=transitions, subtitles=subtitles,
                          edit_points=edit_points, clip_grades=clip_grades, stabilizations=stabilizations,
                          library_overlays=overlays,
                          alignment_reference_camera=payload.get('alignment_reference_camera'),
                          color_reference_camera=payload.get('color_reference_camera'),
                          video_stack_enabled=payload.get('video_stack_enabled', False),
                          video_stack_order=payload.get('video_stack_order', []),
                          video_stack_hidden=payload.get('video_stack_hidden', []),
                          timeline_empty_tracks=payload.get('timeline_empty_tracks', []))
            project.validate()
            return project
        except (json.JSONDecodeError, TypeError, KeyError, OverflowError) as exc:
            raise ValueError(f"工程文件损坏：{exc}") from exc


def _progress_seconds(snapshot: dict) -> float:
    try:
        return max(0.0, float(snapshot.get("out_time_us", snapshot.get("out_time_ms", 0))) / 1_000_000)
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _audio_stream_info(ffprobe: str, path: str) -> tuple[dict, float]:
    result = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a:0",
                             "-show_entries", "stream=index,duration,codec_name,sample_rate:format=duration", "-of", "json", path],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            encoding="utf-8", errors="replace", timeout=30,
                            creationflags=CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(f"无法读取主音频：{result.stderr[-1600:]}")
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    raw_duration = (streams[0].get("duration") if streams else None) or payload.get("format", {}).get("duration", 0)
    try:
        duration = float(raw_duration)
    except (ValueError, TypeError):
        duration = 0.0
    return streams[0] if streams else {}, duration if math.isfinite(duration) and duration > 0 else 0.0


def _audio_info(ffprobe: str, path: str) -> tuple[bool, float]:
    stream, duration = _audio_stream_info(ffprobe, path)
    return bool(stream), duration


def _delivery_audio_sample_rate(settings: ExportSettings, source: dict) -> int:
    sample_rate = getattr(settings, "audio_sample_rate", 48000)
    if sample_rate is None:
        try:
            sample_rate = int(source.get("sample_rate") or 48000)
        except (ValueError, TypeError, OverflowError):
            sample_rate = 48000
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 384000:
        raise ValueError("音频采样率应在 8000 到 384000 Hz 之间。")
    return sample_rate


def _delivery_audio_args(settings: ExportSettings, output: Path, source: dict,
                         audio_start: float, duration: float, available: float) -> list[str]:
    """Validate before encoding picture; stream-copy never silently transcodes."""
    codec = str(settings.audio).lower()
    if codec == "none":
        return []
    if codec == "copy":
        if not source or available <= 0 or audio_start < -1e-9 or audio_start + duration > available + 1e-6:
            raise ValueError("音频复制需要连续主音频完整覆盖导出范围；需要补静音时请选择 AAC、PCM 或 FLAC。")
        if audio_start > 1e-9:
            raise ValueError("音频复制无法保证截取起点的采样精度；导出局部范围请选择 AAC、PCM 或 FLAC。")
        name = str(source.get("codec_name", ""))
        pcm = {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_s16be", "pcm_s24be", "pcm_s32be"}
        allowed = {"aac", "alac", "mp3", "ac3", "eac3"}
        if output.suffix.lower() == ".mov":
            allowed |= pcm
        elif output.suffix.lower() == ".mkv":
            allowed |= pcm | {"flac", "opus", "vorbis", "dts", "truehd"}
        if name not in allowed:
            raise ValueError(f"{output.suffix.upper()} 不支持复制 {name or '未知'} 音频，请选择兼容容器或转换音频编码。")
        return ["-c:a", "copy"]
    sample_rate = _delivery_audio_sample_rate(settings, source)
    if codec == "flac":
        if output.suffix.lower() != ".mkv":
            raise ValueError("FLAC 无损音频请选择 MKV 容器；MOV/MP4 请使用 PCM 或 AAC。")
        return ["-c:a", "flac", "-ar", str(sample_rate)]
    return build_audio_encode_args(codec, settings.audio_bitrate or "320k", sample_rate, str(output))


def _cuda_segment_filter(media: MediaInfo, settings: ExportSettings, caps: Capabilities,
                         width: int, height: int, depth: int, rate_text: str,
                         frames: int, duration: float, prefix_frames: int = 0) -> str | None:
    """Keep compatible camera pixels on the GPU; metadata filters only clone refs.

    CUDA padding does not support the 10-bit surfaces used by this renderer.
    A camera that needs letterboxing therefore retains the proven software
    normalization path, as do unsupported decoders and pixel formats.
    """
    if not (settings.decode_mode == "cuda" and settings.codec.endswith("_nvenc")
            and caps.cuda_decode and media.video_codec in {"h264", "hevc", "av1", "vp9"}
            and media.pixel_format in {"yuv420p", "yuvj420p", "nv12", "yuv420p10le", "p010le"}
            and media.width > 0 and media.height > 0
            and media.width * height == media.height * width):
        return None
    conversion = media.width != width or media.height != height or media.bit_depth != depth
    if conversion and not caps.scale_cuda:
        return None
    filters = f"setpts=PTS-STARTPTS,fps=fps={rate_text}:start_time=0"
    if conversion:
        surface = "p010le" if depth == 10 else "nv12"
        filters += f",scale_cuda={width}:{height}:format={surface}:interp_algo=bicubic:passthrough=0"
    # stop_mode=clone works with hardware frames: it references the final CUDA
    # surface rather than drawing or downloading each padded frame.
    trim = (f"trim=start_frame={prefix_frames}:end_frame={prefix_frames + frames}" if prefix_frames
            else f"trim=end_frame={frames}")
    return (filters + f",setsar=1,tpad=stop_mode=clone:stop_duration={duration + 1:.9f},"
            f"{trim},settb=AVTB,setpts=N/({rate_text}*TB)")


def render_multicam(
    project: MulticamProject,
    output: str | Path,
    ffmpeg: str,
    ffprobe: str,
    settings: ExportSettings,
    caps: Capabilities,
    run_process: Callable,
    progress: Callable[[float, float, str], None] | None = None,
    cancel_event=None,
    *,
    metrics: Callable[[dict], None] | None = None,
) -> str:
    """Render programme segments, then copy picture and mux master audio once.

    ``run_process(command, callback)`` uses the application's existing process
    registration/cancellation runner and returns ``(returncode, diagnostics)``.
    Normal commands open one camera; stack jobs open their active layers.
    Temporary files and an atomic
    final replacement keep an existing destination intact on failure/cancel.
    """
    project.validate()
    # The UI stores a rounded decimal (e.g. 59.94006). Use one canonical clock
    # for frame selection, seeks, task boundaries, and encoded stream timing.
    project = replace(project, fps=float(canonical_frame_rate(project.fps)))
    from video_stack_render import plan_stack_segments, validate_stack_media, build_stack_command
    stacked = project.video_stack_enabled
    segments = plan_stack_segments(project) if stacked else project.segments()
    if not segments:
        raise ValueError("请先添加相机素材")
    if not stacked:
        segments = plan_creative_segments(project, segments)
        segments = coalesce_render_segments(project, segments)
    for grade in [project.global_grade, *project.camera_grades.values(),
                  *(item.grade for item in project.clip_grades)]:
        validate_grade(grade)
    destination = Path(output).resolve()
    if destination.suffix.lower() not in {".mp4", ".mov", ".mkv", ".m4v"}:
        raise ValueError("成片文件请选择 MP4、MOV 或 MKV")
    protected = {Path(value).resolve() for clip in project.clips
                 for value in (clip.path, clip.proxy_path) if value}
    if project.master_audio:
        protected.add(Path(project.master_audio).resolve())
    protected.update(Path(item.cache_path).resolve() for item in project.stabilizations)
    protected.update(Path(value).resolve() for item in project.library_overlays
                     for value in (item.cache_path, item.asset_path))
    if destination in protected:
        raise ValueError("成片输出不能覆盖原素材、代理或主音频")
    destination.parent.mkdir(parents=True, exist_ok=True)

    def cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ExportCancelled("多机位导出已取消")

    cancelled()
    media: dict[str, MediaInfo] = {}
    for clip in project.clips:
        cancelled()
        if not Path(clip.path).is_file():
            raise ValueError(f"原始素材不存在：{clip.path}")
        if clip.path not in media:
            media[clip.path] = probe_media(ffprobe, clip.path)
    reference = media[project.clips[0].path]
    for segment in segments:
        pictures = ([layer.path for layer in segment.layers] if stacked else
                    (segment.path, getattr(segment, 'other_path', None)))
        for picture in pictures:
            if picture and picture not in media:
                if not Path(picture).is_file(): raise ValueError('防抖或转场素材不存在：' + picture)
                media[picture] = probe_media(ffprobe, picture)
    if stacked:
        validate_stack_media(segments, media)
    if reference.width <= 0 or reference.height <= 0:
        raise ValueError("无法读取素材画面尺寸")
    height = _resolution_height(settings.resolution) or reference.height
    width = max(2, round(reference.width * height / reference.height / 2) * 2)
    height = max(2, round(height / 2) * 2)
    if settings.codec not in {"libx264", "libx265", *HARDWARE_ENCODERS}:
        raise ValueError("多机位导出暂不支持此视频编码器")
    depth = _target_depth(settings, reference)
    if settings.codec in {"h264_nvenc", "h264_amf", "h264_qsv"}:
        depth = 8
    # Software fallback normalizes mixed aspect ratios and unsupported inputs;
    # compatible camera chunks retain CUDA surfaces throughout the pipeline.
    pixel_format = "yuv420p10le" if depth == 10 else "yuv420p"
    rate = Fraction(project.fps).limit_denominator(100_000)
    rate_text = f"{rate.numerator}/{rate.denominator}"
    duration = sum(segment.frames for segment in segments) / project.fps
    timeline_start = segments[0].start
    encode_settings = replace(settings, extra_args=list(settings.extra_args))
    encode_reference = replace(reference, width=width, height=height, fps=project.fps,
                               bit_depth=depth, pixel_format=pixel_format,
                               sample_aspect_ratio="1:1")
    if is_hardware_codec(settings.codec):
        encode_args = build_video_args(encode_settings, encode_reference, caps)
        if settings.codec.endswith("_nvenc"):
            encode_args += ["-gpu", str(settings.gpu_index)]
            # Delivery exposes the B-frame count explicitly, including P1/P2.
            encode_args[encode_args.index("-bf") + 1] = str(settings.bframes)
        # This renderer normalizes every input with fps= and supplies the same
        # -r for each independent chunk.  The shared single-file exporter uses
        # passthrough, which FFmpeg rejects alongside -r; multicamera chunks
        # instead require one common constant frame rate for concat-copy.
        mode_index = encode_args.index("-fps_mode")
        encode_args[mode_index + 1] = "cfr"
    else:
        preset = settings.preset if settings.preset in {
            "ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
            "slow", "slower", "veryslow", "placebo",
        } else "veryfast"
        encode_args = ["-c:v", settings.codec, "-preset", preset,
                       "-g", str(settings.gop), "-bf", str(settings.bframes)]
        if settings.video_bitrate.strip():
            encode_args += ["-b:v", settings.video_bitrate.strip()]
        else:
            encode_args += ["-crf", str(settings.quality)]
        if settings.maxrate.strip():
            encode_args += ["-maxrate", settings.maxrate.strip(), "-bufsize", settings.maxrate.strip()]
        encode_args += list(settings.extra_args)

    audio_path = project.master_audio
    audio_duration = 0.0
    audio_source: dict = {}
    audio_origin = 0.0
    if audio_path and settings.audio != "none":
        if not Path(audio_path).is_file():
            raise ValueError(f"主音频不存在：{audio_path}")
        audio_source, audio_duration = _audio_stream_info(ffprobe, audio_path)
        if not audio_source:
            audio_path = ""
    elif not audio_path and settings.audio != "none":
        audio_clip = next((clip for clip in project.clips if media[clip.path].has_audio), None)
        if audio_clip is not None:
            audio_path, audio_origin = audio_clip.path, audio_clip.start
            audio_source, audio_duration = _audio_stream_info(ffprobe, audio_path)
    audio_start = timeline_start - audio_origin
    audio_args = _delivery_audio_args(settings, destination, audio_source, audio_start, duration, audio_duration)
    audio_sample_rate = (_delivery_audio_sample_rate(settings, audio_source)
                         if settings.audio not in {"none", "copy"} else 48000)
    if audio_duration > 0 and audio_start >= audio_duration:
        audio_path = ""
    base = [str(ffmpeg), "-hide_banner", "-y", "-loglevel", "warning",
            "-stats_period", "0.25", "-progress", "pipe:1", "-nostats"]
    gpu_effect_ffmpeg = (find_gpu_export_ffmpeg() if not stacked and settings.decode_mode == 'cuda'
        and settings.codec.endswith('_nvenc') and Path(ffmpeg).is_file() else '')
    workers = getattr(settings, "parallel_workers", 1)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 4:
        raise ValueError("并行编码数量请选择 1 到 4。")
    workers=choose_export_workers(settings,caps,media,output_size=(width,height))
    if stacked:
        # A stack already decodes several cameras concurrently. Bound the
        # number of concurrent stacks so 36-camera work cannot multiply into
        # four independent 36-decoder jobs and exhaust memory.
        active_layers = max((len(segment.layers) for segment in segments), default=0)
        workers = min(workers, max(1, 8 // max(1, active_layers)))
    needs_phase = any(getattr(segment, "phase_origin", None) is not None
                      and segment.phase_origin < segment.start - .5 / project.fps for segment in segments)
    source_rates = probe_partition_rates(ffprobe, media, cancel_event) if stacked or workers > 1 or needs_phase else {}
    if workers > 1 and not stacked:
        segments = [chunk for segment in segments for chunk in (
            [segment] if isinstance(segment, TransitionSegment) else
            partition_render_segments([segment], project.fps, workers, source_frame_rates=source_rates))]
    workers = min(workers, len(segments))
    total_frames = sum(segment.frames for segment in segments)
    started = time.monotonic()
    lock = threading.RLock()
    aborted = threading.Event()
    processed_frames = [0] * len(segments)
    completed_segments = [False] * len(segments)
    active: dict[int, tuple[float, float, float]] = {}
    gpu_disabled: dict[str,str] = {}
    trace_lock = threading.Lock()
    with tempfile.NamedTemporaryFile(prefix='multicam_',suffix='.jsonl',
            dir=cache_dir('logs/exports'),delete=False) as stream:
        trace_path = Path(stream.name)

    def trace(event,**data):
        # One event per attempt, not per frame. Retain diagnostics after the
        # temporary chunks are cleaned, without blocking FFmpeg pipe draining.
        with trace_lock:
            with trace_path.open('a',encoding='utf8') as stream:
                stream.write(json.dumps({'event':event,'elapsed':time.monotonic()-started,**data},
                                        ensure_ascii=False)+'\n')

    def disabled_gpu_reason(path):
        with lock:return gpu_disabled.get('*') or gpu_disabled.get(path,'')

    def disable_failed_gpu(path,details):
        reason=str(details)
        device_failure=any(token in reason.lower() for token in (
            'vk_error_','vkqueuesubmit','unable to submit command','device lost',
            'failed creating vulkan device','failed to create vulkan device',
            'out of device memory','cuda_error_'))
        with lock:gpu_disabled['*' if device_failure else path]=reason
        trace('gpu_route_disabled',source=path,scope='export' if device_failure else 'source',reason=reason)
    mux_started = 0.0
    mux_seconds = 0.0
    mux_rate = RollingProgressRate(clock=lambda: time.monotonic())

    def publish(phase: str, *, mux: bool = False, finished: bool = False, mux_speed: float = 0.0) -> None:
        # Call only under lock. Serializing delivery keeps parallel worker
        # notifications in chronological order for the UI's event queue.
        elapsed = max(0.0, time.monotonic() - started)
        frames = sum(processed_frames)
        seconds = mux_seconds if mux else frames / project.fps
        fraction = (0.95 + 0.05 * seconds / duration) if mux else 0.95 * frames / total_frames
        percent = 100.0 if finished else min(99.9, max(0.0, 100.0 * fraction))
        now = time.monotonic()
        fps = 0.0 if mux or finished else sum(value[0] for value in active.values() if now - value[2] <= 3.0)
        speed = mux_speed if mux and not finished else (fps / project.fps if not finished else 0.0)
        stage_elapsed = max(0.0, time.monotonic() - mux_started) if mux else elapsed
        eta = 0.0 if finished else (max(0.0, duration - seconds) * stage_elapsed / seconds if seconds > 0 else None)
        if not mux and not finished:
            phase += f" · 已完成 {sum(completed_segments)}/{len(segments)}"
        if progress is not None:
            progress(duration * percent / 100.0, duration, phase)
        if metrics is not None:
            metrics({"phase": phase, "progress_percent": percent,
                     "processed_seconds": seconds, "total_seconds": duration,
                     "encoded_frames": frames, "fps": fps, "speed": speed,
                     "average_fps": frames/elapsed if elapsed else 0.,
                     "diagnostic_path":str(trace_path),
                     "elapsed": elapsed, "eta": eta,
                     "stage": "done" if finished else "muxing" if mux else "encoding",
                     "completed_segments": sum(completed_segments), "total_segments": len(segments),
                     "active_workers": 1 if mux and not finished else len(active)})

    def check_output(code: int, details, expected: Path, phase: str) -> None:
        cancelled()
        if code or not expected.is_file() or expected.stat().st_size <= 0:
            aborted.set()
            trace('output_error',phase=phase,returncode=code,details=str(details),expected=str(expected))
            raise RuntimeError(f"{phase}失败：\n{str(details)[-3000:]}\n导出日志：{trace_path}")

    with lock:
        publish("准备多机位导出")
    trace('export_start',source_count=len(media),output=str(destination),width=width,height=height,
          frames=total_frames,rate=rate_text,workers=workers)
    with tempfile.TemporaryDirectory(prefix=".okk_multicam_", dir=cache_dir('exports')) as folder:
        temporary = Path(folder)
        ass_path = None
        if project.subtitles.enabled and project.subtitles.burn_in and project.subtitles.cues:
            ass_path = write_ass(project.subtitles, temporary / 'subtitles.ass', width, height)
        encoded = [temporary / f"camera_{index:06d}.mp4" for index in range(len(segments))]

        def encode_segment(index: int) -> None:
            cancelled()
            if aborted.is_set():
                return
            segment = segments[index]
            chunk = encoded[index]
            seek_start = segment.source_start
            prefix_frames = 0
            origin = getattr(segment, "phase_origin", None)
            if segment.path and origin is not None and origin < segment.start - .5 / project.fps:
                offset_frames = _frame(segment.start - origin, project.fps)
                source_rate = source_rates.get(segment.path)
                # Known CFR needs only a short prefix (one conversion
                # period). Unknown/VFR keeps the exact original fps phase by
                # decoding from the original continuous segment's origin.
                quantum = (rate / source_rate).numerator if source_rate else None
                prefix_frames = offset_frames % quantum if quantum else offset_frames
                seek_start = max(0.0, seek_start - prefix_frames / project.fps)
            phase = (f"转场 {segment.camera} → {segment.other_camera}（任务 {index + 1}/{len(segments)}）"
                     if isinstance(segment, TransitionSegment) else
                     f"编码机位 {segment.camera}（任务 {index + 1}/{len(segments)}）" if segment.path
                     else f"生成黑场（任务 {index + 1}/{len(segments)}）")
            command = base.copy()
            cuda_filter = (_cuda_segment_filter(media[segment.path], settings, caps,
                                                width, height, depth, rate_text,
                                                segment.frames, segment.duration + prefix_frames / project.fps,
                                                prefix_frames)
                           if segment.path and not isinstance(segment, TransitionSegment)
                           and not has_pixel_effects(project, segment.camera, segment.start) else None)
            if segment.path is None:
                command += ["-f", "lavfi", "-i",
                            f"color=c=black:s={width}x{height}:r={rate_text}"]
            else:
                command += ["-ss", f"{seek_start:.9f}"]
                if settings.decode_mode == "cuda" and settings.codec.endswith("_nvenc"):
                    command += ["-hwaccel", "cuda", "-hwaccel_device", str(settings.gpu_index)]
                    if cuda_filter is not None:
                        command += ["-hwaccel_output_format", "cuda"]
                command += ["-i", segment.path]
            trim = (f"trim=start_frame={prefix_frames}:end_frame={prefix_frames + segment.frames}"
                    if prefix_frames else f"trim=end_frame={segment.frames}")
            filters = cuda_filter or (
                f"setpts=PTS-STARTPTS,fps=fps={rate_text}:start_time=0,"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
                f"format={pixel_format},tpad=stop_mode=clone:stop_duration={segment.duration + prefix_frames / project.fps + 1:.9f},"
                f"{trim},settb=AVTB,setpts=N/({rate_text}*TB)"
            )
            if cuda_filter is None:
                effect_chain = normal_effect_filter(project, segment.camera, segment.start, ass_path)
                if effect_chain:
                    filters += ',' + effect_chain + f',format={pixel_format}'
                    command += ['-filter_threads', '1']
            command += ["-map", "0:v:0", "-an", "-sn", "-dn", "-vf", filters]
            command += encode_args
            # Requesting a software -pix_fmt here would force CUDA surfaces
            # back across PCIe. NVENC reads their native NV12/P010 layout.
            if cuda_filter is None:
                command += ["-pix_fmt", pixel_format]
            command += ["-r", rate_text,
                        "-frames:v", str(segment.frames), "-map_metadata", "-1",
                        "-video_track_timescale", str(rate.numerator), str(chunk)]
            if isinstance(segment, TransitionSegment):
                command = build_transition_command(segment, project, base, settings, width, height,
                    pixel_format, rate_text, encode_args, chunk, ass_path, source_frame_rates=source_rates)
            if stacked:
                command = build_stack_command(segment, project, base, width, height,
                    pixel_format, rate_text, encode_args, chunk, ass_path,
                    source_frame_rates=source_rates, media_by_path=media)
                # Dozens of layers plus curves/LUT paths can exceed Windows'
                # command-line length limit. Keep the full graph in this
                # export's temporary directory instead of on the command line.
                graph_index = command.index('-filter_complex')
                graph_path = temporary / f'stack_{index:06d}.filtergraph'
                graph_path.write_text(command[graph_index+1], encoding='utf-8')
                command[graph_index:graph_index+2] = ['-/filter_complex', str(graph_path)]
                backend = {'nvidia':'NVENC', 'amd':'AMF', 'intel':'QSV'}.get(encoder_vendor(settings.codec), '软件')
                phase = f'合成 {len(segment.layers)} 层画面（任务 {index + 1}/{len(segments)}） · {backend} 编码'
            fallback_command = command
            gpu_commands = []
            if gpu_effect_ffmpeg and segment.path and (isinstance(segment, TransitionSegment) or cuda_filter is None):
                blocked=disabled_gpu_reason(segment.path)
                if blocked:phase+=' · CPU 特效（已跳过失败的 GPU 路径）'
                # Vulkan HEVC decoding failed with VK_ERROR_DEVICE_LOST on
                # actual camera footage. Honor the selected CUDA decoder;
                # Vulkan is used only by the grading filter after a 4K bridge.
                for decode_mode in (() if blocked else ('cuda',)):
                    try:
                        gpu_base = [gpu_effect_ffmpeg, *base[1:]]
                        if isinstance(segment, TransitionSegment):
                            candidate = build_gpu_transition_command(segment, project, gpu_base, settings,
                                width, height, pixel_format, rate_text, encode_args, chunk, ass_path,
                                source_frame_rates=source_rates, media_by_path=media, decode_mode=decode_mode)
                        else:
                            plan = build_gpu_video_plan((project.camera_grades.get(segment.camera),
                                grade_at(project, segment.start), project.global_grade),
                                ffmpeg_path=gpu_effect_ffmpeg, width=width, height=height,
                                pixel_format=pixel_format, rate_text=rate_text, frames=segment.frames,
                                duration=segment.duration + prefix_frames/project.fps,
                                media=media[segment.path], prefix_frames=prefix_frames, decode_mode=decode_mode,
                                gpu_index=settings.gpu_index)
                            chain = plan.filter_chain
                            if ass_path is not None:
                                chain += (f',setpts=PTS+{segment.start:.12g}/TB,' + subtitle_filter(ass_path)
                                          + ',setpts=PTS-STARTPTS')
                            candidate = (gpu_base + list(plan.global_options) + ['-filter_threads','1']
                                + list(plan.input_options) + ['-ss',f'{seek_start:.9f}','-i',segment.path,
                                '-map','0:v:0','-an','-sn','-dn','-vf',chain, *encode_args,
                                '-pix_fmt',pixel_format,'-r',rate_text,'-frames:v',str(segment.frames),
                                *plan.color_options,'-map_metadata','-1','-video_track_timescale',
                                str(rate.numerator),str(chunk)])
                        gpu_commands.append((candidate,decode_mode))
                    except (GpuExportUnsupported,OSError,subprocess.SubprocessError) as error:
                        phase += ' · CPU 特效：' + str(error)
                        break
            if gpu_commands:
                command = gpu_commands[0][0]
                phase += ' · CUDA 硬解 + GPU 缩放/调色'
                if ass_path is not None:phase += ' + 字幕'
            elif cuda_filter is not None:
                phase += ' · CUDA 硬解/缩放 → NVENC'
            tracker = RollingProgressRate(clock=lambda: time.monotonic())
            with lock:
                active[index] = (0.0, 0.0, time.monotonic())
                publish(phase)

            def report(snapshot: dict) -> None:
                # Each segment owns its counters. A new FFmpeg process starts
                # at zero; it must never reset the aggregate or inflate speed.
                # Cancellation is handled by the runner, never by throwing
                # out of its progress callback while its child is still alive.
                try:
                    frames = max(0, min(segment.frames, int(snapshot.get("frame", 0))))
                except (ValueError, TypeError, OverflowError):
                    frames = 0
                if not frames:
                    frames = min(segment.frames, int(_progress_seconds(snapshot) * project.fps))
                # Keep the original packet for rate calculation: a final
                # success event below may fill missing counters for progress,
                # but is not a throughput sample.
                with lock:
                    recent_fps, recent_speed = tracker.update(snapshot, project.fps)
                    active[index] = (recent_fps, recent_speed, time.monotonic())
                    processed_frames[index] = max(processed_frames[index], frames)
                    publish(phase)

            try:
                using_gpu=bool(gpu_commands)
                # Another worker may have discovered a device failure while
                # this job was planning. Do not launch a known broken route.
                if using_gpu and disabled_gpu_reason(segment.path):
                    command=fallback_command;using_gpu=False
                    phase=f'任务 {index+1}/{len(segments)} · CPU 特效（已跳过失败的 GPU 路径）'
                attempt_started=time.monotonic()
                trace('attempt_start',task=index+1,backend='gpu_effects' if using_gpu else 'original',
                      source=segment.path,frames=segment.frames,prefix_frames=prefix_frames,command=command)
                code, details = run_process(command, report)
                if code and not using_gpu:aborted.set()
                trace('attempt_finish',task=index+1,backend='gpu_effects' if using_gpu else 'original',
                      returncode=code,seconds=time.monotonic()-attempt_started,details=str(details) if code else '')
                if code and using_gpu:
                    cancelled()
                    disable_failed_gpu(segment.path,details)
                    command=fallback_command
                    phase=f'任务 {index+1}/{len(segments)} · CPU 特效回退（GPU 失败，详见导出日志）'
                    with lock:
                        processed_frames[index] = 0
                        active[index] = (0.,0.,time.monotonic())
                        tracker = RollingProgressRate(clock=lambda: time.monotonic())
                        publish(phase)
                    attempt_started=time.monotonic()
                    trace('attempt_start',task=index+1,backend='cpu_effects_fallback',source=segment.path,
                          frames=segment.frames,prefix_frames=prefix_frames,command=command)
                    code, details = run_process(command, report)
                    if code:aborted.set()
                    trace('attempt_finish',task=index+1,backend='cpu_effects_fallback',returncode=code,
                          seconds=time.monotonic()-attempt_started,details=str(details) if code else '')
                check_output(code, details, chunk, phase)
                with lock:
                    processed_frames[index] = segment.frames
                    completed_segments[index] = True
            except BaseException as error:
                aborted.set()
                trace('task_error',task=index+1,error=str(error),cancelled=isinstance(error,ExportCancelled))
                raise
            finally:
                with lock:
                    active.pop(index, None)
                    publish(phase)

        # Only submit the available slots. On failure/cancel no additional
        # chunks start, and the executor waits for every launched runner before
        # TemporaryDirectory can remove files still opened by FFmpeg.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="multicam-export") as pool:
            pending = set()
            next_index = 0
            for _ in range(workers):
                pending.add(pool.submit(encode_segment, next_index))
                next_index += 1
            try:
                while pending:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        future.result()
                    cancelled()
                    while next_index < len(segments) and len(pending) < workers and not aborted.is_set():
                        pending.add(pool.submit(encode_segment, next_index))
                        next_index += 1
            except BaseException:
                aborted.set()
                for future in pending:
                    future.cancel()
                raise
        cancelled()
        # The manifest uses controlled ASCII basenames inside the temp folder;
        # no external filename can inject ffconcat directives.
        manifest = temporary / "programme.ffconcat"
        manifest.write_text("ffconcat version 1.0\n" + "".join(
            f"file '{chunk.name}'\n" for chunk in encoded), encoding="utf-8")
        final = temporary / ("programme" + destination.suffix.lower())
        command = base + ["-f", "concat", "-safe", "0", "-i", str(manifest)]
        if settings.audio == "none":
            command += ["-map", "0:v:0", "-c:v", "copy", "-an"]
        else:
            if audio_path:
                # Seeking even to zero can discard AAC priming metadata in
                # camera MP4 files. Open the untouched stream at its start.
                if audio_start > 0:
                    command += ["-ss", f"{audio_start:.9f}"]
                command += ["-i", audio_path]
            else:
                command += ["-f", "lavfi", "-i", f"anullsrc=r={audio_sample_rate}:cl=stereo"]
            command += ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy"]
            if settings.audio != "copy":
                filters = "asetpts=PTS-STARTPTS"
                if audio_path and audio_start < 0:
                    # A camera recording starting after programme zero needs
                    # leading silence on the continuous master clock.
                    delay = round(-audio_start * audio_sample_rate)
                    filters += f",aresample={audio_sample_rate},adelay={delay}S:all=1"
                command += ["-af", filters + f",apad,atrim=duration={duration:.9f}"]
            command += audio_args
        if destination.suffix.lower() in {".mp4", ".mov", ".m4v"}:
            command += ["-movflags", "+faststart"]
        command += ["-t", f"{duration:.9f}", "-map_metadata", "-1",
                    "-max_muxing_queue_size", "4096", str(final)]
        phase = "封装节目并写入连续主音频"
        with lock:
            mux_started = time.monotonic()
            publish(phase, mux=True)

        def report_mux(snapshot: dict) -> None:
            nonlocal mux_seconds
            with lock:
                mux_seconds = max(mux_seconds, min(duration, _progress_seconds(snapshot)))
                # FFmpeg's mux frame counter counts copied packets. It is not
                # an encoding FPS measurement; use timestamp deltas only.
                _, speed = mux_rate.update({"out_time_us": str(round(mux_seconds * 1_000_000))})
                publish(phase, mux=True, mux_speed=speed)

        cancelled()
        code, details = run_process(command, report_mux)
        check_output(code, details, final, phase)
        cancelled()
        def report_delivery(copied, total):
            with lock:
                publish(f'写入成片目标目录 {copied/max(1,total)*100:.1f}%', mux=True)
        publish_completed(final, destination, cancel_event=cancel_event, progress=report_delivery)
    with lock:
        mux_seconds = duration
        publish("多机位导出完成", mux=True, finished=True)
    trace('export_finish',frames=total_frames,seconds=time.monotonic()-started,output=str(destination))
    return str(destination)
