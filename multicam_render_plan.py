"""Frame-exact render jobs without changing programme cuts or clip selection."""
from __future__ import annotations

import math
from dataclasses import replace
from fractions import Fraction
from typing import Iterable, Mapping, Protocol, TypeVar


class _Segment(Protocol):
    camera: int
    path: str | None
    source_start: float
    start: float
    duration: float
    frames: int


_T = TypeVar("_T", bound=_Segment)


def coalesce_render_segments(project, segments: Iterable[_T]) -> list[_T]:
    """Join equivalent export jobs while preserving all programme edits.

    This runs after transition planning and before optional parallel splitting.
    A shared resampling origin is mandatory: merely adjoining two recordings
    with the same nominal fps can select different source frames. The first
    job's origin and seek are retained; only its output length is extended.
    """
    from creative_core import grade_is_identity, grade_to_dict
    from creative_render import TransitionSegment
    from program_edits import grade_at

    fps = float(project.fps)
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("工程帧率必须是正数。")
    original = list(segments)
    if len(original) < 2: return original
    track = getattr(project, 'subtitles', None)
    if track is not None and track.enabled and track.burn_in and track.cues:
        # ASS rounds timestamps at its own millisecond clock. Replacing two
        # decimal PTS offsets by one can change a cue boundary by one frame.
        return original

    def frame(value): return math.floor(float(value)*fps+.5)

    def grade_key(grade):
        # Disabled/identity layers already bypass both current renderers.
        return None if grade is None or grade_is_identity(grade) else grade_to_dict(grade)

    def effect_key(segment):
        return (grade_key(project.camera_grades.get(segment.camera)),
                grade_key(grade_at(project, segment.start)), grade_key(project.global_grade))

    def stabilization_key(segment):
        resolver = getattr(project, 'stabilization_at', None)
        if not callable(resolver): return None
        clip = project.clip_at(segment.camera, segment.start)
        effect = resolver(segment.camera, segment.start, clip.id if clip is not None else '')
        if effect is None or str(effect.cache_path) != str(segment.path): return None
        return (effect.source_id, effect.cache_path, effect.signature, effect.source_start, effect.source_end)

    def compatible(left, right):
        if (type(left) is not type(right) or isinstance(left, TransitionSegment)
                or isinstance(right, TransitionSegment) or left.path is None or left.path != right.path
                or left.camera != right.camera or left.frames < 1 or right.frames < 1):
            return False
        # Only exact frame-clock jobs qualify; do not reinterpret an arbitrary
        # off-grid caller's timestamps while extending its duration.
        if (abs(left.start-frame(left.start)/fps) > 1e-7
                or abs(right.start-frame(right.start)/fps) > 1e-7
                or frame(left.start)+left.frames != frame(right.start)
                or abs(left.start+left.frames/fps-right.start) > 1e-7
                or f'{left.source_start+left.frames/fps:.9f}' != f'{right.source_start:.9f}'):
            return False
        left_origin = getattr(left, 'phase_origin', None)
        right_origin = getattr(right, 'phase_origin', None)
        left_origin = left.start if left_origin is None else left_origin
        right_origin = right.start if right_origin is None else right_origin
        if frame(left_origin) != frame(right_origin): return False
        return effect_key(left) == effect_key(right) and stabilization_key(left) == stabilization_key(right)

    result = []
    for segment in original:
        if result and compatible(result[-1], segment):
            previous = result[-1]
            count = previous.frames+segment.frames
            result[-1] = replace(previous, frames=count, duration=count/fps)
        else:
            result.append(segment)
    return result


def partition_render_segments(
    segments: Iterable[_T],
    fps: float | Fraction,
    workers: int,
    max_chunk_seconds: float = 60,
    *,
    source_frame_rates: Mapping[str, Fraction] | None = None,
) -> list[_T]:
    """Bound parallel-job lengths while retaining timeline/concat order.

    Positions always derive from the original segment and an integer frame
    offset. They never accumulate rounded chunk durations. ``replace`` keeps
    the caller's dataclass type and any additional fields, so this module does
    not need to import the renderer (which imports this planner).

    If ``source_frame_rates`` is supplied, camera sources absent from that
    verified-CFR mapping remain whole. Known sources split only at whole
    frame-rate-conversion periods, preserving the existing fps-filter phase.
    Black frames need no source-period constraint. ``None`` retains the older
    caller contract that every source is already safe to split.

    Single-worker exports and already-short segments retain their original
    objects.
    """
    if isinstance(fps, bool) or not isinstance(fps, (int, float, Fraction)) or not math.isfinite(fps) or fps <= 0:
        raise ValueError("工程帧率必须是正数。")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("并行编码数量必须是正整数。")
    if (isinstance(max_chunk_seconds, bool)
            or not isinstance(max_chunk_seconds, (int, float))
            or not math.isfinite(max_chunk_seconds) or max_chunk_seconds <= 0):
        raise ValueError("渲染分段时长必须是正数。")
    original = list(segments)
    if workers == 1:
        return original
    # At fractional rates, one frame may straddle the nominal time limit;
    # flooring keeps every multi-frame job within that limit. A minimum of one
    # frame also gives a meaningful job for sub-frame limits/very low rates.
    max_frames = max(1, math.floor(fps * max_chunk_seconds))
    output_rate = Fraction(fps).limit_denominator(100_000)
    clock_fps = float(fps)
    result: list[_T] = []
    for segment in original:
        if segment.frames <= max_frames:
            result.append(segment)
            continue
        quantum = 1
        if source_frame_rates is not None and segment.path is not None:
            source_rate = source_frame_rates.get(segment.path)
            if source_rate is None:
                result.append(segment)
                continue
            source_rate = Fraction(source_rate)
            if source_rate <= 0:
                raise ValueError("已验证的素材帧率必须是正数。")
            quantum = (output_rate / source_rate).numerator
        chunk_frames = max_frames // quantum * quantum
        if not chunk_frames:
            # A short artificial job would reset the frame-conversion phase;
            # retaining the original segment is preferable to moving frames.
            result.append(segment)
            continue
        for offset in range(0, segment.frames, chunk_frames):
            frames = min(chunk_frames, segment.frames - offset)
            seconds = offset / clock_fps
            result.append(replace(
                segment,
                source_start=segment.source_start + seconds,
                start=segment.start + seconds,
                duration=frames / clock_fps,
                frames=frames,
            ))
    return result
