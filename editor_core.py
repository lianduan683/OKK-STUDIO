from __future__ import annotations

import math
import cmath
import os
import re
import subprocess
import sys
from array import array
from dataclasses import dataclass
from fractions import Fraction
from itertools import islice
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from effects_core import EffectSettings, build_effect_filter_graph, overlay_active, validate_effects

from exporter_core import (
    CREATE_NO_WINDOW,
    Capabilities,
    ExportSettings,
    MediaInfo,
    _resolution_height,
    _target_depth,
    build_encode_command,
    build_video_args,
    build_video_filter,
)


@dataclass(slots=True)
class AlignmentResult:
    offset_seconds: float
    confidence: float
    envelope_rate: int
    engine: str = "NumPy 高速"


@dataclass(frozen=True, slots=True)
class JoinTimelineClip:
    """A video clip placed on the shared master-audio timeline."""

    index: int
    path: str
    start: float
    duration: float
    aligned: bool
    confidence: float = 0.0
    engine: str = ""

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass(frozen=True, slots=True)
class JoinTimelineGap:
    """A stop-camera gap between two adjacent input clips."""

    after_index: int
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass(frozen=True, slots=True)
class JoinTimelineModel:
    clips: tuple[JoinTimelineClip, ...]
    gaps: tuple[JoinTimelineGap, ...]
    start: float
    end: float
    audio_duration: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def timeline_frame_zoom_multiplier(
    fit_pixels_per_second: float,
    fps: float,
    *,
    pixels_per_frame: float = 12.0,
) -> float:
    """Return the zoom needed to make every source frame individually visible."""
    fit_pixels_per_second = float(fit_pixels_per_second)
    fps = float(fps)
    pixels_per_frame = float(pixels_per_frame)
    if fit_pixels_per_second <= 0 or not math.isfinite(fit_pixels_per_second):
        raise ValueError("适配时间线像素比例必须大于零")
    if fps <= 0 or not math.isfinite(fps):
        raise ValueError("素材帧率必须大于零")
    if pixels_per_frame <= 0 or not math.isfinite(pixels_per_frame):
        raise ValueError("逐帧显示宽度必须大于零")
    return max(1.0, fps * pixels_per_frame / fit_pixels_per_second)


def fit_aspect_size(
    container_width: float,
    container_height: float,
    source_width: float,
    source_height: float,
    *,
    scale: float = 1.0,
) -> tuple[int, int]:
    """Fit a source rectangle inside a container without changing its aspect ratio."""
    values = (container_width, container_height, source_width, source_height, scale)
    if any(not math.isfinite(float(value)) or float(value) <= 0 for value in values):
        raise ValueError("监看尺寸和缩放比例必须大于零")
    container_width = float(container_width)
    container_height = float(container_height)
    aspect = float(source_width) / float(source_height)
    fitted_width = min(container_width, container_height * aspect)
    fitted_height = fitted_width / aspect
    scale = min(1.0, float(scale))
    return (
        max(1, int(round(fitted_width * scale))),
        max(1, int(round(fitted_height * scale))),
    )


def build_join_timeline_model(
    paths: Sequence[str],
    durations: Sequence[float],
    alignments: Mapping[str, AlignmentResult],
    *,
    audio_duration: float = 0.0,
    trim_before_audio: bool = False,
    trim_after_audio: bool = False,
) -> JoinTimelineModel:
    """Place clips for the editor timeline without changing export semantics.

    Aligned clips use ``-offset_seconds`` as their master-audio start.  Clips
    that have not been aligned yet are laid out sequentially so they remain
    selectable instead of disappearing from the visual timeline.
    """
    if len(paths) != len(durations):
        raise ValueError("视频路径与时长数量不一致")
    audio_duration = max(0.0, float(audio_duration or 0.0))

    clips: list[JoinTimelineClip] = []
    sequential_start = 0.0
    for index, (path, raw_duration) in enumerate(zip(paths, durations)):
        duration = float(raw_duration or 0.0)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError(f"第 {index + 1} 段视频时长无效")
        result = alignments.get(path)
        start = -float(result.offset_seconds) if result is not None else sequential_start
        if not math.isfinite(start):
            raise ValueError(f"第 {index + 1} 段视频对齐时间无效")
        if result is not None and trim_before_audio and start < 0:
            duration = max(0.0, duration + start)
            start = 0.0
        if result is not None and trim_after_audio and audio_duration > 0 and start + duration > audio_duration:
            duration = max(0.0, audio_duration - start)
        clip = JoinTimelineClip(
            index=index,
            path=str(path),
            start=start,
            duration=duration,
            aligned=result is not None,
            confidence=float(result.confidence) if result is not None else 0.0,
            engine=str(result.engine) if result is not None else "",
        )
        clips.append(clip)
        sequential_start = max(sequential_start, clip.end)

    gaps: list[JoinTimelineGap] = []
    for previous, current in zip(clips, clips[1:]):
        gap = current.start - previous.end
        if previous.aligned and current.aligned and gap > 0.0005:
            gaps.append(JoinTimelineGap(previous.index, previous.end, gap))

    starts = [0.0, *(clip.start for clip in clips)]
    ends = [audio_duration, *(clip.end for clip in clips)]
    timeline_start = min(starts)
    timeline_end = max(ends)
    if timeline_end <= timeline_start:
        timeline_end = timeline_start + 1.0
    return JoinTimelineModel(
        clips=tuple(clips), gaps=tuple(gaps), start=timeline_start,
        end=timeline_end, audio_duration=audio_duration,
    )


def aligned_clip_timeline(
    results: Sequence[AlignmentResult],
    durations: Sequence[float],
    *,
    trim_before_audio: bool = False,
    keep_before_audio: bool = False,
    tolerance: float = 0.25,
) -> tuple[list[float], list[tuple[float, float]]]:
    """Return per-clip head trims and matching master-audio intervals."""
    if len(results) != len(durations) or not results:
        raise ValueError("对齐结果与视频片段数量不一致")
    video_trims: list[float] = []
    intervals: list[tuple[float, float]] = []
    for index, (result, duration) in enumerate(zip(results, durations), start=1):
        duration = float(duration)
        if duration <= 0:
            raise ValueError(f"第 {index} 段视频时长无效")
        leading_video = max(0.0, float(result.offset_seconds))
        if trim_before_audio and keep_before_audio:
            raise ValueError("主音频开始前的处理模式不能同时选择裁剪和保留。")
        if leading_video > tolerance and not trim_before_audio and not keep_before_audio:
            raise ValueError(
                f"第 {index} 段早于主音频开始 {leading_video:.3f} 秒；"
                "请开启“裁到主音频开始”，或让主录音更早开始。"
            )
        video_trim = leading_video if trim_before_audio else 0.0
        usable_duration = duration - video_trim
        if usable_duration <= tolerance:
            raise ValueError(
                f"第 {index} 段需要裁掉 {video_trim:.3f} 秒，但视频总长只有 {duration:.3f} 秒。"
            )
        video_trims.append(video_trim)
        master_start = -float(result.offset_seconds)
        if not keep_before_audio:
            master_start = max(0.0, master_start)
        intervals.append((master_start, usable_duration))
    return video_trims, intervals


def aligned_audio_intervals(
    results: Sequence[AlignmentResult],
    durations: Sequence[float],
    *,
    trim_before_audio: bool = False,
    keep_before_audio: bool = False,
    tolerance: float = 0.25,
) -> list[tuple[float, float]]:
    """Convert per-clip offsets into master-audio slices for gap-safe concatenation."""
    _video_trims, intervals = aligned_clip_timeline(
        results, durations, trim_before_audio=trim_before_audio,
        keep_before_audio=keep_before_audio, tolerance=tolerance,
    )
    return intervals


def format_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remaining = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{remaining:06.3f}"


def format_timeline_ruler_time(seconds: float, tick_step: float) -> str:
    """Format ruler labels compactly while retaining useful sub-second detail."""
    value = float(seconds or 0.0)
    step = abs(float(tick_step or 0.0))
    precision = 0 if step >= 1.0 else (1 if step >= 0.1 else (2 if step >= 0.01 else 3))
    factor = 10 ** precision
    total_units = round(abs(value) * factor)
    hours, remainder = divmod(total_units, 3600 * factor)
    minutes, second_units = divmod(remainder, 60 * factor)
    if precision:
        seconds_text = f"{second_units / factor:0{3 + precision}.{precision}f}"
    else:
        seconds_text = f"{second_units:02d}"
    sign = "-" if value < 0 else ""
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds_text}"


def parse_timecode(value: str) -> float:
    """Parse seconds, MM:SS.mmm, or HH:MM:SS.mmm into seconds."""
    text = str(value).strip().replace(",", ".")
    if not text:
        raise ValueError("时间码不能为空")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError("时间码格式应为 HH:MM:SS.mmm")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError("时间码只能包含数字、冒号和小数点") from exc
    if any(number < 0 for number in numbers):
        raise ValueError("时间码不能为负数")
    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2:
        minutes, seconds = numbers
        if seconds >= 60:
            raise ValueError("秒数必须小于 60")
        return minutes * 60 + seconds
    hours, minutes, seconds = numbers
    if minutes >= 60 or seconds >= 60:
        raise ValueError("分和秒必须小于 60")
    return hours * 3600 + minutes * 60 + seconds


