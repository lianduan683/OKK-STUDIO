"""Lossless cut detection helpers built on FFmpeg log output.

The module intentionally has no third-party dependencies.  It parses the
text emitted by FFmpeg's ``blackdetect``, ``silencedetect`` and scene
detection filters, converts the detected intervals to ``CutSegment`` values
and provides small helpers for removing or splitting segments.  It also
builds lightweight timeline thumbnail commands and streams 16-bit PCM peaks
for waveform drawing without NumPy.

All time values are seconds.  Negative timestamps (common with some
containers), ``NaN``/``inf`` values and log lines emitted out of order are
handled explicitly; the parsers never require the log to be sorted.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import sys
import threading
from array import array
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
PCM_PEAK_SCALE = 32768.0

_EPSILON = 1e-9
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"

# Log formats differ between FFmpeg versions.  blackdetect/silencedetect
# print ``key:value`` pairs, select at debug level prints ``t:`` as well as
# ``scene:`` and scdet prints ``lavfi.scd.*`` metadata.
_BLACK_EVENT_RE = re.compile(
    rf"\b(black_start|black_end|black_duration)\s*:\s*({_NUMBER})",
    re.IGNORECASE,
)
_BLACK_FIELD_RE = re.compile(
    r"\b(black_start|black_end|black_duration)\s*:",
    re.IGNORECASE,
)
_SILENCE_EVENT_RE = re.compile(
    rf"\b(silence_start|silence_end|silence_duration)\s*:\s*({_NUMBER})",
    re.IGNORECASE,
)
_SILENCE_FIELD_RE = re.compile(
    r"\b(silence_start|silence_end|silence_duration)\s*:",
    re.IGNORECASE,
)
_PTS_TIME_RE = re.compile(rf"\bpts_time\s*:\s*({_NUMBER})", re.IGNORECASE)
_SELECT_TIME_RE = re.compile(rf"(?<![A-Za-z0-9_])t\s*:\s*({_NUMBER})", re.IGNORECASE)
_SCENE_RE = re.compile(rf"\bscene\s*:\s*({_NUMBER})", re.IGNORECASE)
_SCENE_FIELD_RE = re.compile(r"\bscene\s*:", re.IGNORECASE)
_SCD_SCORE_RE = re.compile(
    rf"\blavfi\.scd\.score\s*[:=]\s*({_NUMBER})",
    re.IGNORECASE,
)
_SCD_SCORE_FIELD_RE = re.compile(r"\blavfi\.scd\.score\s*[:=]", re.IGNORECASE)
_SCD_TIME_RE = re.compile(
    rf"\blavfi\.scd\.time\s*[:=]\s*({_NUMBER})",
    re.IGNORECASE,
)
_FRAME_RE = re.compile(rf"(?<![A-Za-z0-9_])n\s*:\s*({_NUMBER})", re.IGNORECASE)
_SHOWINFO_RE = re.compile(r"\bParsed_showinfo", re.IGNORECASE)
_SELECT_DEBUG_RE = re.compile(r"\bselect_out\s*:", re.IGNORECASE)
_SELECT_OUT_RE = re.compile(r"\bselect_out\s*:\s*(" + _NUMBER + r")", re.IGNORECASE)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _require_finite(value: Any, label: str) -> float:
    result = _finite(value)
    if result is None:
        raise ValueError(f"{label}必须是有限数字。")
    return result


def _require_positive(value: Any, label: str) -> float:
    result = _require_finite(value, label)
    if result <= 0:
        raise ValueError(f"{label}必须大于零。")
    return result


def _require_non_negative(value: Any, label: str) -> float:
    result = _require_finite(value, label)
    if result < 0:
        raise ValueError(f"{label}不能小于零。")
    return result


def _require_int(value: Any, label: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是整数。")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{label}必须是整数。")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是整数。") from exc
    if result < minimum:
        raise ValueError(f"{label}不能小于 {minimum}。")
    return result


def _validate_duration(duration: Any) -> float | None:
    if duration is None:
        return None
    return _require_non_negative(duration, "素材时长")


def _ffmpeg_text(ffmpeg: Any) -> str:
    text = str(ffmpeg or "").strip()
    if not text:
        raise ValueError("FFmpeg 路径不能为空。")
    return text


def _selected_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "否"}
    return bool(value)


def _normalize_tags(tags: Any) -> tuple[str, ...]:
    if tags is None:
        return ()
    if isinstance(tags, str):
        values: Iterable[Any] = (tags,)
    elif isinstance(tags, (set, frozenset)):
        values = sorted(tags, key=str)
    else:
        try:
            values = list(tags)
        except TypeError as exc:
            raise ValueError("标签必须是字符串或字符串序列。") from exc
    result: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in result:
            result.append(text)
    return tuple(result)


@dataclass(frozen=True, slots=True, eq=False)
class TimeRange:
    """A half-open (or, for detection, closed enough) time interval.

    Instances are tuple-like and also compare equal to ``(start, end)``
    pairs, which keeps parser output convenient for callers that treat
    detected ranges as plain tuples.
    """

    start: float
    end: float

    def __post_init__(self) -> None:
        start = _require_finite(self.start, "区间起点")
        end = _require_finite(self.end, "区间终点")
        if end <= start + _EPSILON:
            raise ValueError("区间终点必须晚于起点。")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2.0

    def contains(self, time: float, tolerance: float = 0.0) -> bool:
        return self.start - tolerance <= time <= self.end + tolerance

    def overlaps(self, other: "TimeRange", tolerance: float = 0.0) -> bool:
        return self.start < other.end + tolerance and other.start < self.end + tolerance

    def __eq__(self, other: object) -> bool:
        if isinstance(other, TimeRange):
            return self.start == other.start and self.end == other.end
        if isinstance(other, (tuple, list)) and len(other) == 2:
            try:
                return float(other[0]) == self.start and float(other[1]) == self.end
            except (TypeError, ValueError):
                return False
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.start, self.end))

    def __iter__(self) -> Iterator[float]:
        yield self.start
        yield self.end

    def __len__(self) -> int:
        return 2

    def __getitem__(self, index: int) -> float:
        if index in (0, -2):
            return self.start
        if index in (1, -1):
            return self.end
        raise IndexError("区间索引越界。")


@dataclass(slots=True)
class CutSegment:
    """A cut segment shape shared with the editor UI.

    The five fields mirror the segment model used by the rest of the
    application (``start``, ``end``, ``name``, ``tags``, ``selected``).
    Foreign segment objects with the same attributes are accepted by all
    helper functions and are normalised through :func:`coerce_cut_segment`.
    """

    start: float
    end: float
    name: str = ""
    tags: tuple[str, ...] | dict[str, str] = ()
    selected: bool = True

    def __post_init__(self) -> None:
        start = _require_finite(self.start, "片段起点")
        end = _require_finite(self.end, "片段终点")
        if end <= start + _EPSILON:
            raise ValueError("片段终点必须晚于起点。")
        self.start = start
        self.end = end
        self.name = "" if self.name is None else str(self.name)
        if isinstance(self.tags, Mapping):
            self.tags = {str(key): str(value) for key, value in self.tags.items()}
        else:
            self.tags = _normalize_tags(self.tags)
        self.selected = _selected_bool(self.selected)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2.0

    def to_dict(self) -> dict[str, Any]:
        tags = dict(self.tags) if isinstance(self.tags, Mapping) else list(self.tags)
        return {
            "start": self.start,
            "end": self.end,
            "name": self.name,
            "tags": tags,
            "selected": self.selected,
        }

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "CutSegment":
        return coerce_cut_segment(values)


@dataclass(frozen=True, slots=True)
class SceneEvent:
    """A scene-change event from select/showinfo or scdet logs.

    ``score`` is 0..1 for the select filter and 0..100 for scdet; it is
    ``None`` when a showinfo log only contains the selected frame times.
    """

    time: float
    score: float | None = None
    frame: int | None = None

    def __post_init__(self) -> None:
        time = _require_finite(self.time, "场景时间")
        score = None if self.score is None else _require_finite(self.score, "场景分数")
        frame = None if self.frame is None else _require_int(self.frame, "帧序号", minimum=0)
        object.__setattr__(self, "time", time)
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "frame", frame)


@dataclass(frozen=True, slots=True)
class PeakWindow:
    """One PCM peak window.  ``peak`` and ``rms`` use raw int16 units."""

    index: int
    start: float
    duration: float
    peak: float
    rms: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "index", _require_int(self.index, "窗口序号", minimum=0))
        object.__setattr__(self, "start", _require_finite(self.start, "窗口起点"))
        object.__setattr__(self, "duration", _require_positive(self.duration, "窗口时长"))
        object.__setattr__(self, "peak", _require_non_negative(self.peak, "峰值"))
        object.__setattr__(self, "rms", _require_non_negative(self.rms, "RMS"))

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def normalized_peak(self) -> float:
        return self.peak / PCM_PEAK_SCALE


@dataclass(frozen=True, slots=True)
class Waveform:
    """A regular peak envelope in raw int16 units (0..32768)."""

    peaks: tuple[float, ...]
    rms: tuple[float, ...]
    window_seconds: float
    sample_rate: int
    start: float = 0.0
    duration: float = 0.0

    def __post_init__(self) -> None:
        peaks = tuple(_require_non_negative(value, "波形峰值") for value in self.peaks)
        rms = tuple(_require_non_negative(value, "波形 RMS") for value in self.rms)
        if rms and len(rms) != len(peaks):
            raise ValueError("RMS 数量必须与峰值数量一致。")
        window = _require_positive(self.window_seconds, "波形窗口")
        rate = _require_int(self.sample_rate, "波形采样率")
        start = _require_non_negative(self.start, "波形起点")
        duration = _require_non_negative(self.duration, "波形时长")
        object.__setattr__(self, "peaks", peaks)
        object.__setattr__(self, "rms", rms)
        object.__setattr__(self, "window_seconds", window)
        object.__setattr__(self, "sample_rate", rate)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "duration", duration)

    def __len__(self) -> int:
        return len(self.peaks)

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def max_peak(self) -> float:
        return max(self.peaks, default=0.0)

    def normalized_peaks(self) -> tuple[float, ...]:
        return tuple(value / PCM_PEAK_SCALE for value in self.peaks)

    def time_at(self, index: int) -> float:
        return self.start + index * self.window_seconds

    def peak_at(self, time: float) -> float:
        if not self.peaks:
            return 0.0
        value = _finite(time)
        if value is None:
            return 0.0
        if value <= self.start:
            return self.peaks[0]
        index = int((value - self.start) / self.window_seconds)
        if index < 0:
            return self.peaks[0]
        if index >= len(self.peaks):
            return self.peaks[-1]
        return self.peaks[index]

    def sample(self, max_points: int) -> "Waveform":
        limit = _require_int(max_points, "采样点数")
        count = len(self.peaks)
        if count == 0 or count <= limit:
            return self
        group = math.ceil(count / limit)
        peaks: list[float] = []
        rms: list[float] = []
        for offset in range(0, count, group):
            peaks.append(max(self.peaks[offset : offset + group]))
            if self.rms:
                rms.append(max(self.rms[offset : offset + group]))
        return Waveform(
            tuple(peaks),
            tuple(rms),
            self.window_seconds * group,
            self.sample_rate,
            self.start,
            self.duration,
        )

    def to_pairs(self) -> list[tuple[float, float]]:
        return [(self.time_at(index), peak) for index, peak in enumerate(self.peaks)]


def _range_bounds(value: Any) -> tuple[float, float]:
    if isinstance(value, TimeRange):
        return value.start, value.end
    if isinstance(value, CutSegment):
        return value.start, value.end
    if isinstance(value, Mapping):
        start = value.get("start")
        end = value.get("end")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) < 2:
            raise ValueError("区间至少需要起点和终点。")
        start, end = value[0], value[1]
    else:
        start = getattr(value, "start", None)
        end = getattr(value, "end", None)
    if start is None or end is None:
        raise ValueError("区间必须提供 start 和 end。")
    start_value = _finite(start)
    end_value = _finite(end)
    if start_value is None or end_value is None:
        raise ValueError("区间时间必须是有限数字。")
    return start_value, end_value


def _as_segment_iterable(segments: Any) -> Iterable[Any]:
    if segments is None:
        return ()
    if isinstance(segments, (CutSegment, TimeRange, Mapping)):
        return (segments,)
    if isinstance(segments, Sequence) and not isinstance(segments, (str, bytes, bytearray)):
        if 2 <= len(segments) <= 5 and all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in segments[:2]
        ):
            return (segments,)
    if hasattr(segments, "start") and hasattr(segments, "end"):
        return (segments,)
    return segments


def _as_range_iterable(ranges: Any) -> Iterable[Any]:
    if ranges is None:
        return ()
    if isinstance(ranges, (TimeRange, CutSegment, Mapping)):
        return (ranges,)
    if isinstance(ranges, Sequence) and not isinstance(ranges, (str, bytes, bytearray)):
        if len(ranges) == 2 and all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in ranges
        ):
            return (ranges,)
    if hasattr(ranges, "start") and hasattr(ranges, "end"):
        return (ranges,)
    return ranges


def normalize_ranges(
    ranges: Iterable[Any] | None,
    *,
    duration: float | None = None,
    merge_gap: float = 0.0,
    min_duration: float = 0.0,
    merge_touching: bool = True,
) -> list[TimeRange]:
    """Clamp, sort and merge arbitrary ranges.

    Negative starts are clipped to zero, ranges beyond ``duration`` are
    clipped, non-finite and empty ranges are dropped.  ``merge_gap`` joins
    ranges separated by no more than that many seconds.  Touching ranges
    are merged too unless ``merge_touching`` is false; scene partitions set
    it to false because adjacent shots share a boundary but must stay
    separate.
    """

    limit = _validate_duration(duration)
    gap = _require_non_negative(merge_gap, "合并间隔")
    minimum = _require_non_negative(min_duration, "最小区间时长")
    collected: list[tuple[float, float]] = []
    for item in _as_range_iterable(ranges):
        start, end = _range_bounds(item)
        if limit is not None:
            if start < 0:
                start = 0.0
            if end > limit:
                end = limit
            if start >= limit:
                continue
        elif start < 0:
            start = 0.0
        if end <= start + _EPSILON:
            continue
        if minimum and end - start < minimum - _EPSILON:
            continue
        collected.append((start, end))
    collected.sort()
    merged: list[tuple[float, float]] = []
    for start, end in collected:
        if merged:
            previous_end = merged[-1][1]
            overlaps = start < previous_end - _EPSILON
            touching = abs(start - previous_end) <= _EPSILON
            within_gap = start <= previous_end + gap + _EPSILON
            if overlaps or (within_gap and (merge_touching or gap > 0 or not touching)):
                if end > previous_end:
                    merged[-1] = (merged[-1][0], end)
                continue
        merged.append((start, end))
    return [TimeRange(start, end) for start, end in merged]


def subtract_ranges(
    base_ranges: Iterable[Any] | None,
    cut_ranges: Iterable[Any] | None,
    *,
    duration: float | None = None,
    min_duration: float = 0.0,
) -> list[TimeRange]:
    """Return the parts of ``base_ranges`` that are not in ``cut_ranges``."""

    minimum = _require_non_negative(min_duration, "最小区间时长")
    base = normalize_ranges(base_ranges, duration=duration, merge_touching=False)
    cuts = normalize_ranges(cut_ranges, duration=duration)
    result: list[TimeRange] = []
    for base_range in base:
        cursor = base_range.start
        for cut in cuts:
            if cut.end <= cursor + _EPSILON:
                continue
            if cut.start >= base_range.end - _EPSILON:
                break
            piece_end = min(cut.start, base_range.end)
            if piece_end - cursor >= minimum - _EPSILON and piece_end > cursor + _EPSILON:
                result.append(TimeRange(cursor, piece_end))
            cursor = max(cursor, cut.end)
            if cursor >= base_range.end - _EPSILON:
                break
        if base_range.end - cursor >= minimum - _EPSILON and cursor < base_range.end - _EPSILON:
            result.append(TimeRange(cursor, base_range.end))
    return result


def invert_ranges(
    ranges: Iterable[Any] | None,
    duration: float,
    *,
    min_duration: float = 0.0,
) -> list[TimeRange]:
    """Return the complement of ``ranges`` inside ``[0, duration]``."""

    limit = _require_positive(duration, "素材时长")
    return subtract_ranges(
        [TimeRange(0.0, limit)],
        ranges,
        duration=limit,
        min_duration=min_duration,
    )


def ranges_to_cut_segments(
    ranges: Iterable[Any] | None,
    *,
    name: str = "",
    tags: Any = (),
    selected: bool = True,
    duration: float | None = None,
    merge_gap: float = 0.0,
    min_duration: float = 0.0,
    merge_touching: bool = True,
    factory: Callable[..., Any] | None = None,
) -> list[Any]:
    """Convert detected ranges to segment objects.

    ``factory`` can be supplied when the caller wants its own ``CutSegment``
    class; by default :class:`CutSegment` from this module is used.
    """

    normalized = normalize_ranges(
        ranges,
        duration=duration,
        merge_gap=merge_gap,
        min_duration=min_duration,
        merge_touching=merge_touching,
    )
    segment_type = factory or CutSegment
    normalized_tags = (
        {str(key): str(value) for key, value in tags.items()}
        if isinstance(tags, Mapping)
        else _normalize_tags(tags)
    )
    if (
        not isinstance(normalized_tags, Mapping)
        and getattr(segment_type, "__module__", "") == "lossless_core"
    ):
        # lossless_core stores tags as a string-to-string mapping.
        normalized_tags = {tag: "1" for tag in normalized_tags}
    return [
        segment_type(
            start=item.start,
            end=item.end,
            name=name if name is not None else "",
            tags=normalized_tags,
            selected=_selected_bool(selected),
        )
        for item in normalized
    ]


segments_from_ranges = ranges_to_cut_segments


def coerce_cut_segment(value: Any) -> CutSegment:
    """Normalise a mapping, tuple or foreign segment object."""

    if isinstance(value, CutSegment):
        return value
    if isinstance(value, Mapping):
        start = value.get("start")
        end = value.get("end")
        name = value.get("name", "")
        tags = value.get("tags", ())
        selected = value.get("selected", True)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) < 2:
            raise ValueError("片段至少需要起点和终点。")
        start, end = value[0], value[1]
        name = value[2] if len(value) > 2 else ""
        tags = value[3] if len(value) > 3 else ()
        selected = value[4] if len(value) > 4 else True
    else:
        start = getattr(value, "start", None)
        end = getattr(value, "end", None)
        name = getattr(value, "name", "")
        tags = getattr(value, "tags", ())
        selected = getattr(value, "selected", True)
    if start is None or end is None:
        raise ValueError("片段必须提供 start 和 end。")
    return CutSegment(start, end, name, tags, selected)


def coerce_cut_segments(segments: Iterable[Any] | None) -> list[CutSegment]:
    return [coerce_cut_segment(segment) for segment in _as_segment_iterable(segments)]


def _segment_parts(value: Any) -> tuple[float, float, str, Any, bool]:
    """Read the shared segment fields without forcing a concrete class."""

    start, end = _range_bounds(value)
    if isinstance(value, Mapping):
        name = value.get("name", "")
        tags = value.get("tags", ())
        selected = value.get("selected", True)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        name = value[2] if len(value) > 2 else ""
        tags = value[3] if len(value) > 3 else ()
        selected = value[4] if len(value) > 4 else True
    else:
        name = getattr(value, "name", "")
        tags = getattr(value, "tags", ())
        selected = getattr(value, "selected", True)
    if end <= start + _EPSILON:
        raise ValueError("片段终点必须晚于起点。")
    return (
        start,
        end,
        "" if name is None else str(name),
        tags,
        _selected_bool(selected),
    )


def _merge_tags(tags: Any, add_tags: Any) -> Any:
    if isinstance(tags, Mapping):
        result = {str(key): str(value) for key, value in tags.items()}
        if isinstance(add_tags, Mapping):
            for key, value in add_tags.items():
                result[str(key)] = str(value)
        else:
            for tag in _normalize_tags(add_tags):
                result.setdefault(tag, "1")
        return result
    merged: list[str] = list(_normalize_tags(tags))
    for tag in _normalize_tags(add_tags):
        if tag not in merged:
            merged.append(tag)
    return tuple(merged)


def _normalized_source(value: Any) -> Any:
    if isinstance(value, TimeRange):
        return coerce_cut_segment(value)
    if isinstance(value, Mapping) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    ):
        return coerce_cut_segment(value)
    return value


def _clone_segment_like(
    source: Any,
    start: float,
    end: float,
    *,
    name: str | None,
    add_tags: Any,
    selected: bool | None,
) -> Any:
    """Clone a segment while preserving its concrete class when possible."""

    _, _, source_name, source_tags, source_selected = _segment_parts(source)
    merged_tags = _merge_tags(source_tags, add_tags)
    final_name = source_name if name is None else str(name)
    final_selected = (
        source_selected if selected is None else _selected_bool(selected)
    )
    if isinstance(source, CutSegment):
        return CutSegment(start, end, final_name, merged_tags, final_selected)
    clone = getattr(source, "clone", None)
    if callable(clone):
        try:
            return clone(
                new_id=True,
                start=start,
                end=end,
                name=final_name,
                tags=merged_tags,
                selected=final_selected,
            )
        except (TypeError, ValueError):
            pass
    return CutSegment(start, end, final_name, merged_tags, final_selected)


def to_core_segments(
    segments: Iterable[Any] | None,
    *,
    tag_value: str = "1",
) -> list[Any]:
    """Convert ranges or foreign segments to ``lossless_core.CutSegment``.

    Detection tags are string labels, while ``lossless_core`` stores a
    string-to-string mapping.  Each label becomes a mapping entry with
    ``tag_value`` as its value; existing mappings are preserved.
    """

    try:
        from lossless_core import CutSegment as CoreCutSegment
        from lossless_core import make_segment_id
    except (ImportError, AttributeError) as exc:
        raise RuntimeError("当前环境没有可用的 lossless_core.CutSegment。") from exc
    result: list[Any] = []
    for source in _as_segment_iterable(segments):
        start, end, name, tags, selected = _segment_parts(source)
        if isinstance(tags, Mapping):
            tag_map = {str(key): str(value) for key, value in tags.items()}
        else:
            tag_map = {tag: str(tag_value) for tag in _normalize_tags(tags)}
        result.append(
            CoreCutSegment(
                id=make_segment_id(),
                start=start,
                end=end,
                name=name,
                tags=tag_map,
                selected=_selected_bool(selected),
            )
        )
    return result


def cut_out_ranges(
    segments: Iterable[Any] | None,
    ranges: Iterable[Any] | None,
    *,
    duration: float | None = None,
    min_duration: float = 0.0,
    add_tags: Any = (),
    name: str | None = None,
    selected: bool | None = None,
) -> list[Any]:
    """Remove ``ranges`` from every segment and return the remaining pieces.

    Foreign segment objects (for example ``lossless_core.CutSegment``) are
    preserved: their type, identifier and tag mapping are kept when the
    object exposes a ``clone(new_id=True, ...)`` method.
    """

    minimum = _require_non_negative(min_duration, "最短片段时长")
    cut_ranges = normalize_ranges(ranges, duration=duration)
    result: list[Any] = []
    for source_value in _as_segment_iterable(segments):
        source_start, source_end, _, _, _ = _segment_parts(source_value)
        relevant = [
            cut
            for cut in cut_ranges
            if cut.start < source_end - _EPSILON and cut.end > source_start + _EPSILON
        ]
        if not relevant:
            result.append(_normalized_source(source_value))
            continue
        for piece in subtract_ranges(
            [(source_start, source_end)],
            relevant,
            duration=duration,
            min_duration=minimum,
        ):
            result.append(
                _clone_segment_like(
                    source_value,
                    piece.start,
                    piece.end,
                    name=name,
                    add_tags=add_tags,
                    selected=selected,
                )
            )
    return result


def _ranges_or_log(
    value: Any,
    parser: Callable[..., list[TimeRange]],
    *,
    duration: float | None,
    merge_gap: float,
    min_duration: float,
) -> Any:
    if isinstance(value, (str, bytes, bytearray)):
        return parser(
            value,
            duration=duration,
            merge_gap=merge_gap,
            min_duration=min_duration,
        )
    return value


def cut_segments_by_black(
    segments: Iterable[Any] | None,
    black: Iterable[Any] | str | bytes,
    *,
    duration: float | None = None,
    min_duration: float = 0.0,
    merge_gap: float = 0.0,
    add_tags: Any = (),
    name: str | None = None,
    selected: bool | None = None,
) -> list[Any]:
    """Remove black intervals (ranges or a blackdetect log) from segments."""

    ranges = normalize_ranges(
        _ranges_or_log(
            black,
            parse_blackdetect,
            duration=duration,
            merge_gap=merge_gap,
            min_duration=0.0,
        ),
        duration=duration,
        merge_gap=merge_gap,
    )
    return cut_out_ranges(
        segments,
        ranges,
        duration=duration,
        min_duration=min_duration,
        add_tags=add_tags,
        name=name,
        selected=selected,
    )


def cut_segments_by_silence(
    segments: Iterable[Any] | None,
    silence: Iterable[Any] | str | bytes,
    *,
    duration: float | None = None,
    min_duration: float = 0.0,
    merge_gap: float = 0.0,
    add_tags: Any = (),
    name: str | None = None,
    selected: bool | None = None,
) -> list[Any]:
    """Remove silent intervals (ranges or a silencedetect log) from segments."""

    ranges = normalize_ranges(
        _ranges_or_log(
            silence,
            parse_silencedetect,
            duration=duration,
            merge_gap=merge_gap,
            min_duration=0.0,
        ),
        duration=duration,
        merge_gap=merge_gap,
    )
    return cut_out_ranges(
        segments,
        ranges,
        duration=duration,
        min_duration=min_duration,
        add_tags=add_tags,
        name=name,
        selected=selected,
    )


def cut_segments_by_black_or_silence(
    segments: Iterable[Any] | None,
    black: Iterable[Any] | str | bytes = (),
    silence: Iterable[Any] | str | bytes = (),
    *,
    remove_black: bool = True,
    remove_silence: bool = True,
    duration: float | None = None,
    min_duration: float = 0.0,
    merge_gap: float = 0.0,
    add_tags: Any = (),
    name: str | None = None,
    selected: bool | None = None,
) -> list[Any]:
    """Remove black and/or silent intervals in one pass."""

    ranges: list[TimeRange] = []
    if remove_black:
        ranges.extend(
            normalize_ranges(
                _ranges_or_log(
                    black,
                    parse_blackdetect,
                    duration=duration,
                    merge_gap=merge_gap,
                    min_duration=0.0,
                ),
                duration=duration,
                merge_gap=merge_gap,
            )
        )
    if remove_silence:
        ranges.extend(
            normalize_ranges(
                _ranges_or_log(
                    silence,
                    parse_silencedetect,
                    duration=duration,
                    merge_gap=merge_gap,
                    min_duration=0.0,
                ),
                duration=duration,
                merge_gap=merge_gap,
            )
        )
    return cut_out_ranges(
        segments,
        ranges,
        duration=duration,
        min_duration=min_duration,
        add_tags=add_tags,
        name=name,
        selected=selected,
    )


def split_segments_by_scene(
    segments: Iterable[Any] | None,
    cuts: Iterable[Any] | str | bytes,
    *,
    duration: float | None = None,
    min_duration: float = 0.0,
    dedupe_tolerance: float = 1e-4,
    name: str | None = None,
    add_tags: Any = (),
    selected: bool | None = None,
) -> list[Any]:
    """Split segments at scene-change times (iterable or select/scdet log).

    Input segment types are preserved when they expose a standard ``clone``
    method; every resulting piece receives a fresh identifier.
    """

    minimum = _require_non_negative(min_duration, "最短片段时长")
    cut_times = _coerce_cut_times(
        cuts,
        duration=duration,
        dedupe_tolerance=dedupe_tolerance,
    )
    result: list[Any] = []
    for source_value in _as_segment_iterable(segments):
        source_start, source_end, _, _, _ = _segment_parts(source_value)
        boundaries = [source_start]
        for cut in cut_times:
            if cut <= source_start + _EPSILON or cut >= source_end - _EPSILON:
                continue
            if cut - boundaries[-1] < minimum - _EPSILON:
                continue
            if source_end - cut < minimum - _EPSILON:
                continue
            boundaries.append(cut)
        boundaries.append(source_end)
        if len(boundaries) == 2:
            if name is None and not _normalize_tags(add_tags) and selected is None:
                result.append(_normalized_source(source_value))
            else:
                result.append(
                    _clone_segment_like(
                        source_value,
                        source_start,
                        source_end,
                        name=name,
                        add_tags=add_tags,
                        selected=selected,
                    )
                )
            continue
        for start, end in zip(boundaries, boundaries[1:]):
            result.append(
                _clone_segment_like(
                    source_value,
                    start,
                    end,
                    name=name,
                    add_tags=add_tags,
                    selected=selected,
                )
            )
    return result


remove_black_and_silence = cut_segments_by_black_or_silence
remove_black_ranges = cut_segments_by_black
remove_silence_ranges = cut_segments_by_silence
ranges_to_core_segments = to_core_segments
split_by_scene = split_segments_by_scene


def _iter_log_lines(log: str | bytes | Iterable[str | bytes]) -> Iterator[str]:
    if isinstance(log, bytes):
        log = log.decode("utf-8", errors="replace")
    if isinstance(log, str):
        yield from log.splitlines()
        return
    try:
        iterator = iter(log)
    except TypeError as exc:
        raise TypeError("日志必须是文本、字节或文本行序列。") from exc
    for line in iterator:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        yield str(line).rstrip("\r\n")


def _parse_interval_log(
    log: str | bytes | Iterable[str | bytes],
    pattern: re.Pattern[str],
    field_pattern: re.Pattern[str],
    keys: tuple[str, str, str],
    *,
    duration: float | None,
    merge_gap: float,
    min_duration: float,
) -> list[TimeRange]:
    start_key, end_key, duration_key = keys
    limit = _validate_duration(duration)
    ranges: list[tuple[float, float]] = []
    pending_start: float | None = None

    for line in _iter_log_lines(log):
        start_value: float | None = None
        end_value: float | None = None
        length_value: float | None = None
        seen_start = False
        seen_end = False
        seen_duration = False
        for match in field_pattern.finditer(line):
            key = match.group(1).lower()
            if key == start_key:
                seen_start = True
            elif key == end_key:
                seen_end = True
            elif key == duration_key:
                seen_duration = True
        for match in pattern.finditer(line):
            key = match.group(1).lower()
            value = _finite(match.group(2))
            if key == start_key and value is not None:
                start_value = value
            elif key == end_key and value is not None:
                end_value = value
            elif key == duration_key and value is not None:
                length_value = value

        if seen_start and start_value is None:
            # An explicit but invalid start (NaN/inf) invalidates the event;
            # never let it silently close a previous pending interval.
            pending_start = None
            continue
        if start_value is not None and end_value is not None:
            ranges.append((start_value, end_value))
            pending_start = None
        elif start_value is not None and length_value is not None:
            ranges.append((start_value, start_value + length_value))
            pending_start = None
        elif start_value is not None and not seen_end and not seen_duration:
            pending_start = start_value
        elif start_value is not None:
            # A present-but-invalid end/duration on this line invalidates it.
            pending_start = None
        elif end_value is not None:
            if pending_start is not None:
                ranges.append((pending_start, end_value))
                pending_start = None
            elif length_value is not None:
                ranges.append((end_value - length_value, end_value))
        elif seen_start and seen_end:
            pending_start = None

    if pending_start is not None and limit is not None and limit > pending_start + _EPSILON:
        ranges.append((pending_start, limit))

    return normalize_ranges(
        ranges,
        duration=limit,
        merge_gap=merge_gap,
        min_duration=min_duration,
    )


def parse_blackdetect(
    log: str | bytes | Iterable[str | bytes],
    *,
    duration: float | None = None,
    merge_gap: float = 0.0,
    min_duration: float = 0.0,
) -> list[TimeRange]:
    """Parse ``blackdetect`` output into normalised ranges."""

    return _parse_interval_log(
        log,
        _BLACK_EVENT_RE,
        _BLACK_FIELD_RE,
        ("black_start", "black_end", "black_duration"),
        duration=duration,
        merge_gap=merge_gap,
        min_duration=min_duration,
    )


def parse_silencedetect(
    log: str | bytes | Iterable[str | bytes],
    *,
    duration: float | None = None,
    merge_gap: float = 0.0,
    min_duration: float = 0.0,
) -> list[TimeRange]:
    """Parse ``silencedetect`` output into normalised ranges."""

    return _parse_interval_log(
        log,
        _SILENCE_EVENT_RE,
        _SILENCE_FIELD_RE,
        ("silence_start", "silence_end", "silence_duration"),
        duration=duration,
        merge_gap=merge_gap,
        min_duration=min_duration,
    )


def parse_blackdetect_segments(
    log: str | bytes | Iterable[str | bytes],
    *,
    duration: float | None = None,
    name: str = "黑场",
    tags: Any = ("black",),
    selected: bool = True,
    merge_gap: float = 0.0,
    min_duration: float = 0.0,
) -> list[CutSegment]:
    return ranges_to_cut_segments(
        parse_blackdetect(
            log,
            duration=duration,
            merge_gap=merge_gap,
            min_duration=min_duration,
        ),
        name=name,
        tags=tags,
        selected=selected,
    )


def parse_silencedetect_segments(
    log: str | bytes | Iterable[str | bytes],
    *,
    duration: float | None = None,
    name: str = "静音",
    tags: Any = ("silence",),
    selected: bool = True,
    merge_gap: float = 0.0,
    min_duration: float = 0.0,
) -> list[CutSegment]:
    return ranges_to_cut_segments(
        parse_silencedetect(
            log,
            duration=duration,
            merge_gap=merge_gap,
            min_duration=min_duration,
        ),
        name=name,
        tags=tags,
        selected=selected,
    )


def _event_score_key(event: SceneEvent) -> float:
    return -math.inf if event.score is None else event.score


def parse_scene_log(
    log: str | bytes | Iterable[str | bytes],
    *,
    duration: float | None = None,
    dedupe_tolerance: float = 1e-4,
) -> list[SceneEvent]:
    """Parse select/gt(scene), showinfo or scdet logs.

    Both the debug ``select`` output (``t:`` plus ``scene:``) and the
    ``select=...,showinfo`` output (selected frames only, no score) are
    accepted, as are ``scdet`` metadata lines.  Duplicated timestamps are
    collapsed, keeping the highest score.
    """

    limit = _validate_duration(duration)
    tolerance = _require_non_negative(dedupe_tolerance, "去重容差")
    lines = list(_iter_log_lines(log))
    if not lines:
        return []

    def line_has_score(line: str) -> bool:
        if not (_SCENE_FIELD_RE.search(line) or _SCD_SCORE_FIELD_RE.search(line)):
            return False
        return bool(
            _PTS_TIME_RE.search(line)
            or _SELECT_TIME_RE.search(line)
            or _SCD_TIME_RE.search(line)
        )

    has_score_field = any(line_has_score(line) for line in lines)
    events: list[SceneEvent] = []
    for line in lines:
        scene_field = _SCENE_FIELD_RE.search(line)
        scd_score_field = _SCD_SCORE_FIELD_RE.search(line)
        score: float | None = None
        if scene_field is not None:
            match = _SCENE_RE.search(line)
            if match is None:
                continue
            score = _finite(match.group(1))
            if score is None:
                continue
        elif scd_score_field is not None:
            match = _SCD_SCORE_RE.search(line)
            if match is None:
                continue
            score = _finite(match.group(1))
            if score is None:
                continue
        elif has_score_field:
            continue

        scd_time = _SCD_TIME_RE.search(line)
        if scd_time is not None:
            time_value = _finite(scd_time.group(1))
        else:
            pts_time = _PTS_TIME_RE.search(line)
            if pts_time is not None:
                time_value = _finite(pts_time.group(1))
            else:
                select_time = _SELECT_TIME_RE.search(line)
                time_value = _finite(select_time.group(1)) if select_time else None
        if time_value is None:
            continue
        if not has_score_field and not (
            _SHOWINFO_RE.search(line) or _SELECT_DEBUG_RE.search(line)
        ):
            continue
        if time_value < 0:
            time_value = 0.0
        if limit is not None and time_value > limit + tolerance:
            continue
        select_out = _SELECT_OUT_RE.search(line)
        if select_out is not None:
            output_index = _finite(select_out.group(1))
            if output_index is None or output_index < 0:
                continue

        frame_value: int | None = None
        frame_match = _FRAME_RE.search(line)
        if frame_match is not None:
            frame_number = _finite(frame_match.group(1))
            if frame_number is not None and frame_number >= 0:
                frame_value = int(round(frame_number))
        events.append(SceneEvent(time_value, score, frame_value))

    events.sort(key=lambda event: (event.time, -_event_score_key(event)))
    deduped: list[SceneEvent] = []
    for event in events:
        if deduped and abs(event.time - deduped[-1].time) <= tolerance:
            previous = deduped[-1]
            if _event_score_key(event) > _event_score_key(previous):
                frame = event.frame if event.frame is not None else previous.frame
                deduped[-1] = SceneEvent(event.time, event.score, frame)
            elif previous.frame is None and event.frame is not None:
                deduped[-1] = SceneEvent(previous.time, previous.score, event.frame)
        else:
            deduped.append(event)
    return deduped


def parse_scene_cuts(
    log: str | bytes | Iterable[str | bytes],
    *,
    duration: float | None = None,
    dedupe_tolerance: float = 1e-4,
) -> list[float]:
    return [
        event.time
        for event in parse_scene_log(
            log,
            duration=duration,
            dedupe_tolerance=dedupe_tolerance,
        )
    ]


def _coerce_cut_times(
    cuts: Iterable[Any] | str | bytes,
    *,
    duration: float | None,
    dedupe_tolerance: float,
) -> list[float]:
    limit = _validate_duration(duration)
    tolerance = _require_non_negative(dedupe_tolerance, "去重容差")
    if isinstance(cuts, (str, bytes, bytearray)):
        values: Iterable[Any] = parse_scene_cuts(
            cuts,
            duration=limit,
            dedupe_tolerance=tolerance,
        )
    elif isinstance(cuts, (int, float)) and not isinstance(cuts, bool):
        values = (cuts,)
    elif isinstance(cuts, SceneEvent) or (
        isinstance(cuts, Mapping) and "time" in cuts
    ):
        values = (cuts,)
    else:
        values = cuts or ()
    collected: list[float] = []
    for value in values:
        if isinstance(value, SceneEvent):
            time_value = value.time
        elif isinstance(value, Mapping):
            time_value = _finite(value.get("time"))
        else:
            time_value = _finite(getattr(value, "time", value))
        if time_value is None:
            continue
        if time_value < 0:
            time_value = 0.0
        if limit is not None and time_value > limit + tolerance:
            continue
        collected.append(time_value)
    collected.sort()
    result: list[float] = []
    for value in collected:
        if not result or value - result[-1] > tolerance:
            result.append(value)
    return result


def scene_ranges_from_cuts(
    cuts: Iterable[Any] | str | bytes,
    duration: float,
    *,
    min_duration: float = 0.0,
    dedupe_tolerance: float = 1e-4,
) -> list[TimeRange]:
    """Build full-coverage shot ranges from scene cut times."""

    limit = _require_positive(duration, "素材时长")
    minimum = _require_non_negative(min_duration, "最短片段时长")
    cut_times = _coerce_cut_times(
        cuts,
        duration=limit,
        dedupe_tolerance=dedupe_tolerance,
    )
    boundaries = [0.0]
    for cut in cut_times:
        if cut <= boundaries[-1] + _EPSILON:
            continue
        if limit - cut <= _EPSILON:
            continue
        if minimum and cut - boundaries[-1] < minimum - _EPSILON:
            continue
        if minimum and limit - cut < minimum - _EPSILON:
            continue
        boundaries.append(cut)
    boundaries.append(limit)
    return [
        TimeRange(start, end)
        for start, end in zip(boundaries, boundaries[1:])
        if end > start + _EPSILON
    ]


scene_segments_from_cuts = scene_ranges_from_cuts


def scene_cut_segments(
    cuts: Iterable[Any] | str | bytes,
    duration: float,
    *,
    name: str = "场景",
    tags: Any = ("scene",),
    selected: bool = True,
    min_duration: float = 0.0,
    dedupe_tolerance: float = 1e-4,
) -> list[CutSegment]:
    return ranges_to_cut_segments(
        scene_ranges_from_cuts(
            cuts,
            duration,
            min_duration=min_duration,
            dedupe_tolerance=dedupe_tolerance,
        ),
        name=name,
        tags=tags,
        selected=selected,
        merge_touching=False,
    )


def _append_common_input(command: list[str], source: str, start: Any, duration: Any) -> float | None:
    source_text = str(source or "").strip()
    if not source_text:
        raise ValueError("素材路径不能为空。")
    start_value = None if start is None else _require_non_negative(start, "起始时间")
    duration_value = None if duration is None else _require_positive(duration, "检测时长")
    if start_value is not None and start_value > 0:
        command += ["-ss", f"{start_value:.6f}"]
    command += ["-i", source_text]
    if duration_value is not None:
        command += ["-t", f"{duration_value:.6f}"]
    return start_value


def _append_null_output(
    command: list[str],
    *,
    video: bool,
    filter_flag: str,
    expression: str,
) -> list[str]:
    if video:
        command += ["-map", "0:v:0", "-an", "-sn", "-dn"]
    else:
        command += ["-map", "0:a:0", "-vn", "-sn", "-dn"]
    command += [filter_flag, expression, "-f", "null", "-"]
    return command


def build_blackdetect_command(
    ffmpeg: str,
    source: str,
    *,
    min_duration: float = 0.3,
    picture_threshold: float = 0.98,
    pixel_threshold: float = 0.10,
    start: float | None = None,
    duration: float | None = None,
) -> list[str]:
    """Build a blackdetect command.

    ``picture_threshold`` is FFmpeg's ``pic_th`` (fraction of black pixels)
    and ``pixel_threshold`` is ``pix_th`` (luma value considered black).
    """

    minimum = _require_non_negative(min_duration, "黑场最短时长")
    picture = _require_finite(picture_threshold, "黑场画面比例")
    pixel = _require_finite(pixel_threshold, "黑场像素阈值")
    if not 0.0 <= picture <= 1.0:
        raise ValueError("黑场画面比例必须在 0–1 之间。")
    if not 0.0 <= pixel <= 1.0:
        raise ValueError("黑场像素阈值必须在 0–1 之间。")
    command = [_ffmpeg_text(ffmpeg), "-hide_banner", "-nostats", "-loglevel", "info"]
    _append_common_input(command, source, start, duration)
    expression = (
        f"blackdetect=d={minimum:g}:pix_th={pixel:g}:pic_th={picture:g}"
    )
    return _append_null_output(
        command,
        video=True,
        filter_flag="-vf",
        expression=expression,
    )


def build_silencedetect_command(
    ffmpeg: str,
    source: str,
    *,
    noise_db: float = -30.0,
    min_duration: float = 0.5,
    start: float | None = None,
    duration: float | None = None,
) -> list[str]:
    noise = _require_finite(noise_db, "静音噪声阈值")
    minimum = _require_non_negative(min_duration, "静音最短时长")
    command = [_ffmpeg_text(ffmpeg), "-hide_banner", "-nostats", "-loglevel", "info"]
    _append_common_input(command, source, start, duration)
    expression = f"silencedetect=noise={noise:g}dB:d={minimum:g}"
    return _append_null_output(
        command,
        video=False,
        filter_flag="-af",
        expression=expression,
    )


def build_scene_detect_command(
    ffmpeg: str,
    source: str,
    *,
    threshold: float = 0.4,
    method: str = "select",
    start: float | None = None,
    duration: float | None = None,
) -> list[str]:
    value = _require_finite(threshold, "场景阈值")
    if not 0.0 <= value <= 1.0:
        raise ValueError("场景阈值必须在 0–1 之间。")
    mode = str(method or "select").strip().lower()
    if mode == "select":
        expression = f"select='gt(scene,{value:g})',showinfo"
    elif mode == "scdet":
        expression = f"scdet=threshold={value * 100.0:g}"
    else:
        raise ValueError("场景检测方式必须是 select 或 scdet。")
    command = [_ffmpeg_text(ffmpeg), "-hide_banner", "-nostats", "-loglevel", "info"]
    _append_common_input(command, source, start, duration)
    return _append_null_output(
        command,
        video=True,
        filter_flag="-vf",
        expression=expression,
    )


def run_log_filter(
    command: Sequence[str],
    *,
    timeout: float | None = None,
    check: bool = True,
) -> str:
    """Run an FFmpeg filter command and return combined stdout/stderr."""

    if not command:
        raise ValueError("FFmpeg 命令不能为空。")
    completed = subprocess.run(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
    text = (completed.stdout or b"").decode("utf-8", errors="replace")
    if check and completed.returncode:
        tail = text[-4000:].strip()
        raise RuntimeError(
            f"FFmpeg 检测失败（退出码 {completed.returncode}）：{tail}"
        )
    return text


def detect_black(
    ffmpeg: str,
    source: str,
    *,
    min_duration: float = 0.3,
    picture_threshold: float = 0.98,
    pixel_threshold: float = 0.10,
    start: float | None = None,
    duration: float | None = None,
    parse_duration: float | None = None,
    merge_gap: float = 0.0,
    timeout: float | None = None,
) -> list[TimeRange]:
    """Run blackdetect and return ranges relative to the analysed clip.

    With ``start``/``duration`` the FFmpeg filter timestamps restart at zero,
    so the returned ranges are clip-relative.  Pass ``parse_duration`` when
    the clip length should also clamp the results.
    """

    command = build_blackdetect_command(
        ffmpeg,
        source,
        min_duration=min_duration,
        picture_threshold=picture_threshold,
        pixel_threshold=pixel_threshold,
        start=start,
        duration=duration,
    )
    log = run_log_filter(command, timeout=timeout)
    limit = parse_duration if parse_duration is not None else duration
    return parse_blackdetect(log, duration=limit, merge_gap=merge_gap)


def detect_silence(
    ffmpeg: str,
    source: str,
    *,
    noise_db: float = -30.0,
    min_duration: float = 0.5,
    start: float | None = None,
    duration: float | None = None,
    parse_duration: float | None = None,
    merge_gap: float = 0.0,
    timeout: float | None = None,
) -> list[TimeRange]:
    """Run silencedetect; returned times are relative to the analysed clip."""

    command = build_silencedetect_command(
        ffmpeg,
        source,
        noise_db=noise_db,
        min_duration=min_duration,
        start=start,
        duration=duration,
    )
    log = run_log_filter(command, timeout=timeout)
    limit = parse_duration if parse_duration is not None else duration
    return parse_silencedetect(log, duration=limit, merge_gap=merge_gap)


def detect_scenes(
    ffmpeg: str,
    source: str,
    *,
    threshold: float = 0.4,
    method: str = "select",
    start: float | None = None,
    duration: float | None = None,
    parse_duration: float | None = None,
    timeout: float | None = None,
) -> list[SceneEvent]:
    """Run scene detection; returned times are relative to the analysed clip."""

    command = build_scene_detect_command(
        ffmpeg,
        source,
        threshold=threshold,
        method=method,
        start=start,
        duration=duration,
    )
    log = run_log_filter(command, timeout=timeout)
    limit = parse_duration if parse_duration is not None else duration
    return parse_scene_log(log, duration=limit)


def detect_scene_cuts(
    ffmpeg: str,
    source: str,
    *,
    threshold: float = 0.4,
    method: str = "select",
    start: float | None = None,
    duration: float | None = None,
    parse_duration: float | None = None,
    timeout: float | None = None,
) -> list[float]:
    return [
        event.time
        for event in detect_scenes(
            ffmpeg,
            source,
            threshold=threshold,
            method=method,
            start=start,
            duration=duration,
            parse_duration=parse_duration,
            timeout=timeout,
        )
    ]


def timeline_thumbnail_times(start: float, end: float, count: int) -> list[float]:
    """Evenly spaced thumbnail timestamps (bin centres) for ``[start, end]``."""

    start_value = _require_non_negative(start, "缩略图起点")
    end_value = _require_finite(end, "缩略图终点")
    if end_value <= start_value + _EPSILON:
        raise ValueError("缩略图终点必须晚于起点。")
    amount = _require_int(count, "缩略图数量")
    step = (end_value - start_value) / amount
    return [start_value + (index + 0.5) * step for index in range(amount)]


def _thumbnail_height(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("缩略图高度必须是整数。")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("缩略图高度必须是整数。")
    try:
        height = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("缩略图高度必须是整数。") from exc
    if height == 0 or height < -2:
        raise ValueError("缩略图高度必须是 -2、-1 或正整数。")
    return height


def build_thumbnail_command(
    ffmpeg: str,
    source: str,
    timestamp: float,
    output: str,
    *,
    width: int = 160,
    height: int = -2,
    quality: int = 5,
    accurate: bool = True,
) -> list[str]:
    """Build a single lightweight thumbnail extraction command.

    ``-ss`` is placed before ``-i`` so the decoder seeks to the nearest
    keyframe instead of decoding the whole file.  ``accurate=False`` adds
    ``-noaccurate_seek`` for the fastest (keyframe-only) path.
    """

    time_value = _require_non_negative(timestamp, "缩略图时间")
    width_value = _require_int(width, "缩略图宽度")
    height_value = _thumbnail_height(height)
    quality_value = _require_int(quality, "缩略图质量")
    if not 1 <= quality_value <= 31:
        raise ValueError("缩略图质量必须在 1–31 之间。")
    source_text = str(source or "").strip()
    output_text = str(output or "").strip()
    if not source_text or not output_text:
        raise ValueError("缩略图输入和输出路径不能为空。")
    command = [
        _ffmpeg_text(ffmpeg),
        "-hide_banner",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{time_value:.6f}",
    ]
    if not accurate:
        command += ["-noaccurate_seek"]
    command += [
        "-i",
        source_text,
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-vf",
        f"scale={width_value}:{height_value}",
        "-q:v",
        str(quality_value),
        output_text,
    ]
    return command


def build_timeline_thumbnail_commands(
    ffmpeg: str,
    source: str,
    times: Iterable[float],
    output_dir: str | Path,
    *,
    prefix: str = "thumb",
    extension: str = ".jpg",
    width: int = 160,
    height: int = -2,
    quality: int = 5,
    accurate: bool = True,
) -> list[tuple[float, str, list[str]]]:
    """Build one thumbnail command per timestamp for a timeline strip."""

    directory = Path(output_dir)
    suffix_text = str(extension or ".jpg")
    suffix = suffix_text if suffix_text.startswith(".") else f".{suffix_text}"
    name_prefix = str(prefix or "thumb")
    result: list[tuple[float, str, list[str]]] = []
    for index, timestamp in enumerate(times, start=1):
        time_value = _require_non_negative(timestamp, "缩略图时间")
        output = str(directory / f"{name_prefix}_{index:04d}{suffix}")
        command = build_thumbnail_command(
            ffmpeg,
            source,
            time_value,
            output,
            width=width,
            height=height,
            quality=quality,
            accurate=accurate,
        )
        result.append((time_value, output, command))
    return result


def build_timeline_thumbnail_sheet_command(
    ffmpeg: str,
    source: str,
    output: str,
    *,
    start: float = 0.0,
    end: float | None = None,
    count: int = 25,
    columns: int = 5,
    width: int = 160,
    height: int = -2,
    quality: int = 4,
) -> list[str]:
    """Build a one-pass contact sheet command for a timeline."""

    start_value = _require_non_negative(start, "缩略图表起点")
    if end is None:
        raise ValueError("缩略图表必须提供终点。")
    end_value = _require_finite(end, "缩略图表终点")
    if end_value <= start_value + _EPSILON:
        raise ValueError("缩略图表终点必须晚于起点。")
    amount = _require_int(count, "缩略图数量")
    column_count = _require_int(columns, "缩略图列数")
    width_value = _require_int(width, "缩略图宽度")
    height_value = _thumbnail_height(height)
    quality_value = _require_int(quality, "缩略图质量")
    if not 1 <= quality_value <= 31:
        raise ValueError("缩略图质量必须在 1–31 之间。")
    rows = math.ceil(amount / column_count)
    interval = (end_value - start_value) / amount
    if interval <= _EPSILON:
        raise ValueError("缩略图时间间隔必须大于零。")
    fps = 1.0 / interval
    expression = (
        f"fps={fps:.6f},scale={width_value}:{height_value},"
        f"tile={column_count}x{rows}"
    )
    source_text = str(source or "").strip()
    output_text = str(output or "").strip()
    if not source_text or not output_text:
        raise ValueError("缩略图表输入和输出路径不能为空。")
    return [
        _ffmpeg_text(ffmpeg),
        "-hide_banner",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{start_value:.6f}",
        "-t",
        f"{end_value - start_value:.6f}",
        "-i",
        source_text,
        "-map",
        "0:v:0",
        "-vf",
        expression,
        "-frames:v",
        "1",
        "-q:v",
        str(quality_value),
        output_text,
    ]


def build_waveform_command(
    ffmpeg: str,
    source: str,
    *,
    sample_rate: int = 2000,
    start: float | None = None,
    duration: float | None = None,
) -> list[str]:
    """Build a mono 16-bit little-endian PCM stream command for waveforms."""

    rate = _require_int(sample_rate, "波形采样率")
    source_text = str(source or "").strip()
    if not source_text:
        raise ValueError("素材路径不能为空。")
    start_value = None if start is None else _require_non_negative(start, "波形起点")
    duration_value = None if duration is None else _require_positive(duration, "波形时长")
    command = [_ffmpeg_text(ffmpeg), "-hide_banner", "-nostats", "-loglevel", "error"]
    if start_value is not None and start_value > 0:
        command += ["-ss", f"{start_value:.6f}"]
    command += ["-i", source_text]
    if duration_value is not None:
        command += ["-t", f"{duration_value:.6f}"]
    command += [
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        str(rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    return command


def _window_sample_count(sample_rate: int, window_seconds: float) -> int:
    return max(1, int(round(sample_rate * window_seconds)))


def _pcm_block_stats(block: array, include_rms: bool) -> tuple[float, float]:
    high = max(block)
    low = min(block)
    peak = float(max(high, -low))
    if include_rms:
        total = sum(value * value for value in block)
        rms = math.sqrt(total / len(block))
    else:
        rms = 0.0
    return peak, rms


def iter_peak_windows(
    chunks: Iterable[bytes | bytearray | memoryview],
    *,
    sample_rate: int = 2000,
    window_seconds: float = 0.05,
    channels: int = 1,
    start: float = 0.0,
    include_rms: bool = True,
) -> Iterator[PeakWindow]:
    """Stream raw ``s16le`` PCM chunks into peak windows.

    Chunks may split anywhere, including in the middle of a sample or an
    interleaved frame.  Memory use is bounded by one window plus the current
    chunk; a three-hour recording is processed in a single linear pass.
    """

    rate = _require_int(sample_rate, "波形采样率")
    window = _require_positive(window_seconds, "波形窗口")
    channel_count = _require_int(channels, "声道数")
    offset_start = _require_non_negative(start, "波形起点")
    per_window = _window_sample_count(rate, window)
    actual_window = per_window / rate
    window_samples = per_window * channel_count
    window_bytes = window_samples * 2
    buffer = bytearray()
    index = 0

    if isinstance(chunks, (bytes, bytearray, memoryview)):
        chunks = (bytes(chunks),)

    for chunk in chunks:
        if not chunk:
            continue
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("PCM 数据块必须是 bytes、bytearray 或 memoryview。")
        buffer.extend(chunk)
        offset = 0
        available = len(buffer)
        while available - offset >= window_bytes:
            raw = bytes(buffer[offset : offset + window_bytes])
            offset += window_bytes
            block = array("h")
            block.frombytes(raw)
            if sys.byteorder != "little":
                block.byteswap()
            peak, rms = _pcm_block_stats(block, include_rms)
            yield PeakWindow(
                index,
                offset_start + index * actual_window,
                actual_window,
                peak,
                rms,
            )
            index += 1
        if offset:
            del buffer[:offset]

    usable = len(buffer) - (len(buffer) % 2)
    if usable:
        raw = bytes(buffer[:usable])
        block = array("h")
        block.frombytes(raw)
        if sys.byteorder != "little":
            block.byteswap()
        frame_count = len(block) // channel_count
        if frame_count:
            if len(block) != frame_count * channel_count:
                block = block[: frame_count * channel_count]
            peak, rms = _pcm_block_stats(block, include_rms)
            yield PeakWindow(
                index,
                offset_start + index * actual_window,
                frame_count / rate,
                peak,
                rms,
            )


def waveform_from_pcm(
    chunks: Iterable[bytes | bytearray | memoryview] | bytes | bytearray | memoryview,
    *,
    sample_rate: int = 2000,
    window_seconds: float = 0.05,
    channels: int = 1,
    start: float = 0.0,
    include_rms: bool = True,
) -> Waveform:
    """Build a :class:`Waveform` from in-memory ``s16le`` PCM chunks."""

    rate = _require_int(sample_rate, "波形采样率")
    window = _require_positive(window_seconds, "波形窗口")
    channel_count = _require_int(channels, "声道数")
    actual_window = _window_sample_count(rate, window) / rate
    peaks: list[float] = []
    rms_values: list[float] = []
    duration = 0.0
    for item in iter_peak_windows(
        chunks,
        sample_rate=rate,
        window_seconds=window,
        channels=channel_count,
        start=start,
        include_rms=include_rms,
    ):
        peaks.append(item.peak)
        if include_rms:
            rms_values.append(item.rms)
        duration = item.end - start
    return Waveform(
        tuple(peaks),
        tuple(rms_values),
        actual_window,
        rate,
        start,
        max(0.0, duration),
    )


def pcm_peaks(
    pcm: bytes | bytearray | memoryview,
    *,
    sample_rate: int = 2000,
    window_seconds: float = 0.05,
    channels: int = 1,
    include_rms: bool = True,
) -> Waveform:
    return waveform_from_pcm(
        pcm,
        sample_rate=sample_rate,
        window_seconds=window_seconds,
        channels=channels,
        include_rms=include_rms,
    )


def sample_peaks(peaks: Sequence[float], max_points: int) -> list[float]:
    """Downsample a peak sequence by taking the maximum of each bucket."""

    limit = _require_int(max_points, "采样点数")
    values: list[float] = []
    for value in peaks:
        numeric = _require_finite(value, "波形峰值")
        if numeric < 0:
            raise ValueError("波形峰值不能小于零。")
        values.append(numeric)
    if len(values) <= limit:
        return values
    group = math.ceil(len(values) / limit)
    return [
        max(values[offset : offset + group])
        for offset in range(0, len(values), group)
    ]


def sample_waveform(waveform: Waveform, max_points: int) -> Waveform:
    return waveform.sample(max_points)


def _iter_stdout(stream: Any) -> Iterator[bytes]:
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        yield chunk


def generate_waveform(
    ffmpeg: str,
    source: str,
    *,
    sample_rate: int = 2000,
    window_seconds: float = 0.05,
    start: float | None = None,
    duration: float | None = None,
    include_rms: bool = True,
    max_points: int | None = None,
) -> Waveform:
    """Decode audio to mono PCM and stream a peak waveform.

    The decode is incremental, so even a three-hour file never occupies more
    than a small chunk of memory.  ``max_points`` optionally compresses the
    result for a lightweight UI strip.
    """

    rate = _require_int(sample_rate, "波形采样率")
    window = _require_positive(window_seconds, "波形窗口")
    offset = 0.0 if start is None else _require_non_negative(start, "波形起点")
    point_limit = None if max_points is None else _require_int(max_points, "采样点数")
    command = build_waveform_command(
        ffmpeg,
        source,
        sample_rate=rate,
        start=start,
        duration=duration,
    )
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
    )
    error_tail = bytearray()

    def drain_errors() -> None:
        stream = process.stderr
        if stream is None:
            return
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            error_tail.extend(chunk)
            if len(error_tail) > 32768:
                del error_tail[:-32768]

    error_thread = threading.Thread(target=drain_errors, daemon=True)
    error_thread.start()
    try:
        stdout = process.stdout
        if stdout is None:
            raise RuntimeError("无法读取 FFmpeg 音频输出。")
        waveform = waveform_from_pcm(
            _iter_stdout(stdout),
            sample_rate=rate,
            window_seconds=window,
            start=offset,
            include_rms=include_rms,
        )
        returncode = process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        error_thread.join(timeout=5.0)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    if returncode:
        detail = bytes(error_tail).decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"无法读取音频波形：{detail[-1200:]}")
    if point_limit is not None:
        waveform = waveform.sample(point_limit)
    return waveform


__all__ = [
    "CREATE_NO_WINDOW",
    "PCM_PEAK_SCALE",
    "CutSegment",
    "PeakWindow",
    "SceneEvent",
    "TimeRange",
    "Waveform",
    "build_blackdetect_command",
    "build_scene_detect_command",
    "build_silencedetect_command",
    "build_thumbnail_command",
    "build_timeline_thumbnail_commands",
    "build_timeline_thumbnail_sheet_command",
    "build_waveform_command",
    "coerce_cut_segment",
    "coerce_cut_segments",
    "cut_out_ranges",
    "cut_segments_by_black",
    "cut_segments_by_black_or_silence",
    "cut_segments_by_silence",
    "detect_black",
    "detect_scene_cuts",
    "detect_scenes",
    "detect_silence",
    "generate_waveform",
    "invert_ranges",
    "iter_peak_windows",
    "normalize_ranges",
    "parse_blackdetect",
    "parse_blackdetect_segments",
    "parse_scene_cuts",
    "parse_scene_log",
    "parse_silencedetect",
    "parse_silencedetect_segments",
    "pcm_peaks",
    "ranges_to_core_segments",
    "ranges_to_cut_segments",
    "remove_black_and_silence",
    "remove_black_ranges",
    "remove_silence_ranges",
    "run_log_filter",
    "sample_peaks",
    "sample_waveform",
    "scene_cut_segments",
    "scene_ranges_from_cuts",
    "scene_segments_from_cuts",
    "segments_from_ranges",
    "split_by_scene",
    "split_segments_by_scene",
    "subtract_ranges",
    "timeline_thumbnail_times",
    "to_core_segments",
    "waveform_from_pcm",
]
