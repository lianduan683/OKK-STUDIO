"""Frame based programme selections, explicit edits and local colour grades.

An edit boundary has an identity of its own: cutting a shot in two does not
need to change the camera. Ranges are half open on the same master clock as
the audio. This module deliberately does not import the project model.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Mapping

from creative_core import ColorGrade, grade_from_dict, grade_to_dict, validate_grade


@dataclass(frozen=True, slots=True)
class ProgramSpan:
    start: float
    end: float
    camera: int

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(slots=True)
class ClipGrade:
    start: float
    end: float
    grade: ColorGrade


def _number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label}必须是有限数字")
    return float(value)


def _frame(value: float, fps: float) -> int:
    return math.floor(value * fps + .5)


def _clock(project) -> tuple[float, int]:
    fps = _number(project.fps, "工程帧率")
    duration = _number(project.duration, "工程时长")
    if not 0 < fps <= 1000 or duration < 0:
        raise ValueError("工程帧率或时长无效")
    return fps, _frame(duration, fps)


def _range(project, start: float, end: float) -> tuple[int, int, float]:
    fps, last = _clock(project)
    start, end = _number(start, "片段开始"), _number(end, "片段结束")
    if start < 0 or end > max(project.duration, last / fps) + 1e-6 or end <= start:
        raise ValueError("片段范围无效或超出工程")
    first, stop = _frame(start, fps), _frame(end, fps)
    if first < 0 or stop > last or stop <= first:
        raise ValueError("片段范围不足一帧或超出工程")
    return first, stop, fps


def validate_program_edits(project) -> None:
    fps, last = _clock(project)
    points = getattr(project, "edit_points", [])
    grades = getattr(project, "clip_grades", [])
    if not isinstance(points, list) or len(points) > 1_000_000:
        raise ValueError("编辑切点必须是列表且数量不能超过 1000000")
    seen = set()
    for value in points:
        value = _number(value, "编辑切点")
        frame = _frame(value, fps)
        if value <= 0 or value >= project.duration or not 0 < frame < last:
            raise ValueError("编辑切点必须在工程内部")
        if frame in seen:
            raise ValueError("编辑切点不能重复")
        seen.add(frame)
    if not isinstance(grades, list) or len(grades) > 100_000:
        raise ValueError("片段调色必须是列表且数量不能超过 100000")
    ranges = []
    for item in grades:
        if not isinstance(item, ClipGrade):
            raise ValueError("片段调色结构无效")
        first, stop, _ = _range(project, item.start, item.end)
        validate_grade(item.grade, check_lut=False)
        ranges.append((first, stop))
    previous = -1
    for first, stop in sorted(ranges):
        if first < previous:
            raise ValueError("片段调色范围不能重叠")
        previous = stop


def program_spans(project) -> list[ProgramSpan]:
    validate_program_edits(project)
    fps, last = _clock(project)
    if last <= 0 or not project.cameras:
        return []
    edges = {0, last}
    edges.update(_frame(cut.time, fps) for cut in project.cuts)
    edges.update(_frame(value, fps) for value in getattr(project, "edit_points", []))
    for item in getattr(project, "clip_grades", []):
        edges.update((_frame(item.start, fps), _frame(item.end, fps)))
    ordered = sorted(value for value in edges if 0 <= value <= last)
    cuts = sorted(project.cuts, key=lambda item: item.time)
    camera, index = min(project.cameras), 0
    result = []
    for first, stop in zip(ordered, ordered[1:]):
        while index < len(cuts) and _frame(cuts[index].time, fps) <= first:
            camera = cuts[index].camera
            index += 1
        if stop > first:
            result.append(ProgramSpan(first / fps, stop / fps, camera))
    return result


def split_at(project, time: float) -> bool:
    fps, last = _clock(project)
    time = _number(time, "切割时间")
    if not 0 <= time <= project.duration + 1e-6:
        raise ValueError("切割时间超出工程")
    frame = _frame(time, fps)
    if not 0 < frame < last:
        return False
    edges = {_frame(point, fps) for point in getattr(project, "edit_points", [])}
    edges.update(_frame(cut.time, fps) for cut in project.cuts)
    if frame in edges:
        return False
    project.edit_points = sorted([*getattr(project, "edit_points", []), frame / fps])
    return True


def _preserve_boundaries(project, frames, fps: float, last: int) -> None:
    points = {_frame(value, fps) for value in getattr(project, "edit_points", [])}
    points.update(frame for frame in frames if 0 < frame < last)
    project.edit_points = [frame / fps for frame in sorted(points)]


def set_camera_on_range(project, start: float, end: float, camera: int) -> None:
    from multicam_core import Cut  # Runtime import avoids model dependency cycles.
    first, stop, fps = _range(project, start, end)
    if isinstance(camera, bool) or not isinstance(camera, int) or camera not in project.cameras:
        raise ValueError("请选择工程中存在的机位")
    last = _frame(project.duration, fps)
    original = {_frame(cut.time, fps): cut.camera for cut in sorted(project.cuts, key=lambda item: item.time)}
    original.setdefault(0, min(project.cameras))
    restore = project.camera_at(stop / fps)
    selected = {frame: value for frame, value in original.items() if frame < first or frame >= stop}
    selected[first] = camera
    if stop < last:
        selected[stop] = restore
    normalized = []
    previous = None
    for frame, value in sorted(selected.items()):
        if value != previous:
            normalized.append(Cut(frame / fps, value))
            previous = value
    # Preserve pre-existing cuts as editable boundaries even if the cameras
    # now match. They may carry grades or be selected for another operation.
    _preserve_boundaries(project, {*original, first, stop}, fps, last)
    project.cuts = normalized


def set_grade_on_range(project, start: float, end: float, grade: ColorGrade) -> None:
    first, stop, fps = _range(project, start, end)
    validate_grade(grade, check_lut=False)
    validate_program_edits(project)
    result = []
    for item in getattr(project, "clip_grades", []):
        left, right = _frame(item.start, fps), _frame(item.end, fps)
        if right <= first or left >= stop:
            result.append(deepcopy(item))
            continue
        if left < first:
            result.append(ClipGrade(left / fps, first / fps, deepcopy(item.grade)))
        if right > stop:
            result.append(ClipGrade(stop / fps, right / fps, deepcopy(item.grade)))
    result.append(ClipGrade(first / fps, stop / fps, deepcopy(grade)))
    project.clip_grades = sorted(result, key=lambda item: item.start)
    _preserve_boundaries(project, (first, stop), fps, _frame(project.duration, fps))


def grade_at(project, time: float) -> ColorGrade | None:
    fps, last = _clock(project)
    # Time is looked up on the frame clock, not via inclusive floating point
    # comparisons. Exactly the boundary belongs to the incoming shot.
    time = _number(time, "调色时间")
    frame = _frame(time, fps)
    if time < 0 or not 0 <= frame < last:
        return None
    for item in getattr(project, "clip_grades", []):
        if _frame(item.start, fps) <= frame < _frame(item.end, fps):
            return item.grade
    return None


def program_edits_to_dict(project) -> dict:
    validate_program_edits(project)
    return {"edit_points": list(getattr(project, "edit_points", [])),
            "clip_grades": [{"start": item.start, "end": item.end, "grade": grade_to_dict(item.grade)}
                            for item in getattr(project, "clip_grades", [])]}


def program_edits_from_dict(value: Mapping | None) -> tuple[list[float], list[ClipGrade]]:
    if value is None:
        return [], []
    if not isinstance(value, Mapping) or set(value) - {"edit_points", "clip_grades"}:
        raise ValueError("分段编辑设置包含未知字段")
    points = value.get("edit_points", [])
    grades = value.get("clip_grades", [])
    if not isinstance(points, list) or not isinstance(grades, list):
        raise ValueError("分段编辑设置必须是列表")
    parsed = []
    for item in grades:
        if not isinstance(item, Mapping) or set(item) != {"start", "end", "grade"}:
            raise ValueError("片段调色包含未知字段或缺少范围")
        if not isinstance(item["grade"], Mapping):
            raise ValueError("片段调色设置必须是对象")
        parsed.append(ClipGrade(_number(item["start"], "片段开始"),
                                _number(item["end"], "片段结束"), grade_from_dict(item["grade"])))
    return [_number(item, "编辑切点") for item in points], parsed