def parse_signed_timecode(value: str) -> float:
    """Parse a signed timeline position; negative means before master-audio zero."""
    text = str(value).strip()
    sign = -1.0 if text.startswith("-") else 1.0
    if text[:1] in {"-", "+"}:
        text = text[1:].strip()
    if not text:
        raise ValueError("时间码不能为空")
    return sign * parse_timecode(text)


def format_signed_time(seconds: float) -> str:
    value = float(seconds or 0.0)
    return ("-" if value < 0 else "") + format_time(abs(value))


def probe_media_duration(ffprobe: str, path: str) -> float:
    """Read container duration for audio-only or video files."""
    process = subprocess.run(
        [
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if process.returncode:
        raise RuntimeError(f"无法读取主音频时长：{process.stderr.strip()[-1200:]}")
    try:
        duration = float(process.stdout.strip().splitlines()[0])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("ffprobe 没有返回有效的主音频时长。") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("主音频时长无效。")
    return duration


def build_trim_copy_command(ffmpeg: str, source: str, output: str, start: float, end: float) -> list[str]:
    if start < 0 or end <= start:
        raise ValueError("裁切终点必须晚于起点")
    command = [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "warning",
        "-stats_period", "0.25", "-progress", "pipe:1", "-nostats",
        "-ss", f"{start:.6f}", "-i", source,
        "-t", f"{end - start:.6f}",
        "-map", "0:v:0", "-map", "0:a?",
        "-map_metadata", "0", "-map_chapters", "0",
        "-c", "copy", "-avoid_negative_ts", "make_zero",
    ]
    if Path(output).suffix.lower() in {".mp4", ".mov", ".m4v"}:
        command += ["-movflags", "+faststart"]
    command.append(output)
    return command


def _ffconcat_quote(path: str) -> str:
    if "\n" in path or "\r" in path:
        raise ValueError("文件名不能包含换行符")
    return str(Path(path).resolve()).replace("\\", "/").replace("'", "'\\''")


def write_concat_manifest(paths: Iterable[str], manifest: str | Path) -> None:
    lines = ["ffconcat version 1.0"]
    for path in paths:
        lines.append(f"file '{_ffconcat_quote(path)}'")
    Path(manifest).write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_concat_copy_command(ffmpeg: str, manifest: str, output: str) -> list[str]:
    command = [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "warning",
        "-stats_period", "0.25", "-progress", "pipe:1", "-nostats",
        "-f", "concat", "-safe", "0", "-i", manifest,
        "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0",
        "-c", "copy", "-fflags", "+genpts", "-avoid_negative_ts", "make_zero",
    ]
    if Path(output).suffix.lower() in {".mp4", ".mov", ".m4v"}:
        command += ["-movflags", "+faststart"]
    command.append(output)
    return command


def concat_compatibility_issues(
    media: Sequence[MediaInfo], *, include_audio: bool = True,
) -> list[str]:
    if len(media) < 2:
        return ["至少需要两个视频"]
    reference = media[0]
    issues: list[str] = []
    for index, item in enumerate(media[1:], start=2):
        differences: list[str] = []
        if item.video_codec != reference.video_codec:
            differences.append("视频编码")
        if (item.width, item.height) != (reference.width, reference.height):
            differences.append("分辨率")
        if abs(item.fps - reference.fps) > 0.002:
            differences.append("帧率")
        if item.pixel_format != reference.pixel_format or item.bit_depth != reference.bit_depth:
            differences.append("位深/像素格式")
        if include_audio and (item.has_audio != reference.has_audio or item.audio_codec != reference.audio_codec):
            differences.append("音频轨")
        if (
            item.color_primaries != reference.color_primaries
            or item.color_transfer != reference.color_transfer
            or item.color_space != reference.color_space
        ):
            differences.append("色彩空间")
        if differences:
            issues.append(f"第 {index} 个视频与第 1 个不一致：{'、'.join(differences)}")
    return issues


def normalize_video_bitrate(value: str) -> str:
    """Normalize UI bitrate text; a bare number is treated as kbps."""
    text = str(value or "").strip().replace(" ", "")
    if not text:
        return ""
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return f"{text}k"
    if not re.fullmatch(r"\d+(?:\.\d+)?[kKmMgG]", text):
        raise ValueError("视频码率格式错误，请填写 40000、40000k 或 40M。")
    return text


def normalize_audio_bitrate(value: str) -> str:
    """Normalize an editable AAC bitrate; a bare number is treated as kbps."""
    text = str(value or "").strip().replace(" ", "").lower()
    if not text:
        raise ValueError("AAC 码率不能为空，请填写 48k、96k、320k 等数值。")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        text = f"{text}k"
    if not re.fullmatch(r"\d+(?:\.\d+)?[km]", text):
        raise ValueError("AAC 码率格式错误，请填写 48k、96k、320k 或手动数值。")
    return text


def normalize_audio_sample_rate(value: str) -> int | None:
    """Parse an editable sample rate such as 48000, 48k, or 96 kHz."""
    text = str(value or "").strip().lower().replace(" ", "")
    if not text or text in {"保留", "保留原采样率", "original", "auto", "自动"}:
        return None
    text = text.replace("khz", "k").replace("hz", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(k?)", text)
    if not match:
        raise ValueError("音频采样率格式错误，请填写 48000、48k 或 96k。")
    number = float(match.group(1))
    if match.group(2) == "k" or number < 1000:
        number *= 1000
    rate = int(round(number))
    if rate < 8000 or rate > 384000:
        raise ValueError("音频采样率应在 8 kHz 到 384 kHz 之间。")
    return rate


def build_audio_encode_args(
    codec: str,
    bitrate: str,
    sample_rate: int | None,
    output: str,
) -> list[str]:
    """Build audio output arguments while enforcing container compatibility."""
    codec = str(codec or "aac").lower()
    suffix = Path(output).suffix.lower()
    pcm_codecs = {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le"}
    if codec not in {"aac", *pcm_codecs}:
        raise ValueError(f"不支持的音频编码：{codec}")
    if codec in pcm_codecs and suffix not in {".mov", ".mkv"}:
        raise ValueError("PCM 16/24/32-bit 无损音频需要输出为 MOV 或 MKV；MP4 请使用 AAC。")
    if codec == "aac" and sample_rate is not None and sample_rate > 96000:
        raise ValueError("AAC 最高请选择 96 kHz；更高采样率请改用 PCM 16/24/32-bit 并输出 MOV/MKV。")
    args = ["-c:a", codec]
    if codec == "aac":
        args += ["-b:a", normalize_audio_bitrate(bitrate)]
    if sample_rate is not None:
        args += ["-ar", str(int(sample_rate))]
    return args


def _master_audio_filter(
    intervals: Sequence[tuple[float, float]], *, input_index: int = 1,
    output_label: str = "aout",
) -> str:
    if not intervals:
        raise ValueError("至少需要一个主音频区间")
    normalized: list[tuple[float, float]] = []
    for start, duration in intervals:
        start = float(start)
        duration = float(duration)
        if duration <= 0:
            raise ValueError("主音频区间必须具有正时长")
        if start < 0 and start + duration <= 0:
            raise ValueError("视频片段在主音频开始前已经结束，无法从主音频生成对应声音。")
        normalized.append((start, duration))

    def segment_filter(index: int, start: float, duration: float, output_label: str) -> str:
        source_label = f"[master{index}]" if len(normalized) > 1 else f"[{input_index}:a:0]"
        if start >= 0:
            return (
                f"{source_label}atrim=start={start:.6f}:duration={duration:.6f},"
                f"asetpts=PTS-STARTPTS{output_label}"
            )
        silence = -start
        source_duration = duration - silence
        delay_ms = int(round(silence * 1000.0))
        return (
            f"{source_label}atrim=start=0.000000:duration={source_duration:.6f},"
            f"asetpts=PTS-STARTPTS,adelay={delay_ms}:all=1,"
            f"atrim=duration={duration:.6f}{output_label}"
        )

    if len(normalized) == 1:
        start, duration = normalized[0]
        return segment_filter(0, start, duration, "") + f",apad[{output_label}]"
    split_outputs = "".join(f"[master{index}]" for index in range(len(normalized)))
    parts = [f"[{input_index}:a:0]asplit={len(normalized)}{split_outputs}"]
    for index, (start, duration) in enumerate(normalized):
        parts.append(segment_filter(index, start, duration, f"[a{index}]"))
    concat_inputs = "".join(f"[a{index}]" for index in range(len(normalized)))
    parts.append(f"{concat_inputs}concat=n={len(normalized)}:v=0:a=1,apad[{output_label}]")
    return ";".join(parts)


def build_aligned_concat_copy_command(
    ffmpeg: str,
    manifest: str,
    external_audio: str,
    intervals: Sequence[tuple[float, float]],
    output: str,
    audio_bitrate: str = "320k",
    audio_codec: str = "aac",
    audio_sample_rate: int | None = None,
) -> list[str]:
    """Losslessly concatenate camera video while rebuilding audio from master intervals."""
    command = [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "warning",
        "-stats_period", "0.25", "-progress", "pipe:1", "-nostats",
        "-f", "concat", "-safe", "0", "-i", manifest,
        "-i", external_audio,
        "-filter_complex", _master_audio_filter(intervals),
        "-map", "0:v:0", "-map", "[aout]", "-map_metadata", "0",
        "-c:v", "copy",
    ]
    command += build_audio_encode_args(audio_codec, audio_bitrate, audio_sample_rate, output)
    command += ["-fflags", "+genpts", "-avoid_negative_ts", "make_zero", "-shortest"]
    if Path(output).suffix.lower() in {".mp4", ".mov", ".m4v"}:
        command += ["-movflags", "+faststart"]
    command.append(output)
    return command


def build_concat_encode_command(
    ffmpeg: str,
    manifest: str,
    output: str,
    settings: ExportSettings,
    reference_media: MediaInfo,
    caps: Capabilities,
    *,
    external_audio: str | None = None,
    intervals: Sequence[tuple[float, float]] = (),
    audio_bitrate: str = "320k",
    audio_codec: str = "aac",
    audio_sample_rate: int | None = None,
) -> list[str]:
    """Concatenate and NVENC-encode, optionally replacing audio from aligned master slices."""
    command = [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "warning",
        "-stats_period", "0.25", "-progress", "pipe:1", "-nostats",
    ]
    if settings.decode_mode == "cuda":
        command += [
            "-hwaccel", "cuda", "-hwaccel_device", str(settings.gpu_index),
            "-hwaccel_output_format", "cuda",
        ]
    command += ["-f", "concat", "-safe", "0", "-i", manifest]
    if external_audio:
        command += ["-i", external_audio, "-filter_complex", _master_audio_filter(intervals)]
    command += ["-map", "0:v:0"]
    if external_audio:
        command += ["-map", "[aout]"]
    elif settings.audio != "none":
        command += ["-map", "0:a?"]
    command += build_video_filter(settings, reference_media, caps)
    command += build_video_args(settings, reference_media, caps)
    if external_audio:
        command += build_audio_encode_args(audio_codec, audio_bitrate, audio_sample_rate, output)
    elif settings.audio == "copy":
        command += ["-c:a", "copy"]
    elif settings.audio == "none":
        command += ["-an"]
    else:
        command += ["-c:a", "aac", "-b:a", settings.audio_bitrate]
    if Path(output).suffix.lower() in {".mp4", ".mov", ".m4v"}:
        command += ["-movflags", "+faststart"]
    command += ["-map_metadata", "0", "-max_muxing_queue_size", "4096", "-shortest", output]
    return command


def gap_preserving_timeline(
    results: Sequence[AlignmentResult],
    durations: Sequence[float],
    *,
    trim_before_audio: bool = False,
    keep_before_audio: bool = False,
    tolerance: float = 0.25,
) -> tuple[list[float], list[float], float, float]:
    """Return master starts, gaps after clips, audio start, and total timeline duration."""
    _video_trims, intervals = aligned_clip_timeline(
        results, durations, trim_before_audio=trim_before_audio,
        keep_before_audio=keep_before_audio, tolerance=tolerance,
    )
    starts = [start for start, _duration in intervals]
    usable_durations = [duration for _start, duration in intervals]
    if any(starts[index] + tolerance < starts[index - 1] for index in range(1, len(starts))):
        raise ValueError("视频顺序与主音频时间顺序不一致，请调整片段列表顺序后重新对齐。")
    gaps: list[float] = []
    for index in range(len(starts) - 1):
        gap = starts[index + 1] - (starts[index] + usable_durations[index])
        if gap < -tolerance:
            raise ValueError(
                f"第 {index + 1}、{index + 2} 段在主音频上重叠 {-gap:.3f} 秒；"
                "请先裁掉重复画面，或改用删除空窗模式。"
            )
        gaps.append(max(0.0, gap))
    gaps.append(0.0)
    audio_start = starts[0]
    total_duration = (starts[-1] - audio_start) + usable_durations[-1]
    return starts, gaps, audio_start, total_duration


def resolve_join_output_range(
    results: Sequence[AlignmentResult],
    durations: Sequence[float],
    *,
    trim_before_audio: bool = False,
    keep_before_audio: bool = False,
    preserve_gaps: bool = True,
    master_audio_duration: float | None = None,
    trim_after_audio: bool = False,
    custom_start: float | None = None,
    custom_end: float | None = None,
    tolerance: float = 0.25,
) -> tuple[float, float, float]:
    """Resolve final-file start/end positions, measured from output 00:00."""
    _video_trims, intervals = aligned_clip_timeline(
        results, durations, trim_before_audio=trim_before_audio,
        keep_before_audio=keep_before_audio, tolerance=tolerance,
    )
    if preserve_gaps:
        _starts, _gaps, audio_start, total_duration = gap_preserving_timeline(
            results, durations, trim_before_audio=trim_before_audio,
            keep_before_audio=keep_before_audio, tolerance=tolerance,
        )
    else:
        audio_start = intervals[0][0]
        total_duration = sum(duration for _start, duration in intervals)
    automatic_end = total_duration
    if trim_after_audio:
        if master_audio_duration is None or master_audio_duration <= 0:
            raise ValueError("裁到主音频结束需要有效的主音频时长。")
        if preserve_gaps:
            automatic_end = min(total_duration, max(0.0, master_audio_duration - audio_start))
        else:
            # Deleted camera-off gaps do not exist in the compact output. Map
            # the master-audio ending onto the accumulated visible clip time.
            compact_end = 0.0
            for start, duration in intervals:
                available = max(0.0, min(duration, master_audio_duration - start))
                compact_end += available
                if available + tolerance < duration:
                    break
            automatic_end = min(total_duration, compact_end)

    start = 0.0 if custom_start is None else float(custom_start)
    end = automatic_end if custom_end is None else float(custom_end)
    if start < 0:
        raise ValueError("成片开始时间不能为负数。")
    if end <= start + 0.0005:
        raise ValueError("成片结束时间必须晚于开始时间。")
    if start >= total_duration - 0.0005:
        raise ValueError(f"成片开始时间超过可用画面时长 {format_time(total_duration)}。")
    if end > total_duration + tolerance:
        raise ValueError(f"成片结束时间超过可用画面时长 {format_time(total_duration)}。")
    end = min(end, total_duration)
    return start, end, total_duration


@dataclass(frozen=True, slots=True)
class JoinVideoSpan:
    source_index: int | None  # None denotes a camera-off black interval.
    source_start: float
    master_start: float
    duration: float
    frames: int


@dataclass(frozen=True, slots=True)
class JoinExportPlan:
    """Frame-exact video spans and matching master-audio slices."""

    spans: tuple[JoinVideoSpan, ...]
    audio_intervals: tuple[tuple[float, float], ...]
    duration: float
    fps: float


def plan_join_video_spans(
    video_trims: Sequence[float],
    intervals: Sequence[tuple[float, float]],
    output_start: float,
    output_end: float,
    fps: float,
    *,
    preserve_gaps: bool,
) -> list[JoinVideoSpan]:
    """Intersect the requested output with clips/gaps before opening decoders.

    Round cumulative boundaries, not individual durations, so many fractional
    camera-off gaps cannot accumulate an audio/video timing error.
    """
    spans: list[JoinVideoSpan] = []

    def append(index: int | None, start: float, end: float, source_start: float, master_start: float):
        left, right = max(start, output_start), min(end, output_end)
        if right <= left:
            return
        first_frame = math.floor((left - output_start) * fps + 0.5)
        last_frame = math.floor((right - output_start) * fps + 0.5)
        frames = last_frame - first_frame
        if frames > 0:
            offset = left - start
            spans.append(JoinVideoSpan(index, source_start + offset, master_start + offset, frames / fps, frames))

    cursor = 0.0
    origin = intervals[0][0]
    for index, ((master_start, duration), head_trim) in enumerate(zip(intervals, video_trims)):
        start = master_start - origin if preserve_gaps else cursor
        end = start + duration
        # Alignment tolerates small overlaps. Remove their duplicated tail
        # instead of allowing the continuous master track to drift.
        next_start = intervals[index + 1][0] - origin if preserve_gaps and index + 1 < len(intervals) else end
        append(index, start, min(end, next_start), head_trim, master_start)
        if preserve_gaps and next_start > end:
            append(None, end, next_start, 0.0, origin + end)
        cursor = end
    if not spans:
        raise ValueError("选定的成片范围不足一帧，请扩大开始/结束范围。")
    return spans


def make_join_export_plan(
    results: Sequence[AlignmentResult],
    media: Sequence[MediaInfo],
    *,
    trim_before_audio: bool = False,
    keep_before_audio: bool = False,
    preserve_gaps: bool = True,
    master_audio_duration: float | None = None,
    trim_after_audio: bool = False,
    custom_start: float | None = None,
    custom_end: float | None = None,
) -> JoinExportPlan:
    """Resolve one timeline once for direct and parallel join exports."""
    if not media:
        raise ValueError("拼接至少需要一个有效视频")
    durations = [item.duration for item in media]
    video_trims, intervals = aligned_clip_timeline(
        results, durations,
        trim_before_audio=trim_before_audio,
        keep_before_audio=keep_before_audio,
    )
    output_start, output_end, _ = resolve_join_output_range(
        results, durations,
        trim_before_audio=trim_before_audio,
        keep_before_audio=keep_before_audio,
        preserve_gaps=preserve_gaps,
        master_audio_duration=master_audio_duration,
        trim_after_audio=trim_after_audio,
        custom_start=custom_start,
        custom_end=custom_end,
    )
    fps = float(Fraction(media[0].fps or 30.0).limit_denominator(100000))
    spans = plan_join_video_spans(
        video_trims, intervals, output_start, output_end, fps,
        preserve_gaps=preserve_gaps,
    )
    duration = sum(span.frames for span in spans) / fps
    audio_intervals = (
        [(intervals[0][0] + output_start, duration)]
        if preserve_gaps
        else [(span.master_start, span.duration) for span in spans]
    )
    return JoinExportPlan(tuple(spans), tuple(audio_intervals), duration, fps)


def partition_join_video_spans(
    spans: Sequence[JoinVideoSpan],
    workers: int,
    fps: float,
    *,
    chunks_per_worker: int = 2,
) -> list[list[JoinVideoSpan]]:
    """Split a join timeline into frame-exact, independently encoded jobs.

    A few more jobs than workers keeps both NVDEC engines busy when source
    clips have unequal lengths. A job never opens two real camera sources;
    adjacent black frames may stay with the preceding source.
    """
    workers = max(1, int(workers))
    fps = float(fps)
    if fps <= 0:
        raise ValueError("视频帧率必须大于零。")
    total_frames = sum(max(0, int(span.frames)) for span in spans)
    if total_frames <= 0:
        raise ValueError("选定的成片范围不足一帧。")
    target_frames = max(1, math.ceil(total_frames / max(1, workers * chunks_per_worker)))
    chunks: list[list[JoinVideoSpan]] = []
    current: list[JoinVideoSpan] = []
    current_frames = 0

    def flush() -> None:
        nonlocal current, current_frames
        if current:
            chunks.append(current)
        current = []
        current_frames = 0

    for span in spans:
        remaining = int(span.frames)
        consumed = 0
        while remaining > 0:
            if span.source_index is not None and current:
                real_sources = {item.source_index for item in current if item.source_index is not None}
                if real_sources and span.source_index not in real_sources:
                    flush()
            capacity = target_frames - current_frames
            if capacity <= 0:
                flush()
                capacity = target_frames
            take = min(remaining, capacity)
            offset = consumed / fps
            current.append(JoinVideoSpan(
                span.source_index,
                span.source_start + offset if span.source_index is not None else 0.0,
                span.master_start + offset,
                take / fps,
                take,
            ))
            current_frames += take
            consumed += take
            remaining -= take
            if current_frames >= target_frames:
                flush()
    flush()
    return chunks


def join_uses_cuda_frames(settings: ExportSettings, reference: MediaInfo, caps: Capabilities) -> bool:
    """Use a shared CUDA context throughout the video graph when supported."""
    if settings.decode_mode != "cuda":
        return False
    height = _resolution_height(settings.resolution)
    needs_conversion = (
        (height is not None and height != reference.height)
        or _target_depth(settings, reference) != reference.bit_depth
        or reference.pixel_format not in {"yuv420p", "yuvj420p", "nv12", "yuv420p10le", "p010le"}
    )
    return not needs_conversion or caps.scale_cuda


def _append_seeked_master_audio(
    command: list[str], filters: list[str], external_audio: str,
    intervals: Sequence[tuple[float, float]], first_input: int,
) -> None:
    """Seek each needed master slice; never split/buffer an entire long WAV."""
    merged: list[tuple[float, float]] = []
    for start, duration in intervals:
        if merged and abs(merged[-1][0] + merged[-1][1] - start) < 0.000001:
            previous_start, previous_duration = merged[-1]
            merged[-1] = previous_start, previous_duration + duration
        else:
            merged.append((start, duration))
    labels: list[str] = []
    for index, (start, duration) in enumerate(merged):
        silence = min(duration, max(0.0, -start))
        wanted = max(0.0, duration - silence)
        command += ["-ss", f"{max(0.0, start):.6f}", "-t", f"{max(0.02, wanted):.6f}", "-vn", "-sn", "-dn", "-i", external_audio]
        # An empty trim retains the actual input's channel layout for a wholly
        # silent slice. Pad every slice separately to prevent compact-join drift.
        trim = f"atrim=duration={wanted:.6f}" if wanted > 0 else "atrim=end_sample=0"
        chain = f"[{first_input + index}:a:0]{trim},asetpts=PTS-STARTPTS"
        if silence > 0 and wanted > 0:
            chain += f",adelay={silence * 1000.0:.6f}:all=1"
        label = "aout" if len(merged) == 1 else f"aslice{index}"
        filters.append(chain + f",apad=whole_dur={duration:.6f},atrim=duration={duration:.6f}[{label}]")
        labels.append(f"[{label}]")
    if len(labels) > 1:
        filters.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[aout]")


def _build_join_video_inputs(
    ffmpeg: str,
    paths: Sequence[str],
    spans: Sequence[JoinVideoSpan],
    media: Sequence[MediaInfo],
    settings: ExportSettings,
    caps: Capabilities,
    overlays: Mapping[str, EffectSettings] | None = None,
) -> tuple[list[str], list[str], int, MediaInfo, float]:
    """Create seeked video inputs and one frame-exact concat filter graph."""
    if not spans or not media:
        raise ValueError("没有可导出的视频画面。")
    reference = media[0]
    rate = Fraction(reference.fps or 30.0).limit_denominator(100000)
    fps = float(rate)
    rate_text = f"{rate.numerator}/{rate.denominator}"
    height = _resolution_height(settings.resolution) or reference.height
    width = (
        reference.width if height == reference.height
        else 2 * math.floor(reference.width * height / reference.height / 2 + 0.5)
    )
    if width <= 0 or height <= 0:
        raise ValueError("无法读取有效的视频分辨率。")
    pixel_format = "p010le" if _target_depth(settings, reference) == 10 else "nv12"
    sar = reference.sample_aspect_ratio
    sar = sar.replace(":", "/") if re.fullmatch(r"[1-9]\d*:[1-9]\d*", sar) else "1"
    has_overlays = any(overlay_active((overlays or {}).get(paths[span.source_index]))
                       for span in spans if span.source_index is not None)
    cuda_frames = not has_overlays and join_uses_cuda_frames(settings, reference, caps)
    command = [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "warning",
        "-stats_period", "0.25", "-progress", "pipe:1", "-nostats",
    ]
    if cuda_frames:
        command += [
            "-init_hw_device", f"cuda=okk:{settings.gpu_index}",
            "-filter_hw_device", "okk",
        ]
    filters: list[str] = []
    labels: list[str] = []
    input_index = 0
    for index, span in enumerate(spans):
        label = f"vspan{index}"
        if span.source_index is None:
            chain = (
                f"color=c=black:s={width}x{height}:r={rate_text},"
                f"format={pixel_format},trim=end_frame=1,setsar={sar}"
            )
            if reference.color_range == "pc":
                chain += f",scale=in_range=tv:out_range=pc,format={pixel_format}"
            if cuda_frames:
                chain += ",hwupload"
            chain += (
                f",loop=loop={span.frames - 1}:size=1:start=0,"
                f"trim=end_frame={span.frames}"
            )
        else:
            if span.source_index < 0 or span.source_index >= len(paths):
                raise ValueError("视频时间轴引用了不存在的素材。")
            command += [
                "-ss", f"{span.source_start:.6f}",
                "-t", f"{span.duration + 1.0 / fps:.6f}",
            ]
            if settings.decode_mode == "cuda":
                command += [
                    "-hwaccel", "cuda",
                    "-hwaccel_device", "okk" if cuda_frames else str(settings.gpu_index),
                ]
                if cuda_frames:
                    command += ["-hwaccel_output_format", "cuda"]
            command += ["-an", "-sn", "-dn", "-i", paths[span.source_index]]
            chain = (
                f"[{input_index}:v:0]setpts=PTS-STARTPTS,"
                f"fps=fps={rate_text}:start_time=0,trim=end_frame={span.frames}"
            )
            input_index += 1
            if cuda_frames:
                conversion = (
                    height != reference.height
                    or _target_depth(settings, reference) != reference.bit_depth
                    or reference.pixel_format not in {
                        "yuv420p", "yuvj420p", "nv12", "yuv420p10le", "p010le",
                    }
                )
                if conversion:
                    chain += f",scale_cuda={width}:{height}:format={pixel_format}:passthrough=0"
            else:
                if height != reference.height:
                    chain += f",scale={width}:{height}"
                chain += f",format={pixel_format}"
            chain += f",setsar={sar}"
        effect = (overlays or {}).get(paths[span.source_index]) if span.source_index is not None else None
        if overlay_active(effect):
            effect = validate_effects(effect)
            if not Path(effect.overlay_path).is_file():
                raise ValueError(f"叠加特效素材不存在：{effect.overlay_path}")
            # Only the overlay belongs to this per-clip mapping. Other effects
            # are intentionally excluded from this narrowly scoped operation.
            effect = EffectSettings(overlay=effect.overlay, overlay_path=effect.overlay_path,
                                    overlay_kind=effect.overlay_kind, overlay_fps=effect.overlay_fps,
                                    overlay_opacity=effect.overlay_opacity)
            base = f"effect_base_{index}"
            chain += f",settb=AVTB,setpts=N/({rate_text}*TB)[{base}]"
            filters.append(chain)
            graph = build_effect_filter_graph(effect, in_label=f"[{base}]", out_label=f"[effect_result_{index}]")
            # The movie source loops indefinitely; retain clip-relative phase
            # when an output range or parallel chunk starts inside the clip.
            graph = graph.replace(
                f"setpts=N/({effect.overlay_fps:.6f}*TB)[ov]",
                f"setpts=N/({effect.overlay_fps:.6f}*TB),trim=start={span.source_start:.6f},setpts=PTS-STARTPTS[ov]",
            )
            graph = re.sub(r"(?<!\\)\[(bg|ov|ovs|bgs|bgp|ovp)\]",
                           lambda m: f"[effect_{index}_{m.group(1)}]", graph)
            filters.append(graph)
            filters.append(f"[effect_result_{index}]scale={width}:{height},format={pixel_format},setsar={sar},settb=AVTB,setpts=N/({rate_text}*TB)[{label}]")
        else:
            chain += f",settb=AVTB,setpts=N/({rate_text}*TB)[{label}]"
            filters.append(chain)
        labels.append(f"[{label}]")
    if len(labels) > 1:
        filters.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[vout]")
    else:
        filters.append(labels[0] + "null[vout]")
    duration = sum(span.frames for span in spans) / fps
    return command, filters, input_index, reference, duration


def _append_video_color_metadata(command: list[str], reference: MediaInfo) -> None:
    for flag, value in (
        ("-color_primaries", reference.color_primaries),
        ("-color_trc", reference.color_transfer),
        ("-colorspace", reference.color_space),
        ("-color_range", reference.color_range),
    ):
        if value and value not in {"unknown", "reserved"}:
            command += [flag, value]


def build_join_video_chunk_command(
    ffmpeg: str,
    paths: Sequence[str],
    spans: Sequence[JoinVideoSpan],
    media: Sequence[MediaInfo],
    output: str,
    settings: ExportSettings,
    caps: Capabilities,
    *, overlays: Mapping[str, EffectSettings] | None = None,
) -> list[str]:
    """Encode one independent video-only chunk for parallel NVDEC work."""
    command, filters, input_count, reference, _duration = _build_join_video_inputs(
        ffmpeg, paths, spans, media, settings, caps, overlays,
    )
    frame_count = sum(span.frames for span in spans)
    command += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-an"]
    command += build_video_args(settings, reference, caps)
    _append_video_color_metadata(command, reference)
    command += [
        "-map_metadata", "0" if input_count else "-1",
        "-max_muxing_queue_size", "4096",
        "-frames:v", str(frame_count),
    ]
    if Path(output).suffix.lower() in {".mov", ".mp4", ".m4v"}:
        rate = Fraction(reference.fps or 30.0).limit_denominator(100000)
        command += ["-video_track_timescale", str(rate.numerator)]
    command.append(output)
    return command


def build_join_mux_command(
    ffmpeg: str,
    manifest: str,
    external_audio: str,
    audio_intervals: Sequence[tuple[float, float]],
    duration: float,
    output: str,
    reference: MediaInfo,
    *,
    audio_bitrate: str = "320k",
    audio_codec: str = "aac",
    audio_sample_rate: int | None = None,
) -> list[str]:
    """Copy encoded chunks in order and add the continuous master audio once."""
    command = [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "warning",
        "-stats_period", "0.25", "-progress", "pipe:1", "-nostats",
        "-f", "concat", "-safe", "0", "-i", manifest,
    ]
    filters: list[str] = []
    _append_seeked_master_audio(command, filters, external_audio, audio_intervals, 1)
    command += [
        "-filter_complex", ";".join(filters),
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy",
    ]
    command += build_audio_encode_args(audio_codec, audio_bitrate, audio_sample_rate, output)
    _append_video_color_metadata(command, reference)
    command += [
        "-map_metadata", "0", "-fflags", "+genpts",
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "4096",
        "-t", f"{duration:.6f}", output,
    ]
    return command


def build_gap_preserving_concat_encode_command(
    ffmpeg: str,
    paths: Sequence[str],
    external_audio: str,
    results: Sequence[AlignmentResult],
    media: Sequence[MediaInfo],
    output: str,
    settings: ExportSettings,
    caps: Capabilities,
    audio_bitrate: str = "320k",
    audio_codec: str = "aac",
    audio_sample_rate: int | None = None,
    trim_before_audio: bool = False,
    keep_before_audio: bool = False,
    preserve_gaps: bool = True,
    master_audio_duration: float | None = None,
    trim_after_audio: bool = False,
    custom_start: float | None = None,
    custom_end: float | None = None,
    *, overlays: Mapping[str, EffectSettings] | None = None,
) -> list[str]:
    """Seek -> CUDA clip/black-frame concat -> one NVENC encode and audio mux.

    No full-resolution frame downloads on the supported CUDA route, no
    intermediate movie, and no full-file faststart rewrite at completion.
    """
    if len(paths) < 2 or len(paths) != len(media):
        raise ValueError("拼接至少需要两个有效视频")
    durations = [item.duration for item in media]
    video_trims, intervals = aligned_clip_timeline(
        results, durations, trim_before_audio=trim_before_audio,
        keep_before_audio=keep_before_audio,
    )
    output_start, output_end, _ = resolve_join_output_range(
        results, durations, trim_before_audio=trim_before_audio,
        keep_before_audio=keep_before_audio, preserve_gaps=preserve_gaps,
        master_audio_duration=master_audio_duration, trim_after_audio=trim_after_audio,
        custom_start=custom_start, custom_end=custom_end,
    )
    reference = media[0]
    rate = Fraction(reference.fps or 30.0).limit_denominator(100000)
    fps = float(rate)
    rate_text = f"{rate.numerator}/{rate.denominator}"
    spans = plan_join_video_spans(video_trims, intervals, output_start, output_end, fps, preserve_gaps=preserve_gaps)
    export_duration = sum(span.duration for span in spans)
    if overlays and any(overlay_active(overlays.get(paths[span.source_index]))
                        for span in spans if span.source_index is not None):
        command, filters, input_index, reference, export_duration = _build_join_video_inputs(
            ffmpeg, paths, spans, media, settings, caps, overlays,
        )
        audio_intervals = (
            [(intervals[0][0] + output_start, export_duration)] if preserve_gaps
            else [(span.master_start, span.duration) for span in spans]
        )
        _append_seeked_master_audio(command, filters, external_audio, audio_intervals, input_index)
        command += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]"]
        command += build_video_args(settings, reference, caps)
        command += build_audio_encode_args(audio_codec, audio_bitrate, audio_sample_rate, output)
        _append_video_color_metadata(command, reference)
        command += ["-map_metadata", "0" if input_index else "-1", "-max_muxing_queue_size", "4096", "-t", f"{export_duration:.6f}", output]
        return command
    height = _resolution_height(settings.resolution) or reference.height
    width = reference.width if height == reference.height else 2 * math.floor(reference.width * height / reference.height / 2 + 0.5)
    if width <= 0 or height <= 0:
        raise ValueError("无法读取有效的视频分辨率。")
    pixel_format = "p010le" if _target_depth(settings, reference) == 10 else "nv12"
    sar = reference.sample_aspect_ratio
    sar = sar.replace(":", "/") if re.fullmatch(r"[1-9]\d*:[1-9]\d*", sar) else "1"
    cuda_frames = join_uses_cuda_frames(settings, reference, caps)
    command = [ffmpeg, "-hide_banner", "-y", "-loglevel", "warning", "-stats_period", "0.25", "-progress", "pipe:1", "-nostats"]
    if cuda_frames:
        # All decoders and generated black frames share the same device.
        command += ["-init_hw_device", f"cuda=okk:{settings.gpu_index}", "-filter_hw_device", "okk"]
    filters: list[str] = []
    labels: list[str] = []
    input_index = 0
    for index, span in enumerate(spans):
        label = f"vspan{index}"
        if span.source_index is None:
            # Upload ONE black frame, then repeat its reference in GPU memory.
            # tpad(add) would force every camera frame back into system memory.
            chain = f"color=c=black:s={width}x{height}:r={rate_text},format={pixel_format},trim=end_frame=1,setsar={sar}"
            if reference.color_range == "pc":
                chain += f",scale=in_range=tv:out_range=pc,format={pixel_format}"
            if cuda_frames:
                chain += ",hwupload"
            chain += f",loop=loop={span.frames - 1}:size=1:start=0,trim=end_frame={span.frames}"
        else:
            # Input seeking still decodes the preceding GOP for an accurate
            # first frame, but skips the hours of footage outside this slice.
            command += ["-ss", f"{span.source_start:.6f}", "-t", f"{span.duration + 1.0 / fps:.6f}"]
            if settings.decode_mode == "cuda":
                command += ["-hwaccel", "cuda", "-hwaccel_device", "okk" if cuda_frames else str(settings.gpu_index)]
                if cuda_frames:
                    command += ["-hwaccel_output_format", "cuda"]
            command += ["-an", "-sn", "-dn", "-i", paths[span.source_index]]
            chain = f"[{input_index}:v:0]setpts=PTS-STARTPTS,fps=fps={rate_text}:start_time=0,trim=end_frame={span.frames}"
            input_index += 1
            if cuda_frames:
                conversion = (height != reference.height or _target_depth(settings, reference) != reference.bit_depth
                              or reference.pixel_format not in {"yuv420p", "yuvj420p", "nv12", "yuv420p10le", "p010le"})
                if conversion:
                    chain += f",scale_cuda={width}:{height}:format={pixel_format}:passthrough=0"
            else:
                if height != reference.height:
                    chain += f",scale={width}:{height}"
                chain += f",format={pixel_format}"
            chain += f",setsar={sar}"
        chain += f",settb=AVTB,setpts=N/({rate_text}*TB)[{label}]"
        filters.append(chain)
        labels.append(f"[{label}]")
    if len(labels) > 1:
        filters.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[vout]")
    else:
        filters.append(labels[0] + "null[vout]")

    audio_intervals = (
        [(intervals[0][0] + output_start, export_duration)] if preserve_gaps
        else [(span.master_start, span.duration) for span in spans]
    )
    _append_seeked_master_audio(command, filters, external_audio, audio_intervals, input_index)
    command += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]"]
    command += build_video_args(settings, reference, caps)
    command += build_audio_encode_args(audio_codec, audio_bitrate, audio_sample_rate, output)
    for flag, value in (("-color_primaries", reference.color_primaries), ("-color_trc", reference.color_transfer),
                        ("-colorspace", reference.color_space), ("-color_range", reference.color_range)):
        if value and value not in {"unknown", "reserved"}:
            command += [flag, value]
    command += ["-map_metadata", "0" if input_index else "-1", "-max_muxing_queue_size", "4096", "-t", f"{export_duration:.6f}", output]
    return command


@dataclass(frozen=True, slots=True)
class ProxyFormat:
    key: str
    label: str
    name: str
    codec: str
    extension: str
    profile: str
    pixel_format: str
    bit_depth: int
    container: str
    audio: str
    uses_nvenc: bool = False

    @property
    def extension_label(self) -> str:
        return self.extension.lstrip(".").upper()


# FFmpeg's dnxhd encoder requires yuv422p for DNxHR LB/SQ/HQ and
# yuv422p10le for HQX; prores_ks requires 10-bit 4:2:2 for 422 profiles.
PROXY_FORMATS: tuple[ProxyFormat, ...] = (
    ProxyFormat(
        key="h264_nvenc",
        label="H.264 / MP4（NVENC 最快，默认）",
        name="H.264 NVENC",
        codec="h264_nvenc",
        extension=".mp4",
        profile="",
        pixel_format="nv12",
        bit_depth=8,
        container="mp4",
        audio="aac",
        uses_nvenc=True,
    ),
    ProxyFormat(
        key="dnxhr_lb",
        label="DNxHR LB / MOV（8-bit，最小）",
        name="DNxHR LB",
        codec="dnxhd",
        extension=".mov",
        profile="dnxhr_lb",
        pixel_format="yuv422p",
        bit_depth=8,
        container="mov",
        audio="pcm_s24le",
    ),
    ProxyFormat(
        key="dnxhr_sq",
        label="DNxHR SQ / MOV（8-bit，标准）",
        name="DNxHR SQ",
        codec="dnxhd",
        extension=".mov",
        profile="dnxhr_sq",
        pixel_format="yuv422p",
        bit_depth=8,
        container="mov",
        audio="pcm_s24le",
    ),
    ProxyFormat(
        key="dnxhr_hq",
        label="DNxHR HQ / MOV（8-bit，高质量）",
        name="DNxHR HQ",
        codec="dnxhd",
        extension=".mov",
        profile="dnxhr_hq",
        pixel_format="yuv422p",
        bit_depth=8,
        container="mov",
        audio="pcm_s24le",
    ),
    ProxyFormat(
        key="dnxhr_hqx",
        label="DNxHR HQX / MOV（10-bit）",
        name="DNxHR HQX",
        codec="dnxhd",
        extension=".mov",
        profile="dnxhr_hqx",
        pixel_format="yuv422p10le",
        bit_depth=10,
        container="mov",
        audio="pcm_s24le",
    ),
    ProxyFormat(
        key="prores_proxy",
        label="ProRes 422 Proxy / MOV",
        name="ProRes 422 Proxy",
        codec="prores_ks",
        extension=".mov",
        profile="proxy",
        pixel_format="yuv422p10le",
        bit_depth=10,
        container="mov",
        audio="pcm_s24le",
    ),
    ProxyFormat(
        key="prores_lt",
        label="ProRes 422 LT / MOV",
        name="ProRes 422 LT",
        codec="prores_ks",
        extension=".mov",
        profile="lt",
        pixel_format="yuv422p10le",
        bit_depth=10,
        container="mov",
        audio="pcm_s24le",
    ),
    ProxyFormat(
        key="prores_hq",
        label="ProRes 422 HQ / MOV",
        name="ProRes 422 HQ",
        codec="prores_ks",
        extension=".mov",
        profile="hq",
        pixel_format="yuv422p10le",
        bit_depth=10,
        container="mov",
        audio="pcm_s24le",
    ),
)

DEFAULT_PROXY_FORMAT = PROXY_FORMATS[0].key
PROXY_FORMAT_BY_KEY = {fmt.key: fmt for fmt in PROXY_FORMATS}

_PROXY_FORMAT_ALIASES = {
    "h264": "h264_nvenc",
    "h264_mp4": "h264_nvenc",
    "mp4": "h264_nvenc",
    "dnxhr-lb": "dnxhr_lb",
    "dnx_hr_lb": "dnxhr_lb",
    "lb": "dnxhr_lb",
    "dnxhr-sq": "dnxhr_sq",
    "sq": "dnxhr_sq",
    "dnxhr-hq": "dnxhr_hq",
    "hq": "dnxhr_hq",
    "dnxhr-hqx": "dnxhr_hqx",
    "hqx": "dnxhr_hqx",
    "prores": "prores_proxy",
    "prores_422_proxy": "prores_proxy",
    "prores422proxy": "prores_proxy",
    "prores_422_lt": "prores_lt",
    "prores422lt": "prores_lt",
    "prores_422_hq": "prores_hq",
    "prores422hq": "prores_hq",
}


def proxy_formats() -> tuple[ProxyFormat, ...]:
    return PROXY_FORMATS


def proxy_format_labels() -> list[str]:
    return [fmt.label for fmt in PROXY_FORMATS]


def resolve_proxy_format(value: str | ProxyFormat = DEFAULT_PROXY_FORMAT) -> ProxyFormat:
    """Resolve a key, short alias or UI label to a proxy format entry."""
    if isinstance(value, ProxyFormat):
        return value
    text = str(value).strip()
    for fmt in PROXY_FORMATS:
        if text == fmt.key or text.casefold() == fmt.label.casefold():
            return fmt
    normalized = re.sub(r"[\s\-]+", "_", text.casefold())
    key = _PROXY_FORMAT_ALIASES.get(normalized)
    if key is None and normalized in PROXY_FORMAT_BY_KEY:
        key = normalized
    if key:
        return PROXY_FORMAT_BY_KEY[key]
    raise ValueError(f"未知代理格式：{value}；可选：{', '.join(fmt.key for fmt in PROXY_FORMATS)}")


def proxy_format_label(value: str | ProxyFormat = DEFAULT_PROXY_FORMAT) -> str:
    return resolve_proxy_format(value).label


def _proxy_color_args(media: MediaInfo, *, video_range: bool) -> list[str]:
    args: list[str] = []
    for flag, value in (
        ("-color_primaries", media.color_primaries),
        ("-color_trc", media.color_transfer),
        ("-colorspace", media.color_space),
    ):
        if value and value not in {"unknown", "reserved"}:
            args += [flag, value]
    if video_range:
        args += ["-color_range", "tv"]
    if media.timecode:
        args += ["-timecode", media.timecode]
    return args


def make_proxy_export_settings(
    media: MediaInfo,
    cuda_available: bool = True,
    proxy_format: str | ProxyFormat = DEFAULT_PROXY_FORMAT,
) -> ExportSettings:
    """Build settings for a 1080p proxy encode without changing the source."""
    fmt = resolve_proxy_format(proxy_format)
    if fmt.uses_nvenc:
        return ExportSettings(
            codec=fmt.codec, quality=19, preset="p2", tune="hq",
            multipass="disabled", split_encode="disabled", resolution="1080p",
            bit_depth="8-bit", audio="aac", audio_bitrate="320k",
            decode_mode="cuda" if cuda_available else "cpu",
            gpu_index=0, gop=max(1, int(round((media.fps or 30.0) * 2))),
            bframes=0, extra_args=_proxy_color_args(media, video_range=False),
            profile=fmt.profile, pixel_format=fmt.pixel_format,
        )
    return ExportSettings(
        codec=fmt.codec, quality=0, preset="", tune="",
        multipass="disabled", split_encode="disabled", resolution="1080p",
        bit_depth="10-bit" if fmt.bit_depth > 8 else "8-bit",
        audio=fmt.audio, audio_bitrate="", decode_mode="cpu",
        gpu_index=0, gop=max(1, int(round((media.fps or 30.0) * 2))),
        bframes=0, extra_args=_proxy_color_args(media, video_range=True),
        profile=fmt.profile, pixel_format=fmt.pixel_format,
    )


def make_1080p_proxy_settings(
    media: MediaInfo,
    cuda_available: bool = True,
    proxy_format: str | ProxyFormat = DEFAULT_PROXY_FORMAT,
) -> ExportSettings:
    return make_proxy_export_settings(media, cuda_available, proxy_format)


def build_proxy_encode_command(
    ffmpeg: str,
    input_path: str,
    output_path: str,
    media: MediaInfo,
    caps: Capabilities,
    proxy_format: str | ProxyFormat = DEFAULT_PROXY_FORMAT,
    *,
    cuda_available: bool | None = None,
) -> list[str]:
    """Build a complete 1080p proxy command for the selected proxy format."""
    fmt = resolve_proxy_format(proxy_format)
    if Path(input_path).resolve() == Path(output_path).resolve():
        raise ValueError("代理输出文件不能覆盖原片。")
    settings = make_1080p_proxy_settings(
        media,
        caps.cuda_decode if cuda_available is None else cuda_available,
        fmt,
    )
    return build_encode_command(
        ffmpeg, input_path, output_path, settings, media, caps,
    )


def proxy_output_path(
    source: str,
    output_directory: str | Path,
    proxy_format: str | ProxyFormat = DEFAULT_PROXY_FORMAT,
) -> Path:
    """Keep the source basename so DaVinci Resolve can batch-link proxies."""
    fmt = resolve_proxy_format(proxy_format)
    return Path(output_directory) / f"{Path(source).stem}{fmt.extension}"


def duplicate_proxy_stems(paths: Iterable[str]) -> list[str]:
    grouped: dict[str, list[str]] = {}
    for path in paths:
        grouped.setdefault(Path(path).stem.casefold(), []).append(path)
    return sorted(Path(items[0]).stem for items in grouped.values() if len(items) > 1)


def proxy_source_conflicts(
    paths: Iterable[str],
    output_directory: str | Path,
    proxy_format: str | ProxyFormat = DEFAULT_PROXY_FORMAT,
) -> list[str]:
    """Return sources whose Resolve-compatible proxy name would overwrite any source."""
    sources = list(paths)
    source_keys = {os.path.normcase(str(Path(source).resolve())) for source in sources}
    conflicts: list[str] = []
    for source in sources:
        output = proxy_output_path(source, output_directory, proxy_format)
        if os.path.normcase(str(output.resolve())) in source_keys:
            conflicts.append(source)
    return conflicts


def build_extract_video_command(ffmpeg: str, source: str, output: str) -> list[str]:
    command = [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "warning",
        "-stats_period", "0.25", "-progress", "pipe:1", "-nostats", "-i", source,
        "-map", "0:v:0", "-map_metadata", "0", "-map_chapters", "0",
        "-c:v", "copy", "-an", "-sn", "-dn",
    ]
    if Path(output).suffix.lower() in {".mp4", ".mov", ".m4v"}:
        command += ["-movflags", "+faststart"]
    command.append(output)
    return command


def build_extract_audio_command(ffmpeg: str, source: str, output: str, mode: str) -> list[str]:
    command = [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "warning",
        "-stats_period", "0.25", "-progress", "pipe:1", "-nostats", "-i", source,
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "0",
    ]
    if mode == "copy":
        command += ["-c:a", "copy"]
    elif mode == "wav24":
        command += ["-c:a", "pcm_s24le"]
    elif mode == "flac":
        command += ["-c:a", "flac", "-compression_level", "8"]
    elif mode == "aac":
        command += ["-c:a", "aac", "-b:a", "320k"]
    else:
        raise ValueError(f"未知音频分离模式：{mode}")
    if Path(output).suffix.lower() in {".m4a", ".mp4", ".mov"}:
        command += ["-movflags", "+faststart"]
    command.append(output)
    return command


def build_frame_capture_command(ffmpeg: str, source: str, timestamp: float, output: str) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "error",
        "-ss", f"{max(0.0, timestamp):.6f}", "-i", source,
        "-map", "0:v:0", "-frames:v", "1", "-fps_mode", "passthrough", output,
    ]


def build_replace_audio_command(
    ffmpeg: str,
    video: str,
    audio: str,
    output: str,
    offset_seconds: float = 0.0,
    audio_bitrate: str = "320k",
) -> list[str]:
    command = [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "warning",
        "-stats_period", "0.25", "-progress", "pipe:1", "-nostats", "-i", video,
    ]
    if offset_seconds > 0:
        command += ["-itsoffset", f"{offset_seconds:.6f}"]
    elif offset_seconds < 0:
        command += ["-ss", f"{-offset_seconds:.6f}"]
    command += [
        "-i", audio,
        "-map", "0:v:0", "-map", "1:a:0",
        "-map_metadata", "0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-af", "apad",
        "-shortest",
    ]
    if Path(output).suffix.lower() in {".mp4", ".mov", ".m4v"}:
        command += ["-movflags", "+faststart"]
    command.append(output)
    return command


def _decode_mono_pcm(ffmpeg: str, path: str, pcm_rate: int) -> bytes:
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", path,
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", str(pcm_rate), "-f", "s16le", "pipe:1",
    ]
    process = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
    )
    if process.returncode:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"无法读取音频波形：{detail[-1200:]}")
    return process.stdout


