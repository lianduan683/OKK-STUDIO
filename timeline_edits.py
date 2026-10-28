"""Atomic source-track edits on the unchanged multicamera master clock.

Callers own undo history.  Destructive edits validate a private copy before
publishing; movement checks the whole group before a lightweight in-place
update.  They never delete media/cache files or move master audio.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
import math

from video_stack import prune_stack
from program_edits import ClipGrade, grade_at, program_spans


def _frame(value, fps):
    return math.floor(value * fps + .5)


def _selected(project, clip_ids):
    if isinstance(clip_ids, (str, bytes)):
        raise ValueError('素材选择必须是 ID 列表。')
    identifiers = set(clip_ids)
    if any(not isinstance(value, str) or not value for value in identifiers):
        raise ValueError('素材 ID 无效。')
    clips = [clip for clip in project.clips if clip.id in identifiers]
    if identifiers - {clip.id for clip in clips}:
        raise ValueError('选中的素材已不存在，请重新选择。')
    return identifiers, clips


def _publish(project, candidate):
    candidate.validate()
    for field in fields(candidate):
        setattr(project, field.name, getattr(candidate, field.name))


def _trim_project_edges(project):
    """Keep dependent metadata valid after the final video tail gets shorter."""
    fps, duration = project.fps, project.duration
    last = _frame(duration, fps)
    available = set(project.cameras)
    project.cuts = [cut for cut in project.cuts
                    if cut.camera in available and _frame(cut.time, fps) <= last]
    project.edit_points = sorted({_frame(value, fps) / fps for value in project.edit_points
                                 if 0 < _frame(value, fps) < last})
    grades = []
    for item in project.clip_grades:
        first, stop = max(0, _frame(item.start, fps)), min(last, _frame(item.end, fps))
        if first < stop:
            item.start, item.end = first / fps, stop / fps
            grades.append(item)
    project.clip_grades = grades
    boundaries = {_frame(cut.time, fps) for cut in project.cuts}
    boundaries.update(_frame(value, fps) for value in project.edit_points)
    project.transitions = [item for item in project.transitions
                           if _frame(item.time, fps) in boundaries
                           and _frame(item.time, fps) > 0
                           and item.time - item.duration / 2 >= 0
                           and item.time + item.duration / 2 <= duration]
    if duration <= 0:
        project.range_start, project.range_end = 0., None
    else:
        if project.range_start >= duration:
            project.range_start = 0.
        if project.range_end is not None:
            project.range_end = min(project.range_end, duration)
            if project.range_end <= project.range_start:
                project.range_start, project.range_end = 0., None
    project.camera_grades = {camera: grade for camera, grade in project.camera_grades.items()
                             if camera in available}
    if project.alignment_reference_camera not in available:
        project.alignment_reference_camera = None
    if project.color_reference_camera not in available:
        project.color_reference_camera = None
    prune_stack(project)


def delete_source_clips(project, clip_ids):
    """Remove selected source instances and their effects; return removed count.

    The media file and continuous master-audio track remain untouched.  Camera
    cuts are removed only when their camera no longer has any source instance.
    The UI can undo by retaining one pre-edit project snapshot.
    """
    identifiers, clips = _selected(project, clip_ids)
    if not clips:
        return 0
    candidate = deepcopy(project)
    candidate.clips = [clip for clip in candidate.clips if clip.id not in identifiers]
    candidate.stabilizations = [item for item in candidate.stabilizations
                                if item.source_id not in identifiers]
    candidate.library_overlays = [item for item in candidate.library_overlays
                                  if item.source_id not in identifiers]
    _trim_project_edges(candidate)
    _publish(project, candidate)
    return len(clips)


def source_move_delta(project, clip_ids, delta, *, target_cameras=None):
    """Validate a group offset and return one shared frame-aligned delta.

    Negative starts are intentional: source tracks are aligned to a continuous
    audio clock and may begin before zero.  Baked library overlays depend on
    timeline coordinates and must be removed before such a source is moved.
    This lightweight check is also suitable for a gesture's final target.
    """
    identifiers, clips = _selected(project, clip_ids)
    if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(delta):
        raise ValueError('素材移动量必须是有限数字。')
    if not isinstance(project.fps, (int, float)) or not math.isfinite(project.fps) or not 0 < project.fps <= 1000:
        raise ValueError('工程帧率无效。')
    value = _frame(float(delta), project.fps) / project.fps
    if not clips:
        return 0.
    targets = target_cameras or {}
    if not value and all(targets.get(clip.id, clip.camera) == clip.camera for clip in clips):
        return 0.
    overlays = project.library_overlays
    for clip in clips:
        start = clip.start + value
        if not math.isfinite(start) or not math.isfinite(start + clip.duration):
            raise ValueError('素材移动后的时间无效。')
        for item in overlays:
            if item.source_id in identifiers or (item.camera == targets.get(clip.id, clip.camera)
                    and start < item.end - 1e-8 and start + clip.duration > item.start + 1e-8):
                raise ValueError('所选素材已应用或会覆盖库叠加特效，请先移除叠加效果再移动素材。')
    return value


def move_source_clips(project, clip_ids, delta, *, target_cameras=None):
    """Move selected instances together after one atomic parameter validation.

    Local stabilization follows the associated source; source cache offsets,
    camera cuts, subtitle times and master audio retain their original clocks.
    Return the actual shared delta, or zero for an unchanged/empty selection.
    No full-project copy or validation is performed on this gesture hot path.
    Call ``finalize_source_edit`` once on release.  A UI that applies repeated
    incremental deltas should restore stabilization bounds from its gesture's
    baseline after every update, because clipping a bound at zero is lossy.
    """
    identifiers, _clips = _selected(project, clip_ids)
    value = source_move_delta(project, identifiers, delta, target_cameras=target_cameras)
    if not value:
        return 0.
    for clip in project.clips:
        if clip.id in identifiers:
            clip.start += value
    for item in project.stabilizations:
        if item.source_id in identifiers:
            item.start, item.end = max(0., item.start + value), item.end + value
    return value


def finalize_source_edit(project):
    """Prune vanished tails and validate once after a source drag completes."""
    candidate = deepcopy(project)
    candidate.stabilizations = [item for item in candidate.stabilizations
                                if item.end > item.start]
    _trim_project_edges(candidate)
    _publish(project, candidate)


def program_boundaries_for_ranges(project, ranges):
    """Map selected shots to the cut each shot uses to join its predecessor.

    The first programme shot has no predecessor; select its ending boundary
    instead.  A full-programme selection has no removable boundary and is a
    no-op.  Inputs use the same half-open frame clock as programme selections.
    """
    fps = project.fps
    spans = program_spans(project)
    last = _frame(project.duration, fps)
    existing = {edge for span in spans for edge in
                (_frame(span.start, fps), _frame(span.end, fps)) if 0 < edge < last}
    selected = set()
    for start, end in ranges:
        if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) for value in (start, end))
                or not 0 <= start < end <= max(project.duration, last / fps) + 1e-6):
            raise ValueError('节目片段范围无效。')
        first, stop = _frame(start, fps), _frame(end, fps)
        if first == stop:
            raise ValueError('节目片段范围不足一帧。')
        boundary = first if first > 0 else stop
        if boundary in existing:
            selected.add(boundary)
    return [frame / fps for frame in sorted(selected)]


def remove_program_boundaries(project, positions):
    """Merge shots across selected internal cuts; the left shot continues.

    This removes an edit decision, never programme time.  Each connected group
    of merged shots inherits its leftmost camera and local grade.  Unselected
    boundaries retain their original camera/grade, including an explicit
    restore cut when the next retained boundary was originally grade-only.
    Transitions attached to removed cuts are removed.  Audio, source clips,
    source effects and subtitle clocks remain untouched.  No black gap or
    ripple edit is implied by this operation.
    """
    from multicam_core import Cut

    if isinstance(positions, (str, bytes)):
        raise ValueError('切点选择必须是时间列表。')
    fps = project.fps
    removed = set()
    for value in positions:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            raise ValueError('切点时间必须是有限数字。')
        removed.add(_frame(value, fps))
    if not removed:
        return 0
    spans = program_spans(project)
    existing = {_frame(span.start, fps) for span in spans[1:]}
    if removed - existing:
        raise ValueError('只能删除节目轨内部现有的切点。')
    candidate = deepcopy(project)
    groups = []
    for span in spans:
        if groups and _frame(span.start, fps) in removed:
            groups[-1][1] = span.end
        else:
            groups.append([span.start, span.end, span.camera, deepcopy(grade_at(project, span.start))])
    cuts = []
    previous_camera = None
    for start, end, camera, grade in groups:
        if camera != previous_camera:
            cuts.append(Cut(start, camera))
            previous_camera = camera
    candidate.cuts = cuts
    candidate.edit_points = [start for start, _end, _camera, _grade in groups[1:]]
    candidate.clip_grades = [ClipGrade(start, end, grade) for start, end, _camera, grade in groups
                             if grade is not None]
    candidate.transitions = [item for item in candidate.transitions
                             if _frame(item.time, fps) not in removed]
    _publish(project, candidate)
    return len(removed)


# Both spellings describe removal from the timeline, never filesystem deletion.
remove_source_clips = delete_source_clips
