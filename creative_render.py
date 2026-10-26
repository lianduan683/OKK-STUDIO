"""Frame-exact local effect jobs without re-encoding the complete programme.

Ordinary jobs retain their input dataclass and the fast rendering path.  A
transition owns its full centred window; exporting only part of that window
trims the mixed result, preserving the original transition phase.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, replace
from fractions import Fraction
import math
from pathlib import Path

from creative_core import grade_filter, grade_is_identity, transition_filter, validate_transitions
from subtitle_core import subtitle_filter
from program_edits import grade_at


@dataclass(frozen=True, slots=True)
class TransitionSegment:
    camera: int
    path: str
    source_start: float
    start: float
    duration: float
    frames: int
    other_camera: int
    other_path: str
    other_source_start: float
    kind: str
    full_frames: int
    trim_start_frames: int
    full_start: float
    phase_origin: float | None = None

    @property
    def end(self) -> float:
        return self.start + self.duration


def _frame(seconds: float, fps: float) -> int:
    return math.floor(float(seconds) * fps + .5)


def _continuous_clip(project, camera: int, first: int, last: int):
    """Resolve the same quantized, latest-recording-wins clip as the timeline."""
    clips = [clip for clip in project.clips if clip.camera == camera]
    edges = {first, last}
    for clip in clips:
        edges.update(frame for frame in (_frame(clip.start, project.fps), _frame(clip.end, project.fps))
                     if first < frame < last)
    selected = None
    ordered = sorted(edges)
    for left, right in zip(ordered, ordered[1:]):
        candidates = [clip for clip in clips if _frame(clip.start, project.fps) <= left
                      and _frame(clip.end, project.fps) >= right]
        clip = max(candidates, key=lambda item: (item.start, item.id), default=None)
        if clip is None or (selected is not None and clip.id != selected.id):
            raise ValueError(f"机位 {camera} 的同一段原素材必须完整覆盖转场区间，请缩短转场或调整素材")
        selected = clip
    if selected is None:
        raise ValueError(f"机位 {camera} 没有完整覆盖转场区间的原素材")
    return selected


def plan_creative_segments(project, segments) -> list:
    original = list(segments)
    if not original or not project.transitions:
        return original
    validate_transitions(project.transitions, project)
    fps = float(project.fps)
    first = _frame(original[0].start, fps)
    last = _frame(original[-1].start, fps) + original[-1].frames
    windows = []
    mixed = []
    previous_end = -1
    for transition in sorted(project.transitions, key=lambda item: item.time):
        count = max(1, _frame(transition.duration, fps))
        cut = _frame(transition.time, fps)
        left = cut - count // 2
        right = left + count
        if left < previous_end:
            raise ValueError("转场在工程帧率下重叠，请缩短转场时长")
        if left < 0 or right > _frame(project.duration, fps):
            raise ValueError("转场区间超出工程范围")
        previous_end = right
        if right <= first or left >= last:
            continue
        edit_times = [item.time for item in project.cuts] + list(getattr(project, "edit_points", []))
        if any(left < _frame(time, fps) < right and _frame(time, fps) != cut
               for time in edit_times):
            raise ValueError("转场区间包含其他机位切点，请缩短转场时长")
        outgoing = project.camera_at((cut - 1) / fps)
        incoming = project.camera_at(cut / fps)
        if outgoing is None or incoming is None:
            raise ValueError("转场切点前后必须有可用机位")
        clip_a = _continuous_clip(project, outgoing, left, right)
        clip_b = _continuous_clip(project, incoming, left, right)
        keep_first, keep_last = max(first, left), min(last, right)
        keep_count = keep_last - keep_first
        full_start = left / fps
        # Stabilized selections contain temporal handles. Resolve each side
        # inside its own shot, then rewind to the complete centred window;
        # resolving both at the window's left edge would choose the outgoing
        # shot's treatment for the incoming half of a same-camera cut.
        def resolve_side(camera, clip, sample):
            resolver = getattr(project, 'media_at', None)
            if not callable(resolver):
                return clip.path, max(0.0, full_start-clip.start)
            media = resolver(camera, sample, use_proxies=False)
            if media is None:
                raise ValueError(f'机位 {camera} 的转场画面不可用')
            path, source_time, _ = media
            if str(path) == clip.path:
                return clip.path, max(0.0, full_start-clip.start)
            source_start = float(source_time) + full_start-sample
            if source_start < -1e-6:
                raise ValueError('防抖缓存未完整覆盖转场前余量，请重新准备防抖或缩短转场')
            return str(path), max(0.0, source_start)
        path_a, source_a = resolve_side(outgoing, clip_a, (cut-1)/fps)
        path_b, source_b = resolve_side(incoming, clip_b, cut/fps)
        phase_origin = None
        if outgoing == incoming:
            # A transition on a same-camera manual cut blends two versions
            # of the same continuing shot. Its fps conversion must therefore
            # retain that shot's original phase, including partial exports.
            complete = project.segments(start=0, end=project.duration)
            prior = next((item for item in complete
                          if _frame(item.start, fps) <= cut - 1 < _frame(item.end, fps)), None)
            if prior is not None:
                phase_origin = (prior.phase_origin if getattr(prior, "phase_origin", None) is not None
                                else prior.start)
        mixed.append(TransitionSegment(
            outgoing, path_a, source_a,
            keep_first / fps, keep_count / fps, keep_count,
            incoming, path_b, source_b,
            transition.kind, count, keep_first - left, full_start, phase_origin))
        windows.append((left, right))
    if not windows:
        return original
    normal = []
    ends = [right for _, right in windows]
    for segment in original:
        segment_first = _frame(segment.start, fps)
        segment_last = segment_first + segment.frames
        cursor = segment_first
        window_index = bisect_right(ends, cursor)
        while window_index < len(windows) and windows[window_index][0] < segment_last:
            left, right = windows[window_index]
            if left > cursor:
                count = min(left, segment_last) - cursor
                offset = (cursor - segment_first) / fps
                normal.append(replace(segment, source_start=segment.source_start + offset,
                                      start=cursor / fps, duration=count / fps, frames=count))
            cursor = max(cursor, right)
            window_index += 1
        if cursor < segment_last:
            count = segment_last - cursor
            offset = (cursor - segment_first) / fps
            normal.append(replace(segment, source_start=segment.source_start + offset,
                                  start=cursor / fps, duration=count / fps, frames=count))
    result = sorted(normal + mixed, key=lambda segment: segment.start)
    if sum(segment.frames for segment in result) != sum(segment.frames for segment in original):
        raise ValueError("转场规划与原时间线帧数不一致，请检查导出范围")
    return result


def has_pixel_effects(project, camera: int, time: float | None = None) -> bool:
    if not grade_is_identity(project.global_grade):
        return True
    if camera in project.camera_grades and not grade_is_identity(project.camera_grades[camera]):
        return True
    grades = ([grade_at(project, time)] if time is not None else
              [item.grade for item in getattr(project, "clip_grades", [])])
    if any(grade is not None and not grade_is_identity(grade) for grade in grades):
        return True
    track = project.subtitles
    return bool(track.enabled and track.burn_in and track.cues)


def normal_effect_filter(project, camera: int, segment_start: float, ass_path=None,
                         *, grade_time: float | None = None) -> str:
    chains = []
    if camera in project.camera_grades:
        chains.append(grade_filter(project.camera_grades[camera]))
    local = grade_at(project, segment_start if grade_time is None else grade_time)
    if local is not None:
        chains.append(grade_filter(local))
    chains.append(grade_filter(project.global_grade))
    if ass_path is not None:
        if not math.isfinite(segment_start):
            raise ValueError("字幕片段起点无效")
        chains.extend([f"setpts=PTS+{segment_start:.12g}/TB", subtitle_filter(ass_path), "setpts=PTS-STARTPTS"])
    return ",".join(chain for chain in chains if chain)


def build_transition_command(job: TransitionSegment, project, base, settings, width: int,
                             height: int, pixel_format: str, rate_text: str, encode_args,
                             chunk: str | Path, ass_path=None, *, source_frame_rates=None) -> list[str]:
    """Render only a short transition window with matching grade/subtitle order."""
    if (not isinstance(job, TransitionSegment) or job.frames < 1 or job.full_frames < 1
            or job.trim_start_frames < 0 or job.trim_start_frames + job.frames > job.full_frames):
        raise ValueError("转场渲染帧范围无效")
    rate = Fraction(rate_text)
    full_duration = job.full_frames / project.fps
    working_format = "yuv444p10le" if "10" in pixel_format else "yuv444p"
    prefix_frames = 0
    if job.phase_origin is not None and job.phase_origin < job.full_start:
        offset = _frame(job.full_start - job.phase_origin, project.fps)
        source_rate = (source_frame_rates or {}).get(job.path)
        quantum = (rate / source_rate).numerator if source_rate else None
        prefix_frames = offset % quantum if quantum else offset
    prefix_seconds = prefix_frames / project.fps
    command = list(base) + ["-filter_complex_threads", "1",
                           "-ss", f"{max(0, job.source_start - prefix_seconds):.9f}", "-i", job.path,
                           "-ss", f"{max(0, job.other_source_start - prefix_seconds):.9f}", "-i", job.other_path]
    trim = (f"trim=start_frame={prefix_frames}:end_frame={prefix_frames + job.full_frames}" if prefix_frames
            else f"trim=end_frame={job.full_frames}")
    normalize = (
        f"setpts=PTS-STARTPTS,fps=fps={rate_text}:start_time=0,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
        f"format={working_format},tpad=stop_mode=clone:stop_duration={full_duration + prefix_seconds + 1:.9f},"
        f"{trim},settb=AVTB,setpts=N/({rate_text}*TB)"
    )
    graph = []
    for index, camera in enumerate((job.camera, job.other_camera)):
        # The centred transition starts before the programme cut. Choose
        # each shot's own grade at the actual cut instead of sampling both
        # at that window start; partial exports retain the same two grades.
        cut_frame = _frame(job.full_start, project.fps) + job.full_frames // 2
        grade_time = (cut_frame - 1 if index == 0 else cut_frame) / project.fps
        grade = normal_effect_filter(project, camera, job.full_start, grade_time=grade_time)
        chain = normalize + ("," + grade if grade else "") + f",format={working_format}"
        graph.append(f"[{index}:v:0]{chain}[camera{index}]")
    mixed = (f"[camera0][camera1]xfade=transition={transition_filter(job.kind)}:"
             f"duration={full_duration:.9f}:offset=0,"
             f"trim=start_frame={job.trim_start_frames}:end_frame={job.trim_start_frames + job.frames},"
             f"setpts=N/({rate_text}*TB)")
    if ass_path is not None:
        mixed += (f",setpts=PTS+{job.start:.12g}/TB," + subtitle_filter(ass_path)
                  + ",setpts=PTS-STARTPTS")
    mixed += f",format={pixel_format}[vout]"
    graph.append(mixed)
    command += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-an", "-sn", "-dn"]
    command += list(encode_args)
    command += ["-pix_fmt", pixel_format, "-r", rate_text, "-frames:v", str(job.frames),
                "-map_metadata", "-1", "-video_track_timescale", str(rate.numerator), str(chunk)]
    return command
