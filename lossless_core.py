"""Dependency-free multi-segment model for lossless cutting.

The module is an original implementation of the operations commonly needed by
a LosslessCut-style editor: validated cut segments, marker points, timeline
editing, automatic splitting, undo/redo, keyframe snapping, chapter conversion
and playback planning.  It deliberately contains no ffmpeg calls; callers use
the returned time ranges to build their own cut/export commands.

All public time values are seconds.  A ``CutSegment`` whose ``end`` is ``None``
is a marker (a single point in time) instead of a cut range.
"""

from __future__ import annotations

import math
import uuid
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum

EPSILON = 1e-9
MAX_SPLIT_SEGMENTS = 100_000
DEFAULT_HISTORY_LIMIT = 200


def make_segment_id() -> str:
    """Return a short, collision-resistant identifier for a segment."""
    return uuid.uuid4().hex[:12]


def _number(value: object, label: str, *, allow_negative: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是数字。")
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须是数字。") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label}必须是有限数字。")
    if not allow_negative and result < 0:
        raise ValueError(f"{label}不能是负数。")
    return result


def _positive(value: object, label: str) -> float:
    result = _number(value, label)
    if result == 0:
        raise ValueError(f"{label}必须大于 0。")
    return result


def _integer(value: object, label: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是整数。")
    try:
        number = int(value)  # type: ignore[arg-type]
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须是整数。") from exc
    if not math.isfinite(numeric) or numeric != number:
        raise ValueError(f"{label}必须是整数。")
    if number < minimum:
        raise ValueError(f"{label}必须大于等于 {minimum}。")
    return number


def _name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("分段名称必须是字符串。")
    return value


def _tags(value: Mapping[str, str] | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("分段标签必须是键值对。")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError("分段标签的键和值都必须是字符串。")
        result[key] = item
    return result


def _selected(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("selected 必须是布尔值。")
    return value


def validate_time(
    value: object,
    *,
    label: str = "时间",
    allow_none: bool = False,
) -> float | None:
    """Validate seconds, rejecting negative, NaN and infinite values.

    ``allow_none`` is used for segment ends and chapter ends, where ``None``
    means a marker.
    """

    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{label}不能为空。")
    return _number(value, label)


def validate_segment(
    segment: CutSegment,
    *,
    media_duration: float | None = None,
) -> CutSegment:
    """Return a validated, normalised copy of *segment*.

    The copy normalises numeric inputs to ``float``, copies the tag mapping and
    enforces a strict ``0 <= start < end`` rule for cut ranges.
    """

    if not isinstance(segment, CutSegment):
        raise TypeError("segment 必须是 CutSegment。")

    start = _number(segment.start, "开始时间")
    end = validate_time(segment.end, label="结束时间", allow_none=True)
    if end is not None and end <= start:
        raise ValueError("结束时间必须大于开始时间；标记请将结束时间设为 None。")

    if not isinstance(segment.id, str) or not segment.id.strip():
        raise ValueError("分段 ID 必须是非空字符串。")

    result = CutSegment(
        id=segment.id,
        start=start,
        end=end,
        name=_name(segment.name),
        tags=_tags(segment.tags),
        selected=_selected(segment.selected),
    )

    if media_duration is not None:
        limit = _positive(media_duration, "媒体时长")
        if start > limit + EPSILON:
            raise ValueError("开始时间不能超过媒体时长。")
        if end is not None and end > limit + EPSILON:
            raise ValueError("结束时间不能超过媒体时长。")
    return result


def validate_segments(
    segments: Iterable[CutSegment],
    *,
    media_duration: float | None = None,
) -> tuple[CutSegment, ...]:
    """Validate every segment and reject duplicate identifiers."""

    if isinstance(segments, (str, bytes)):
        raise TypeError("segments 必须是 CutSegment 的可迭代对象。")
    result: list[CutSegment] = []
    seen: set[str] = set()
    for segment in segments:
        validated = validate_segment(segment, media_duration=media_duration)
        if validated.id in seen:
            raise ValueError(f"分段 ID 重复：{validated.id}。")
        seen.add(validated.id)
        result.append(validated)
    return tuple(result)


def clone_segment(segment: CutSegment, *, new_id: bool = False, **changes: object) -> CutSegment:
    """Copy a segment and its tags, optionally assigning a fresh identifier."""

    if not isinstance(segment, CutSegment):
        raise TypeError("segment 必须是 CutSegment。")
    values: dict[str, object] = {
        "id": segment.id,
        "start": segment.start,
        "end": segment.end,
        "name": segment.name,
        "tags": dict(segment.tags),
        "selected": segment.selected,
    }
    values.update(changes)
    if "tags" in changes:
        values["tags"] = _tags(changes["tags"])  # type: ignore[arg-type]
    if new_id:
        values["id"] = make_segment_id()
    return CutSegment(**values)  # type: ignore[arg-type]


def clone_segments(segments: Iterable[CutSegment]) -> tuple[CutSegment, ...]:
    return tuple(clone_segment(segment) for segment in segments)


def segment_sort_key(segment: CutSegment) -> tuple[float, int, float, str]:
    """Chronological key: markers come before clips that share their start."""
    start = float(segment.start)
    if segment.end is None:
        return (start, 0, start, segment.id)
    return (start, 1, float(segment.end), segment.id)


def sort_segments(segments: Iterable[CutSegment]) -> tuple[CutSegment, ...]:
    """Return segments in stable chronological order."""
    return tuple(sorted(segments, key=segment_sort_key))


def clip_segments(segments: Iterable[CutSegment]) -> tuple[CutSegment, ...]:
    """Return only real cut ranges, ignoring markers."""
    return tuple(segment for segment in segments if segment.end is not None)


def total_clip_duration(segments: Iterable[CutSegment]) -> float:
    """Sum the durations of all cut ranges (markers have duration 0)."""
    return sum(segment.duration for segment in segments)


def timeline_span(segments: Iterable[CutSegment]) -> tuple[float, float] | None:
    """Return the first and last clip times, or ``None`` when there are no clips."""
    clips = clip_segments(segments)
    if not clips:
        return None
    return (
        min(segment.start for segment in clips),
        max(segment.end for segment in clips if segment.end is not None),
    )


@dataclass(slots=True)
class CutSegment:
    """One cut range or marker on the source timeline.

    ``id`` is stable across edits, ``end=None`` marks a point marker, and
    ``tags`` stores free-form string metadata for other modules.  Pass
    ``start`` and ``end`` by keyword so the generated id cannot be confused
    with the first positional argument.
    """

    id: str = field(default_factory=make_segment_id)
    start: float = 0.0
    end: float | None = None
    name: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    selected: bool = False

    @property
    def is_marker(self) -> bool:
        return self.end is None

    @property
    def duration(self) -> float:
        if self.end is None:
            return 0.0
        return max(0.0, float(self.end) - float(self.start))

    def clone(self, *, new_id: bool = False, **changes: object) -> CutSegment:
        return clone_segment(self, new_id=new_id, **changes)


@dataclass(frozen=True, slots=True)
class SegmentOverlap:
    first_id: str
    second_id: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def find_overlaps(segments: Iterable[CutSegment]) -> tuple[SegmentOverlap, ...]:
    """Return every pair of cut ranges whose interiors overlap.

    Touching ranges (one ends exactly where the next starts) are not an
    overlap.  Markers are ignored.  Input order is irrelevant.
    """

    validated = validate_segments(segments)
    clips = sorted(clip_segments(validated), key=segment_sort_key)
    active: list[CutSegment] = []
    overlaps: list[SegmentOverlap] = []
    for segment in clips:
        active = [
            other
            for other in active
            if other.end is not None and other.end > segment.start + EPSILON
        ]
        for other in active:
            start = max(other.start, segment.start)
            end = min(other.end, segment.end)  # type: ignore[type-var]
            if end - start > EPSILON:
                overlaps.append(
                    SegmentOverlap(
                        first_id=other.id,
                        second_id=segment.id,
                        start=start,
                        end=end,
                    )
                )
        active.append(segment)
    overlaps.sort(key=lambda item: (item.start, item.end, item.first_id, item.second_id))
    return tuple(overlaps)


def has_overlaps(segments: Iterable[CutSegment]) -> bool:
    return bool(find_overlaps(segments))


def _merge_overlapping(
    validated: tuple[CutSegment, ...],
    *,
    join_adjacent: bool,
) -> tuple[CutSegment, ...]:
    clips = sorted(clip_segments(validated), key=segment_sort_key)
    merged: list[CutSegment] = []
    current: CutSegment | None = None
    for segment in clips:
        if current is None:
            current = clone_segment(segment)
            continue
        current_end = current.end if current.end is not None else current.start
        joins = (
            segment.start <= current_end + EPSILON
            if join_adjacent
            else segment.start < current_end - EPSILON
        )
        if joins:
            new_end = max(current_end, segment.end if segment.end is not None else segment.start)
            current = CutSegment(
                id=current.id,
                start=current.start,
                end=new_end,
                name=current.name or segment.name,
                tags={**current.tags, **segment.tags},
                selected=current.selected or segment.selected,
            )
        else:
            merged.append(current)
            current = clone_segment(segment)
    if current is not None:
        merged.append(current)

    markers = [clone_segment(segment) for segment in validated if segment.is_marker]
    return sort_segments([*merged, *markers])


def merge_overlapping_segments(
    segments: Iterable[CutSegment],
    *,
    join_adjacent: bool = True,
) -> tuple[CutSegment, ...]:
    """Merge overlapping (and optionally touching) ranges into unions.

    The first segment in chronological order keeps its id, name and tags;
    later tags override duplicate keys.  Markers are preserved unchanged.
    """

    validated = validate_segments(segments)
    return _merge_overlapping(validated, join_adjacent=join_adjacent)


@dataclass(frozen=True, slots=True)
class TimeGap:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def find_gaps(
    segments: Iterable[CutSegment],
    media_duration: float,
) -> tuple[TimeGap, ...]:
    """Return uncovered ranges inside ``[0, media_duration]``."""

    duration = _positive(media_duration, "媒体时长")
    validated = validate_segments(segments, media_duration=duration)
    merged = _merge_overlapping(validated, join_adjacent=True)
    gaps: list[TimeGap] = []
    cursor = 0.0
    for segment in clip_segments(merged):
        if segment.start > cursor + EPSILON:
            gaps.append(TimeGap(cursor, segment.start))
        if segment.end is not None:
            cursor = max(cursor, segment.end)
    if duration > cursor + EPSILON:
        gaps.append(TimeGap(cursor, duration))
    return tuple(gaps)


def gap_segments(
    segments: Iterable[CutSegment],
    media_duration: float,
    *,
    name: str = "",
    tags: Mapping[str, str] | None = None,
    selected: bool = False,
) -> tuple[CutSegment, ...]:
    """Build cut segments for every uncovered range."""

    checked_name = _name(name)
    checked_tags = _tags(tags)
    checked_selected = _selected(selected)
    return tuple(
        CutSegment(
            start=gap.start,
            end=gap.end,
            name=checked_name,
            tags=dict(checked_tags),
            selected=checked_selected,
        )
        for gap in find_gaps(segments, media_duration)
    )


def invert_segments(
    segments: Iterable[CutSegment],
    media_duration: float,
    *,
    name: str = "",
    tags: Mapping[str, str] | None = None,
    selected: bool = False,
    keep_markers: bool = False,
) -> tuple[CutSegment, ...]:
    """Return the complement of the supplied cut ranges (ad removal).

    Existing markers are dropped unless ``keep_markers`` is true.  The result
    contains new ids and is sorted chronologically.
    """

    duration = _positive(media_duration, "媒体时长")
    validated = validate_segments(segments, media_duration=duration)
    checked_name = _name(name)
    checked_tags = _tags(tags)
    checked_selected = _selected(selected)
    result = [
        CutSegment(
            start=gap.start,
            end=gap.end,
            name=checked_name,
            tags=dict(checked_tags),
            selected=checked_selected,
        )
        for gap in find_gaps(validated, duration)
    ]
    if keep_markers:
        result.extend(clone_segment(segment) for segment in validated if segment.is_marker)
    return sort_segments(result)


def _validated_range(start: object, end: object) -> tuple[float, float]:
    begin = _number(start, "开始时间")
    finish = _number(end, "结束时间")
    if finish <= begin:
        raise ValueError("结束时间必须大于开始时间。")
    return begin, finish


def _split_bounds(
    start: float,
    end: float,
    count: int,
) -> tuple[tuple[float, float], ...]:
    if count > MAX_SPLIT_SEGMENTS:
        raise ValueError(f"单个区间最多拆分为 {MAX_SPLIT_SEGMENTS} 段。")
    total = end - start
    bounds: list[tuple[float, float]] = []
    for index in range(count):
        piece_start = start + total * index / count
        piece_end = end if index == count - 1 else start + total * (index + 1) / count
        if piece_end <= piece_start:
            raise ValueError("区间太短，无法拆分为指定数量。")
        bounds.append((piece_start, piece_end))
    return tuple(bounds)


def split_range_by_count(
    start: float,
    end: float,
    count: int,
    *,
    name: str = "",
    tags: Mapping[str, str] | None = None,
    selected: bool = False,
) -> tuple[CutSegment, ...]:
    """Split one range into *count* equal pieces covering the whole range."""

    begin, finish = _validated_range(start, end)
    pieces = _integer(count, "分段数量")
    checked_name = _name(name)
    checked_tags = _tags(tags)
    checked_selected = _selected(selected)
    return tuple(
        CutSegment(
            start=piece_start,
            end=piece_end,
            name=checked_name,
            tags=dict(checked_tags),
            selected=checked_selected,
        )
        for piece_start, piece_end in _split_bounds(begin, finish, pieces)
    )


def split_range_by_duration(
    start: float,
    end: float,
    chunk_seconds: float,
    *,
    name: str = "",
    tags: Mapping[str, str] | None = None,
    selected: bool = False,
) -> tuple[CutSegment, ...]:
    """Split one range into chunks of at most ``chunk_seconds``.

    Only the final piece may be shorter.  This is the natural "cut every N
    minutes" operation.
    """

    begin, finish = _validated_range(start, end)
    chunk = _positive(chunk_seconds, "分段时长")
    total = finish - begin
    count = max(1, math.ceil(total / chunk - EPSILON))
    if count > MAX_SPLIT_SEGMENTS:
        raise ValueError(f"单个区间最多拆分为 {MAX_SPLIT_SEGMENTS} 段。")

    checked_name = _name(name)
    checked_tags = _tags(tags)
    checked_selected = _selected(selected)
    result: list[CutSegment] = []
    for index in range(count):
        piece_start = begin + chunk * index
        piece_end = finish if index == count - 1 else min(finish, piece_start + chunk)
        if piece_end - piece_start <= 0:
            continue
        result.append(
            CutSegment(
                start=piece_start,
                end=piece_end,
                name=checked_name,
                tags=dict(checked_tags),
                selected=checked_selected,
            )
        )
    return tuple(result)


def bytes_per_second_from_source(total_bytes: object, source_duration: object) -> float:
    """Estimate average bytes per second from a file size and duration."""

    size = _positive(total_bytes, "文件大小")
    duration = _positive(source_duration, "媒体时长")
    return size / duration


def split_range_by_size(
    start: float,
    end: float,
    max_bytes: object,
    bytes_per_second: object,
    *,
    name: str = "",
    tags: Mapping[str, str] | None = None,
    selected: bool = False,
) -> tuple[CutSegment, ...]:
    """Split a range so every estimated piece is at most ``max_bytes``."""

    max_size = _positive(max_bytes, "单段大小")
    rate = _positive(bytes_per_second, "平均码率")
    chunk_seconds = max_size / rate
    return split_range_by_duration(
        start,
        end,
        chunk_seconds,
        name=name,
        tags=tags,
        selected=selected,
    )


def estimate_size(segments: Iterable[CutSegment], bytes_per_second: object) -> int:
    """Estimate the encoded size of the selected ranges in bytes."""

    rate = _positive(bytes_per_second, "平均码率")
    validated = validate_segments(segments)
    return int(round(total_clip_duration(validated) * rate))


def estimate_size_from_source(
    segments: Iterable[CutSegment],
    total_bytes: object,
    source_duration: object,
) -> int:
    """Estimate size using an average rate derived from the whole source file."""

    rate = bytes_per_second_from_source(total_bytes, source_duration)
    return estimate_size(segments, rate)


@dataclass(frozen=True, slots=True)
class Chapter:
    """A chapter entry; ``end=None`` means the end is inferred."""

    start: float
    end: float | None = None
    title: str = ""


def chapters_to_segments(
    chapters: Iterable[Chapter],
    *,
    media_duration: float | None = None,
    selected: bool = False,
) -> tuple[CutSegment, ...]:
    """Convert chapters to cut segments, inferring missing chapter ends.

    A missing end is filled from the next chapter start, then from
    ``media_duration``.  If neither exists the final chapter becomes a marker.
    Overlapping explicit chapter ranges are rejected.
    """

    entries: list[tuple[float, float | None, str]] = []
    for chapter in chapters:
        if not isinstance(chapter, Chapter):
            raise TypeError("chapter 必须是 Chapter。")
        start = _number(chapter.start, "章节开始时间")
        end = validate_time(chapter.end, label="章节结束时间", allow_none=True)
        title = _name(chapter.title)
        if end is not None and end <= start:
            raise ValueError("章节结束时间必须大于开始时间。")
        entries.append((start, end, title))
    entries.sort(key=lambda item: (item[0], item[1] if item[1] is not None else math.inf))

    duration = _positive(media_duration, "媒体时长") if media_duration is not None else None
    resolved: list[tuple[float, float | None, str]] = []
    for index, (start, end, title) in enumerate(entries):
        if duration is not None and start > duration + EPSILON:
            raise ValueError("章节开始时间不能超过媒体时长。")
        if end is None:
            if index + 1 < len(entries):
                end = entries[index + 1][0]
            elif duration is not None:
                end = duration
        if index + 1 < len(entries):
            next_start = entries[index + 1][0]
            if end is None or end <= start:
                raise ValueError("章节时长必须大于 0。")
            if end > next_start + EPSILON:
                raise ValueError("章节不能重叠。")
        elif end is not None and end <= start:
            raise ValueError("章节时长必须大于 0。")
        resolved.append((start, end, title))

    checked_selected = _selected(selected)
    segments = [
        CutSegment(start=start, end=end, name=title, selected=checked_selected)
        for start, end, title in resolved
    ]
    return sort_segments(validate_segments(segments, media_duration=duration))


def segments_to_chapters(
    segments: Iterable[CutSegment],
    *,
    include_markers: bool = False,
) -> tuple[Chapter, ...]:
    """Convert cut segments to chapter entries."""

    validated = sort_segments(validate_segments(segments))
    chapters: list[Chapter] = []
    for segment in validated:
        if segment.is_marker and not include_markers:
            continue
        chapters.append(Chapter(start=segment.start, end=segment.end, title=segment.name))
    return tuple(chapters)


class SnapDirection(str, Enum):
    PREVIOUS = "previous"
    NEXT = "next"
    NEAREST = "nearest"


_SNAP_ALIASES: dict[str, SnapDirection] = {
    "previous": SnapDirection.PREVIOUS,
    "prev": SnapDirection.PREVIOUS,
    "before": SnapDirection.PREVIOUS,
    "earlier": SnapDirection.PREVIOUS,
    "前": SnapDirection.PREVIOUS,
    "next": SnapDirection.NEXT,
    "after": SnapDirection.NEXT,
    "later": SnapDirection.NEXT,
    "后": SnapDirection.NEXT,
    "nearest": SnapDirection.NEAREST,
    "near": SnapDirection.NEAREST,
    "closest": SnapDirection.NEAREST,
    "最近": SnapDirection.NEAREST,
}


def _snap_direction(value: SnapDirection | str) -> SnapDirection:
    if isinstance(value, SnapDirection):
        return value
    try:
        key = str(value).strip().lower()
    except Exception as exc:  # pragma: no cover - defensive
        raise ValueError("吸附方向必须是 previous、next 或 nearest。") from exc
    direction = _SNAP_ALIASES.get(key)
    if direction is None:
        raise ValueError("吸附方向必须是 previous、next 或 nearest。")
    return direction


def normalize_keyframes(keyframes: Iterable[object] | None) -> tuple[float, ...]:
    """Validate, sort and de-duplicate keyframe timestamps."""

    if keyframes is None:
        return ()
    if isinstance(keyframes, (str, bytes)):
        raise TypeError("keyframes 必须是时间值的可迭代对象。")
    values = sorted({_number(value, "关键帧时间") for value in keyframes})
    return tuple(values)


@dataclass(frozen=True, slots=True)
class KeyframeSnap:
    original: float
    snapped: float
    index: int | None
    direction: SnapDirection

    @property
    def moved(self) -> bool:
        return self.index is not None and abs(self.snapped - self.original) > EPSILON

    @property
    def offset(self) -> float:
        return self.snapped - self.original


class KeyframeIndex:
    """Bisect-backed keyframe lookup supporting previous/next/nearest."""

    def __init__(self, keyframes: Iterable[object] | None = None) -> None:
        self.times = normalize_keyframes(keyframes)

    def previous_index(self, time: object) -> int | None:
        value = _number(time, "时间", allow_negative=True)
        index = bisect_right(self.times, value) - 1
        return index if index >= 0 else None

    def next_index(self, time: object) -> int | None:
        value = _number(time, "时间", allow_negative=True)
        index = bisect_left(self.times, value)
        return index if index < len(self.times) else None

    def nearest_index(self, time: object) -> int | None:
        value = _number(time, "时间", allow_negative=True)
        if not self.times:
            return None
        right = bisect_left(self.times, value)
        if right == 0:
            return 0
        if right == len(self.times):
            return len(self.times) - 1
        left = right - 1
        if value - self.times[left] <= self.times[right] - value:
            return left
        return right

    def previous(self, time: object) -> float | None:
        index = self.previous_index(time)
        return None if index is None else self.times[index]

    def next(self, time: object) -> float | None:
        index = self.next_index(time)
        return None if index is None else self.times[index]

    def nearest(self, time: object) -> float | None:
        index = self.nearest_index(time)
        return None if index is None else self.times[index]

    def index_for(self, time: object, direction: SnapDirection | str) -> int | None:
        checked = _snap_direction(direction)
        if checked is SnapDirection.PREVIOUS:
            return self.previous_index(time)
        if checked is SnapDirection.NEXT:
            return self.next_index(time)
        return self.nearest_index(time)

    def snap(
        self,
        time: object,
        *,
        direction: SnapDirection | str = SnapDirection.NEAREST,
        tolerance: float | None = None,
    ) -> KeyframeSnap:
        value = _number(time, "时间", allow_negative=True)
        checked_direction = _snap_direction(direction)
        limit = None if tolerance is None else _number(tolerance, "吸附容差")
        index = self.index_for(value, checked_direction)
        if index is None:
            return KeyframeSnap(value, value, None, checked_direction)
        snapped = self.times[index]
        if limit is not None and abs(snapped - value) > limit + EPSILON:
            return KeyframeSnap(value, value, None, checked_direction)
        return KeyframeSnap(value, snapped, index, checked_direction)


def previous_keyframe(time: object, keyframes: Iterable[object] | None) -> float | None:
    return KeyframeIndex(keyframes).previous(time)


def next_keyframe(time: object, keyframes: Iterable[object] | None) -> float | None:
    return KeyframeIndex(keyframes).next(time)


def nearest_keyframe(time: object, keyframes: Iterable[object] | None) -> float | None:
    return KeyframeIndex(keyframes).nearest(time)


def snap_time_to_keyframe(
    time: object,
    keyframes: Iterable[object] | None,
    *,
    direction: SnapDirection | str = SnapDirection.NEAREST,
    tolerance: float | None = None,
) -> KeyframeSnap:
    return KeyframeIndex(keyframes).snap(time, direction=direction, tolerance=tolerance)


def snap_segment_to_keyframes(
    segment: CutSegment,
    keyframes: Iterable[object] | None,
    *,
    start_direction: SnapDirection | str = SnapDirection.NEAREST,
    end_direction: SnapDirection | str = SnapDirection.NEAREST,
    tolerance: float | None = None,
) -> CutSegment:
    """Snap segment boundaries to keyframes, keeping the segment id.

    Markers snap their single point using ``start_direction``.  A range whose
    boundaries collapse onto the same keyframe is rejected.
    """

    validated = validate_segment(segment)
    index = KeyframeIndex(keyframes)
    if validated.is_marker:
        snap = index.snap(validated.start, direction=start_direction, tolerance=tolerance)
        return clone_segment(validated, start=snap.snapped)

    start = index.snap(validated.start, direction=start_direction, tolerance=tolerance).snapped
    end = index.snap(validated.end, direction=end_direction, tolerance=tolerance).snapped
    if end <= start:
        raise ValueError("关键帧吸附后分段时长必须大于 0。")
    return clone_segment(validated, start=start, end=end)


class PlaybackMode(str, Enum):
    EMPTY = "empty"
    ONCE = "once"
    SINGLE_LOOP = "single_loop"
    SEQUENCE = "sequence"


@dataclass(frozen=True, slots=True)
class PlaybackItem:
    segment_id: str
    start: float
    end: float
    duration: float
    index: int


@dataclass(frozen=True, slots=True)
class PlaybackPlan:
    mode: PlaybackMode
    items: tuple[PlaybackItem, ...]
    loop: bool

    @property
    def is_empty(self) -> bool:
        return not self.items

    @property
    def total_duration(self) -> float:
        return sum(item.duration for item in self.items)

    def next_index(self, current_index: int) -> int | None:
        """Return the next item index, wrapping only when the plan loops."""
        if not self.items:
            return None
        if current_index < 0:
            return 0
        following = current_index + 1
        if following < len(self.items):
            return following
        return 0 if self.loop else None


def decide_playback(
    segments: Iterable[CutSegment],
    *,
    loop: bool = True,
    selected_only: bool = False,
) -> PlaybackPlan:
    """Choose single-segment loop, play-once or ordered multi-segment playback."""

    validated = sort_segments(validate_segments(segments))
    clips = list(clip_segments(validated))
    if selected_only:
        clips = [segment for segment in clips if segment.selected]

    if not clips:
        return PlaybackPlan(PlaybackMode.EMPTY, (), False)

    items = tuple(
        PlaybackItem(
            segment_id=segment.id,
            start=segment.start,
            end=segment.end if segment.end is not None else segment.start,
            duration=segment.duration,
            index=index,
        )
        for index, segment in enumerate(clips)
    )
    if len(items) == 1:
        mode = PlaybackMode.SINGLE_LOOP if loop else PlaybackMode.ONCE
    else:
        mode = PlaybackMode.SEQUENCE
    return PlaybackPlan(mode, items, bool(loop))


class HistoryError(RuntimeError):
    """Raised when undo/redo is requested without an available state."""


class SegmentHistory:
    """Snapshot-based undo/redo history for segment collections.

    Every snapshot is validated and deep-copied on entry and on exit, so
    callers cannot store invalid times or corrupt earlier states by mutating a
    returned segment or tag dictionary.
    """

    def __init__(
        self,
        initial: Iterable[CutSegment] = (),
        *,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> None:
        self._limit = _integer(limit, "历史记录数量")
        self._states: list[tuple[CutSegment, ...]] = [validate_segments(initial)]
        self._index = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def current(self) -> tuple[CutSegment, ...]:
        return clone_segments(self._states[self._index])

    @property
    def can_undo(self) -> bool:
        return self._index > 0

    @property
    def can_redo(self) -> bool:
        return self._index + 1 < len(self._states)

    def __len__(self) -> int:
        return len(self._states)

    def record(self, segments: Iterable[CutSegment]) -> bool:
        """Append a new state; return ``False`` when nothing changed."""

        snapshot = validate_segments(segments)
        if snapshot == self._states[self._index]:
            return False
        del self._states[self._index + 1 :]
        self._states.append(snapshot)
        if len(self._states) > self._limit:
            del self._states[: len(self._states) - self._limit]
        self._index = len(self._states) - 1
        return True

    def undo(self) -> tuple[CutSegment, ...]:
        if not self.can_undo:
            raise HistoryError("没有可撤销的操作。")
        self._index -= 1
        return self.current

    def redo(self) -> tuple[CutSegment, ...]:
        if not self.can_redo:
            raise HistoryError("没有可重做的操作。")
        self._index += 1
        return self.current

    def reset(self, segments: Iterable[CutSegment] = ()) -> None:
        """Replace the whole history with one new baseline state."""

        self._states = [validate_segments(segments)]
        self._index = 0


class SegmentModel:
    """Editable, history-tracked collection of cut segments and markers.

    Every mutating method validates its result, sorts chronologically and
    records an undo snapshot.  Methods that do not change the collection do
    not add a history entry.
    """

    def __init__(
        self,
        segments: Iterable[CutSegment] = (),
        *,
        media_duration: float | None = None,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> None:
        self._media_duration = (
            _positive(media_duration, "媒体时长") if media_duration is not None else None
        )
        initial = sort_segments(
            validate_segments(segments, media_duration=self._media_duration)
        )
        self._history = SegmentHistory(initial, limit=history_limit)

    @property
    def history(self) -> SegmentHistory:
        return self._history

    @property
    def media_duration(self) -> float | None:
        return self._media_duration

    @property
    def segments(self) -> tuple[CutSegment, ...]:
        return self._history.current

    @property
    def selected_segments(self) -> tuple[CutSegment, ...]:
        return tuple(segment for segment in self.segments if segment.selected)

    @property
    def selected_clips(self) -> tuple[CutSegment, ...]:
        return tuple(
            segment for segment in self.segments if segment.selected and not segment.is_marker
        )

    @property
    def total_duration(self) -> float:
        return total_clip_duration(self.segments)

    def __iter__(self):
        return iter(self.segments)

    def __len__(self) -> int:
        return len(self.segments)

    def get(self, segment_id: str) -> CutSegment:
        if not isinstance(segment_id, str):
            raise TypeError("分段 ID 必须是字符串。")
        for segment in self.segments:
            if segment.id == segment_id:
                return segment
        raise KeyError(f"找不到分段：{segment_id}。")

    def set_media_duration(self, media_duration: float) -> float:
        duration = _positive(media_duration, "媒体时长")
        for segment in self.segments:
            if segment.start > duration + EPSILON:
                raise ValueError("现有分段超出新的媒体时长。")
            if segment.end is not None and segment.end > duration + EPSILON:
                raise ValueError("现有分段超出新的媒体时长。")
        self._media_duration = duration
        return duration

    def _replace(self, segments: Iterable[CutSegment]) -> bool:
        normalized = sort_segments(
            validate_segments(segments, media_duration=self._media_duration)
        )
        return self._history.record(normalized)

    def _require_duration(self) -> float:
        if self._media_duration is None:
            raise ValueError("此操作需要先设置媒体时长。")
        return self._media_duration

    @staticmethod
    def _ids(value: str | Iterable[str]) -> list[str]:
        if isinstance(value, str):
            values = [value]
        else:
            try:
                values = list(value)
            except TypeError as exc:
                raise TypeError("分段 ID 必须是字符串或字符串序列。") from exc
        for segment_id in values:
            if not isinstance(segment_id, str):
                raise TypeError("分段 ID 必须是字符串。")
        return values

    def add(
        self,
        start: float,
        end: float | None = None,
        *,
        name: str = "",
        tags: Mapping[str, str] | None = None,
        selected: bool = False,
        segment_id: str | None = None,
    ) -> CutSegment:
        """Add a cut range, or a marker when ``end`` is ``None``."""

        segment = validate_segment(
            CutSegment(
                id=segment_id if segment_id is not None else make_segment_id(),
                start=start,
                end=end,
                name=name,
                tags=_tags(tags),
                selected=selected,
            ),
            media_duration=self._media_duration,
        )
        existing = self.segments
        if any(item.id == segment.id for item in existing):
            raise ValueError(f"分段 ID 重复：{segment.id}。")
        self._replace([*existing, segment])
        return segment

    def remove(self, segment_id: str) -> CutSegment:
        removed = self.remove_many([segment_id])
        return removed[0]

    def remove_many(
        self,
        segment_ids: str | Iterable[str],
        *,
        missing_ok: bool = False,
    ) -> tuple[CutSegment, ...]:
        requested = set(self._ids(segment_ids))
        existing_ids = {segment.id for segment in self.segments}
        missing = requested - existing_ids
        if missing and not missing_ok:
            raise KeyError(f"找不到分段：{sorted(missing)[0]}。")
        removed: list[CutSegment] = []
        kept: list[CutSegment] = []
        for segment in self.segments:
            if segment.id in requested:
                removed.append(segment)
            else:
                kept.append(segment)
        if removed:
            self._replace(kept)
        return tuple(removed)

    def remove_selected(self) -> tuple[CutSegment, ...]:
        return self.remove_many([segment.id for segment in self.selected_segments])

    def clear(self, *, keep_markers: bool = False) -> int:
        """Remove all segments; optionally keep point markers."""
        current = self.segments
        kept = [segment for segment in current if keep_markers and segment.is_marker]
        removed = len(current) - len(kept)
        if removed:
            self._replace(kept)
        return removed

    def split(self, segment_id: str, at: float) -> tuple[CutSegment, CutSegment]:
        """Split one cut range at ``start < at < end``."""
        segment = self.get(segment_id)
        if segment.end is None:
            raise ValueError("标记不能拆分。")
        split_time = _number(at, "拆分时间")
        if split_time <= segment.start or split_time >= segment.end:
            raise ValueError("拆分时间必须严格位于分段内部。")
        left = clone_segment(segment, end=split_time)
        right = clone_segment(
            segment,
            new_id=True,
            start=split_time,
            end=segment.end,
        )
        self._replace([*[s for s in self.segments if s.id != segment_id], left, right])
        return left, right

    def duplicate(
        self,
        segment_id: str,
        *,
        offset: float = 0.0,
        clamp: bool = False,
        new_id: bool = True,
    ) -> CutSegment:
        """Copy a segment, shifted by ``offset`` seconds."""
        segment = self.get(segment_id)
        shift = _number(offset, "偏移量", allow_negative=True)
        start = segment.start + shift
        end = None if segment.end is None else segment.end + shift
        if start < 0:
            if not clamp:
                raise ValueError("复制后的开始时间不能是负数。")
            start = 0.0
            if end is not None:
                end = segment.duration
        if self._media_duration is not None:
            if end is not None and end > self._media_duration + EPSILON:
                if not clamp:
                    raise ValueError("复制后的结束时间不能超过媒体时长。")
                start = max(0.0, self._media_duration - segment.duration)
                end = self._media_duration
            elif end is None and start > self._media_duration:
                if not clamp:
                    raise ValueError("复制后的时间不能超过媒体时长。")
                start = self._media_duration
        duplicate = clone_segment(
            segment,
            new_id=new_id,
            start=start,
            end=end,
            tags=dict(segment.tags),
        )
        self._replace([*self.segments, duplicate])
        return duplicate

    def merge_selected(self) -> CutSegment:
        """Merge all selected cut ranges into one, including internal gaps."""
        selected = sorted(self.selected_clips, key=segment_sort_key)
        if not selected:
            raise ValueError("没有选中可合并的分段。")
        if len(selected) == 1:
            return selected[0]

        tags: dict[str, str] = {}
        for segment in selected:
            tags.update(segment.tags)
        merged = CutSegment(
            id=selected[0].id,
            start=selected[0].start,
            end=max(segment.end for segment in selected if segment.end is not None),
            name=next((segment.name for segment in selected if segment.name), ""),
            tags=tags,
            selected=True,
        )
        selected_ids = {segment.id for segment in selected}
        kept = [segment for segment in self.segments if segment.id not in selected_ids]
        self._replace([*kept, merged])
        return merged

    def merge_overlapping(self, *, join_adjacent: bool = True) -> tuple[CutSegment, ...]:
        """Merge overlapping ranges in place and return the new segment list."""
        self._replace(merge_overlapping_segments(self.segments, join_adjacent=join_adjacent))
        return self.segments

    def invert(
        self,
        *,
        selected_only: bool = False,
        keep_markers: bool = False,
        name: str = "",
        tags: Mapping[str, str] | None = None,
    ) -> tuple[CutSegment, ...]:
        """Replace segments with the uncovered ranges (remove ads workflow)."""
        duration = self._require_duration()
        source = self.segments
        if selected_only:
            selected = self.selected_clips
            if not selected:
                raise ValueError("没有选中可用于反转的分段。")
            source = selected
        result = invert_segments(
            source,
            duration,
            name=name,
            tags=tags,
            keep_markers=keep_markers and not selected_only,
        )
        if keep_markers and selected_only:
            result = sort_segments(
                [*result, *(segment for segment in self.segments if segment.is_marker)]
            )
        self._replace(result)
        return self.segments

    def fill_gaps(
        self,
        *,
        name: str = "",
        tags: Mapping[str, str] | None = None,
    ) -> tuple[CutSegment, ...]:
        """Add a cut segment for every uncovered range inside the media."""
        duration = self._require_duration()
        gaps = gap_segments(self.segments, duration, name=name, tags=tags)
        if gaps:
            self._replace([*self.segments, *gaps])
        return gaps

    def move(self, segment_id: str, new_start: float, *, clamp: bool = True) -> CutSegment:
        """Move a segment to a new start time, preserving its duration."""
        segment = self.get(segment_id)
        start = _number(new_start, "开始时间")
        duration = segment.duration
        if self._media_duration is not None:
            if segment.is_marker:
                maximum = self._media_duration
            else:
                maximum = self._media_duration - duration
                if maximum < 0:
                    raise ValueError("分段时长超过媒体时长。")
            if start > maximum:
                if not clamp:
                    raise ValueError("移动后的结束时间不能超过媒体时长。")
                start = maximum
        end = None if segment.is_marker else start + duration
        moved = clone_segment(segment, start=start, end=end)
        self._replace([*[s for s in self.segments if s.id != segment_id], moved])
        return moved

    def move_by(
        self,
        segment_id: str,
        offset: float,
        *,
        clamp: bool = False,
    ) -> CutSegment:
        """Shift a segment by a signed offset."""
        segment = self.get(segment_id)
        shift = _number(offset, "偏移量", allow_negative=True)
        target = segment.start + shift
        if target < 0:
            if not clamp:
                raise ValueError("移动后的开始时间不能是负数。")
            target = 0.0
        return self.move(segment_id, target, clamp=clamp)

    def sort(self) -> tuple[CutSegment, ...]:
        """Sort chronologically and record an undo step if order changed."""
        self._replace(self.segments)
        return self.segments

    def reorder(
        self,
        order: Iterable[str] | None = None,
        *,
        start_at: float = 0.0,
    ) -> tuple[CutSegment, ...]:
        """Lay clips back-to-back in the supplied id order.

        Every clip id must appear exactly once.  Markers keep their original
        timestamps.  This is useful for changing the playback/output order of
        discontinuous takes.
        """
        clips = sorted(clip_segments(self.segments), key=segment_sort_key)
        clip_ids = [segment.id for segment in clips]
        if order is None:
            ordered_ids = clip_ids
        else:
            ordered_ids = self._ids(order)
            if len(ordered_ids) != len(set(ordered_ids)):
                raise ValueError("重排列表包含重复的分段 ID。")
            if set(ordered_ids) != set(clip_ids):
                raise ValueError("重排列表必须恰好包含全部分段 ID。")
        begin = _number(start_at, "起始时间")
        if self._media_duration is not None and begin > self._media_duration:
            raise ValueError("起始时间不能超过媒体时长。")

        by_id = {segment.id: segment for segment in clips}
        result: list[CutSegment] = []
        cursor = begin
        for segment_id in ordered_ids:
            segment = by_id[segment_id]
            end = cursor + segment.duration
            result.append(clone_segment(segment, start=cursor, end=end))
            cursor = end
        if self._media_duration is not None and cursor > self._media_duration + EPSILON:
            raise ValueError("重排后的总时长超过媒体时长。")
        result.extend(segment for segment in self.segments if segment.is_marker)
        self._replace(result)
        return self.segments

    def _split_targets(self, selected_only: bool) -> tuple[CutSegment, ...]:
        targets = self.selected_clips if selected_only else clip_segments(self.segments)
        if selected_only and not targets:
            raise ValueError("没有选中可用于分段的分段。")
        return targets

    def _replace_targets(
        self,
        targets: tuple[CutSegment, ...],
        replacement: Mapping[str, tuple[CutSegment, ...]],
    ) -> None:
        target_ids = {segment.id for segment in targets}
        result: list[CutSegment] = []
        for segment in self.segments:
            if segment.id in target_ids:
                result.extend(replacement[segment.id])
            else:
                result.append(segment)
        self._replace(result)

    @staticmethod
    def _with_original_id(
        original: CutSegment,
        pieces: Iterable[CutSegment],
    ) -> tuple[CutSegment, ...]:
        result: list[CutSegment] = []
        for index, piece in enumerate(pieces):
            result.append(
                CutSegment(
                    id=original.id if index == 0 else make_segment_id(),
                    start=piece.start,
                    end=piece.end,
                    name=original.name,
                    tags=dict(original.tags),
                    selected=original.selected,
                )
            )
        return tuple(result)

    def split_all_by_duration(
        self,
        chunk_seconds: float,
        *,
        selected_only: bool = False,
    ) -> tuple[CutSegment, ...]:
        """Split every targeted clip into chunks of at most ``chunk_seconds``."""
        chunk = _positive(chunk_seconds, "分段时长")
        targets = self._split_targets(selected_only)
        replacement: dict[str, tuple[CutSegment, ...]] = {}
        for segment in targets:
            if segment.duration <= chunk + EPSILON:
                replacement[segment.id] = (clone_segment(segment),)
                continue
            pieces = split_range_by_duration(segment.start, segment.end, chunk)  # type: ignore[arg-type]
            replacement[segment.id] = self._with_original_id(segment, pieces)
        self._replace_targets(targets, replacement)
        return self.segments

    def split_all_by_count(
        self,
        count: int,
        *,
        selected_only: bool = False,
    ) -> tuple[CutSegment, ...]:
        """Split every targeted clip into ``count`` equal pieces."""
        pieces_count = _integer(count, "分段数量")
        targets = self._split_targets(selected_only)
        replacement: dict[str, tuple[CutSegment, ...]] = {}
        for segment in targets:
            if pieces_count == 1:
                replacement[segment.id] = (clone_segment(segment),)
                continue
            pieces = split_range_by_count(segment.start, segment.end, pieces_count)  # type: ignore[arg-type]
            replacement[segment.id] = self._with_original_id(segment, pieces)
        self._replace_targets(targets, replacement)
        return self.segments

    def split_all_by_size(
        self,
        max_bytes: object,
        *,
        bytes_per_second: object | None = None,
        total_bytes: object | None = None,
        selected_only: bool = False,
    ) -> tuple[CutSegment, ...]:
        """Split clips so each estimated piece is at most ``max_bytes``.

        Supply either an explicit average ``bytes_per_second`` or the source
        ``total_bytes``; the latter uses the model's media duration.
        """
        if bytes_per_second is not None:
            rate = _positive(bytes_per_second, "平均码率")
        elif total_bytes is not None:
            rate = bytes_per_second_from_source(total_bytes, self._require_duration())
        else:
            raise ValueError("估算大小分段需要字节码率或源文件总大小。")
        chunk = _positive(max_bytes, "单段大小") / rate
        return self.split_all_by_duration(chunk, selected_only=selected_only)

    def snap(
        self,
        segment_id: str,
        keyframes: Iterable[object] | None,
        *,
        start_direction: SnapDirection | str = SnapDirection.NEAREST,
        end_direction: SnapDirection | str = SnapDirection.NEAREST,
        tolerance: float | None = None,
    ) -> CutSegment:
        """Snap one segment's boundaries to keyframes and record the edit."""
        segment = self.get(segment_id)
        snapped = snap_segment_to_keyframes(
            segment,
            keyframes,
            start_direction=start_direction,
            end_direction=end_direction,
            tolerance=tolerance,
        )
        self._replace([*[s for s in self.segments if s.id != segment_id], snapped])
        return snapped

    def set_selected(
        self,
        segment_ids: str | Iterable[str],
        selected: bool,
    ) -> tuple[CutSegment, ...]:
        requested = set(self._ids(segment_ids))
        known = {segment.id for segment in self.segments}
        missing = requested - known
        if missing:
            raise KeyError(f"找不到分段：{sorted(missing)[0]}。")
        checked = _selected(selected)
        result: list[CutSegment] = []
        changed = False
        for segment in self.segments:
            if segment.id in requested and segment.selected != checked:
                segment = clone_segment(segment, selected=checked)
                changed = True
            result.append(segment)
        if changed:
            self._replace(result)
        return self.selected_segments

    def select(self, segment_ids: str | Iterable[str]) -> tuple[CutSegment, ...]:
        return self.set_selected(segment_ids, True)

    def deselect(self, segment_ids: str | Iterable[str]) -> tuple[CutSegment, ...]:
        return self.set_selected(segment_ids, False)

    def toggle_selected(self, segment_ids: str | Iterable[str]) -> tuple[CutSegment, ...]:
        requested = set(self._ids(segment_ids))
        known = {segment.id for segment in self.segments}
        missing = requested - known
        if missing:
            raise KeyError(f"找不到分段：{sorted(missing)[0]}。")
        result: list[CutSegment] = []
        changed = False
        for segment in self.segments:
            if segment.id in requested:
                segment = clone_segment(segment, selected=not segment.selected)
                changed = True
            result.append(segment)
        if changed:
            self._replace(result)
        return self.selected_segments

    def select_only(self, segment_ids: str | Iterable[str]) -> tuple[CutSegment, ...]:
        requested = set(self._ids(segment_ids))
        known = {segment.id for segment in self.segments}
        missing = requested - known
        if missing:
            raise KeyError(f"找不到分段：{sorted(missing)[0]}。")
        self._replace(
            [clone_segment(segment, selected=segment.id in requested) for segment in self.segments]
        )
        return self.selected_segments

    def select_all(self) -> tuple[CutSegment, ...]:
        return self.set_selected([segment.id for segment in self.segments], True)

    def clear_selection(self) -> None:
        self.set_selected([segment.id for segment in self.segments], False)

    def overlaps(self) -> tuple[SegmentOverlap, ...]:
        return find_overlaps(self.segments)

    def gaps(self) -> tuple[TimeGap, ...]:
        return find_gaps(self.segments, self._require_duration())

    def to_chapters(self, *, include_markers: bool = False) -> tuple[Chapter, ...]:
        return segments_to_chapters(self.segments, include_markers=include_markers)

    @classmethod
    def from_chapters(
        cls,
        chapters: Iterable[Chapter],
        *,
        media_duration: float | None = None,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> SegmentModel:
        segments = chapters_to_segments(chapters, media_duration=media_duration)
        return cls(
            segments,
            media_duration=media_duration,
            history_limit=history_limit,
        )

    def playback_plan(
        self,
        *,
        loop: bool = True,
        selected_only: bool = False,
    ) -> PlaybackPlan:
        return decide_playback(
            self.segments,
            loop=loop,
            selected_only=selected_only,
        )

    def estimated_size(self, bytes_per_second: object) -> int:
        return estimate_size(self.segments, bytes_per_second)

    def estimated_size_from_source(self, total_bytes: object) -> int:
        return estimate_size_from_source(
            self.segments,
            total_bytes,
            self._require_duration(),
        )

    def undo(self) -> tuple[CutSegment, ...]:
        return self._history.undo()

    def redo(self) -> tuple[CutSegment, ...]:
        return self._history.redo()

    @property
    def can_undo(self) -> bool:
        return self._history.can_undo

    @property
    def can_redo(self) -> bool:
        return self._history.can_redo


__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "EPSILON",
    "MAX_SPLIT_SEGMENTS",
    "Chapter",
    "CutSegment",
    "HistoryError",
    "KeyframeIndex",
    "KeyframeSnap",
    "PlaybackItem",
    "PlaybackMode",
    "PlaybackPlan",
    "SegmentHistory",
    "SegmentModel",
    "SegmentOverlap",
    "SnapDirection",
    "TimeGap",
    "bytes_per_second_from_source",
    "chapters_to_segments",
    "clip_segments",
    "clone_segment",
    "clone_segments",
    "decide_playback",
    "estimate_size",
    "estimate_size_from_source",
    "find_gaps",
    "find_overlaps",
    "gap_segments",
    "has_overlaps",
    "invert_segments",
    "make_segment_id",
    "merge_overlapping_segments",
    "nearest_keyframe",
    "next_keyframe",
    "normalize_keyframes",
    "previous_keyframe",
    "segment_sort_key",
    "segments_to_chapters",
    "snap_segment_to_keyframes",
    "snap_time_to_keyframe",
    "sort_segments",
    "split_range_by_count",
    "split_range_by_duration",
    "split_range_by_size",
    "timeline_span",
    "total_clip_duration",
    "validate_segment",
    "validate_segments",
    "validate_time",
]
