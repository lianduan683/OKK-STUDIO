"""Frame-exact programme cut moves, independent from source synchronisation."""
from __future__ import annotations

import math

from program_edits import program_spans

OVERLAY_BOUNDARY_MESSAGE = '此片段已应用库叠加特效，请先移除特效、调整切点后重新应用'


def _frame(value, fps):
    return math.floor(float(value)*fps+.5)


def boundary_has_library_overlay(project, position):
    overlays = getattr(project, 'library_overlays', ())
    if not overlays:
        return False
    frame, fps = _frame(position, project.fps), project.fps
    adjacent = [span for span in program_spans(project)
                if _frame(span.start, fps) == frame or _frame(span.end, fps) == frame]
    return any(item.start < span.end-1e-8 and item.end > span.start+1e-8
               for item in overlays for span in adjacent)


def boundary_limits(project, position):
    """Return legal frame bounds, keeping one frame on each neighbouring side.

    Centred transitions retain their duration and must fit between programme
    boundaries, so their occupied windows may narrow the available range.
    """
    fps = project.fps
    frame = _frame(position, fps)
    if boundary_has_library_overlay(project, position):
        raise ValueError(OVERLAY_BOUNDARY_MESSAGE)
    spans = program_spans(project)
    edges = sorted({value for span in spans for value in (_frame(span.start, fps), _frame(span.end, fps))})
    if len(edges) < 3 or frame not in edges or frame in {edges[0], edges[-1]}:
        raise ValueError('只能移动节目轨内部的剪辑切点。')
    index = edges.index(frame)
    previous, following = edges[index-1], edges[index+1]
    lower, upper = previous+1, following-1
    half = max((item.duration/2 for item in project.transitions if _frame(item.time, fps) == frame), default=0.)
    lower = max(lower, math.ceil((previous/fps+half)*fps-1e-7))
    upper = min(upper, math.floor((following/fps-half)*fps+1e-7))
    for item in project.transitions:
        other = _frame(item.time, fps)
        if other < frame:
            lower = max(lower, math.ceil((item.time+item.duration/2+half)*fps-1e-7))
        elif other > frame:
            upper = min(upper, math.floor((item.time-item.duration/2-half)*fps+1e-7))
    return (lower, upper) if lower <= upper else (frame, frame)


def move_program_boundary(project, position, target, *, limits=None):
    """Move all records attached to this boundary together, leaving sources fixed.

    Returns the actual clamped frame-aligned time. Edits are in-place so callers
    can record one undo state for an entire mouse gesture.
    """
    fps = project.fps
    old = _frame(position, fps)
    low, high = boundary_limits(project, position) if limits is None else limits
    value = max(low, min(high, _frame(target, fps)))
    if value == old:
        return old/fps
    time = value/fps
    for cut in project.cuts:
        if _frame(cut.time, fps) == old:
            cut.time = time
    project.cuts.sort(key=lambda item: item.time)
    project.edit_points = sorted({time if _frame(point, fps) == old else point for point in project.edit_points})
    for item in project.clip_grades:
        if _frame(item.start, fps) == old:
            item.start = time
        if _frame(item.end, fps) == old:
            item.end = time
    for item in project.transitions:
        if _frame(item.time, fps) == old:
            item.time = time
    project.transitions.sort(key=lambda item: item.time)
    return time