def _normalize_feature(feature: list[float]) -> list[float]:
    if not feature:
        raise RuntimeError("可用于对齐的音频不足 5 秒。")
    mean = math.fsum(feature) / len(feature)
    centered = [value - mean for value in feature]
    deviation = math.sqrt(math.fsum(value * value for value in centered) / len(centered))
    if not math.isfinite(deviation) or deviation < 1e-6:
        raise RuntimeError("音频变化过小，无法可靠自动对齐。")
    return [value / deviation for value in centered]


def _extract_envelope(
    ffmpeg: str,
    path: str,
    pcm_rate: int = 8000,
    envelope_rate: int = 50,
    *,
    use_numpy: bool = True,
):
    if use_numpy:
        import numpy as np

        raw_pcm = _decode_mono_pcm(ffmpeg, path, pcm_rate)
        pcm = np.frombuffer(raw_pcm, dtype="<i2")
        samples_per_bin = max(1, pcm_rate // envelope_rate)
        usable = (pcm.size // samples_per_bin) * samples_per_bin
        if usable < samples_per_bin * envelope_rate * 5:
            raise RuntimeError("可用于对齐的音频不足 5 秒。")
        blocks = pcm[:usable].astype(np.float32).reshape(-1, samples_per_bin)
        envelope = np.sqrt(np.mean(blocks * blocks, axis=1))
        feature = np.diff(np.log1p(envelope))
        feature -= np.mean(feature)
        deviation = float(np.std(feature))
        if not math.isfinite(deviation) or deviation < 1e-6:
            raise RuntimeError("音频变化过小，无法可靠自动对齐。")
        return feature / deviation

    # The dependency-free path intentionally uses a lower PCM rate. Energy
    # changes remain useful for concert synchronisation while memory and pure
    # Python processing stay reasonable for hour-long recordings.
    pcm_rate = min(pcm_rate, 2000)
    raw_pcm = _decode_mono_pcm(ffmpeg, path, pcm_rate)
    pcm = array("h")
    pcm.frombytes(raw_pcm)
    if sys.byteorder != "little":
        pcm.byteswap()
    samples_per_bin = max(1, pcm_rate // envelope_rate)
    bin_count = len(pcm) // samples_per_bin
    if bin_count < envelope_rate * 5:
        raise RuntimeError("可用于对齐的音频不足 5 秒。")
    iterator = iter(pcm)
    envelope: list[float] = []
    for _ in range(bin_count):
        energy = sum(value * value for value in islice(iterator, samples_per_bin))
        envelope.append(math.sqrt(energy / samples_per_bin))
    feature = [math.log1p(envelope[index]) - math.log1p(envelope[index - 1]) for index in range(1, len(envelope))]
    return _normalize_feature(feature)


def _fft_in_place(values: list[complex], inverse: bool = False) -> None:
    """Iterative radix-2 FFT used when NumPy is unavailable."""
    size = len(values)
    j = 0
    for index in range(1, size):
        bit = size >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if index < j:
            values[index], values[j] = values[j], values[index]

    length = 2
    direction = 1.0 if inverse else -1.0
    while length <= size:
        root = cmath.exp(direction * 2j * math.pi / length)
        half = length // 2
        for start in range(0, size, length):
            factor = 1.0 + 0.0j
            for offset in range(half):
                even = values[start + offset]
                odd = values[start + offset + half] * factor
                values[start + offset] = even + odd
                values[start + offset + half] = even - odd
                factor *= root
        length <<= 1
    if inverse:
        scale = 1.0 / size
        for index in range(size):
            values[index] *= scale


def _normalized_fft_correlation_python(
    reference: Sequence[float],
    candidate: Sequence[float],
    envelope_rate: int,
    max_offset_seconds: float,
    *,
    min_lag: float | None = None,
    max_lag: float | None = None,
    min_overlap_seconds: float | None = None,
    return_second_peak: bool = False,
):
    n, m = len(reference), len(candidate)
    fft_size = 1 << max(1, n + m - 1).bit_length()
    first = [complex(value) for value in reference] + [0j] * (fft_size - n)
    second = [complex(value) for value in reversed(candidate)] + [0j] * (fft_size - m)
    _fft_in_place(first)
    _fft_in_place(second)
    for index in range(fft_size):
        first[index] *= second[index]
    del second
    _fft_in_place(first, inverse=True)

    ref_energy = [0.0]
    cand_energy = [0.0]
    for value in reference:
        ref_energy.append(ref_energy[-1] + value * value)
    for value in candidate:
        cand_energy.append(cand_energy[-1] + value * value)

    min_overlap = min(n, m, max(envelope_rate * 10, int(min(n, m) * 0.05)))
    if min_overlap_seconds is not None:
        min_overlap = min(n, m, max(1, math.ceil(min_overlap_seconds * envelope_rate)))
    maximum_local_lag = int(max_offset_seconds * envelope_rate) if max_offset_seconds > 0 else max(n, m)
    first_lag = max(-(m - min_overlap), -maximum_local_lag)
    last_lag = min(n - min_overlap, maximum_local_lag)
    # Restrict the local lag before choosing its maximum. For a sampled
    # camera window, whole-camera delay is window_start + lag/rate.
    if min_lag is not None:
        first_lag = max(first_lag, math.ceil(min_lag))
    if max_lag is not None:
        last_lag = min(last_lag, math.floor(max_lag))
    best_lag: int | None = None
    best_score = float("-inf")
    peak_scores = [] if return_second_peak else None
    for lag in range(first_lag, last_lag + 1):
        ref_start = max(lag, 0)
        cand_start = max(-lag, 0)
        overlap = min(n - ref_start, m - cand_start)
        ref_sum = ref_energy[ref_start + overlap] - ref_energy[ref_start]
        cand_sum = cand_energy[cand_start + overlap] - cand_energy[cand_start]
        denominator = math.sqrt(max(0.0, ref_sum * cand_sum))
        if denominator <= 1e-9:
            continue
        score = first[lag + m - 1].real / denominator
        if peak_scores is not None:
            peak_scores.append((lag, score))
        if score > best_score:
            best_lag, best_score = lag, score
    if best_lag is None or not math.isfinite(best_score):
        raise RuntimeError("没有找到足够长的重叠音频。")
    if return_second_peak:
        radius = max(1, math.ceil(envelope_rate * 0.75))
        second = max((score for lag, score in peak_scores if abs(lag-best_lag) > radius), default=0.0)
        return best_lag, best_score, max(0.0, second)
    return best_lag, best_score


def _normalized_fft_correlation(reference, candidate, envelope_rate: int, max_offset_seconds: float,
                                *, min_lag=None, max_lag=None, min_overlap_seconds=None,
                                return_second_peak=False):
    import numpy as np

    n = int(reference.size)
    m = int(candidate.size)
    fft_size = 1 << max(1, n + m - 1).bit_length()
    spectrum = np.fft.rfft(reference, fft_size) * np.conj(np.fft.rfft(candidate, fft_size))
    circular = np.fft.irfft(spectrum, fft_size)
    correlation = np.concatenate((circular[-(m - 1):], circular[:n]))
    lags = np.arange(-(m - 1), n, dtype=np.int64)

    ref_start = np.maximum(lags, 0)
    cand_start = np.maximum(-lags, 0)
    overlap = np.minimum(n - ref_start, m - cand_start)
    ref_end = ref_start + overlap
    cand_end = cand_start + overlap

    ref_energy = np.concatenate(([0.0], np.cumsum(reference * reference, dtype=np.float64)))
    cand_energy = np.concatenate(([0.0], np.cumsum(candidate * candidate, dtype=np.float64)))
    denominator = np.sqrt(
        (ref_energy[ref_end] - ref_energy[ref_start])
        * (cand_energy[cand_end] - cand_energy[cand_start])
    )
    scores = np.divide(correlation, denominator, out=np.full_like(correlation, -np.inf), where=denominator > 1e-9)

    min_overlap = min(n, m, max(envelope_rate * 10, int(min(n, m) * 0.05)))
    if min_overlap_seconds is not None:
        min_overlap = min(n, m, max(1, math.ceil(min_overlap_seconds * envelope_rate)))
    valid = overlap >= min_overlap
    if max_offset_seconds > 0:
        valid &= np.abs(lags) <= int(max_offset_seconds * envelope_rate)
    if min_lag is not None:
        valid &= lags >= math.ceil(min_lag)
    if max_lag is not None:
        valid &= lags <= math.floor(max_lag)
    scores[~valid] = -np.inf
    index = int(np.argmax(scores))
    if not np.isfinite(scores[index]):
        raise RuntimeError("没有找到足够长的重叠音频。")
    if return_second_peak:
        radius = max(1, math.ceil(envelope_rate * 0.75))
        scores_near_peak = np.abs(lags-lags[index]) <= radius
        second = float(np.max(scores[~scores_near_peak], initial=0.0))
        return int(lags[index]), float(scores[index]), max(0.0, second)
    return int(lags[index]), float(scores[index])


def align_audio(
    ffmpeg: str,
    video: str,
    external_audio: str,
    *,
    envelope_rate: int = 50,
    max_offset_seconds: float = 1800.0,
) -> AlignmentResult:
    """Return MPV/FFmpeg audio delay: positive means delay the external audio."""
    return align_audio_segments(
        ffmpeg,
        [video],
        external_audio,
        envelope_rate=envelope_rate,
        max_offset_seconds=max_offset_seconds,
    )[0]


def align_audio_segments(
    ffmpeg: str,
    videos: Sequence[str],
    external_audio: str,
    *,
    envelope_rate: int = 50,
    max_offset_seconds: float = 0.0,
    mode: str = "fast",
    cache_dir=None,
    cancel_event=None,
    progress=None,
    continue_on_error: bool = False,
) -> list[AlignmentResult]:
    """Align to the complete master clock using cached, streaming analysis."""
    from audio_sync import align_audio_segments_fast

    return align_audio_segments_fast(
        ffmpeg, videos, external_audio, envelope_rate=envelope_rate,
        max_offset_seconds=max_offset_seconds, mode=mode, cache_dir=cache_dir,
        cancel_event=cancel_event, progress=progress,
        continue_on_error=continue_on_error,
    )


def _align_audio_segments_legacy(
    ffmpeg: str,
    videos: Sequence[str],
    external_audio: str,
    *,
    envelope_rate: int = 50,
    max_offset_seconds: float = 0.0,
) -> list[AlignmentResult]:
    """Align every camera segment to one continuous master audio recording.

    The master envelope is decoded only once. A negative returned offset means
    the matching camera segment begins ``-offset`` seconds into the master.
    ``max_offset_seconds=0`` searches the complete recording, which is required
    for camera files split after two hours.
    """
    if not videos:
        raise ValueError("至少需要一个待对齐的视频")
    try:
        import numpy  # noqa: F401
        use_numpy = True
    except Exception:
        use_numpy = False

    analysis_rate = envelope_rate if use_numpy else min(envelope_rate, 20)
    candidate = _extract_envelope(
        ffmpeg, external_audio, envelope_rate=analysis_rate, use_numpy=use_numpy,
    )
    results: list[AlignmentResult] = []
    for video in videos:
        reference = _extract_envelope(
            ffmpeg, video, envelope_rate=analysis_rate, use_numpy=use_numpy,
        )
        if use_numpy:
            lag, score = _normalized_fft_correlation(
                reference, candidate, analysis_rate, max_offset_seconds,
            )
            engine = "NumPy 高速"
        else:
            lag, score = _normalized_fft_correlation_python(
                reference, candidate, analysis_rate, max_offset_seconds,
            )
            engine = "内置兼容"
        results.append(AlignmentResult(lag / analysis_rate, score, analysis_rate, engine))
    return results
