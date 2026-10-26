"""Atomic batch imports: every selected video becomes one distinct camera."""
from __future__ import annotations

import math
import os
from pathlib import Path

from exporter_core import ExportCancelled, MediaInfo, probe_media


def assign_camera_ids(existing, count: int) -> list[int]:
    """Allocate the lowest unused positive ids without a nine-camera cap."""
    if type(count) is not int or not 0 <= count <= 100000:
        raise ValueError('导入数量必须是 0 到 100000 之间的整数')
    used = set(existing)
    if any(type(value) is not int or value <= 0 for value in used):
        raise ValueError('已有机位编号必须为正整数')
    result = []
    candidate = 1
    while len(result) < count:
        if candidate not in used:
            result.append(candidate)
            used.add(candidate)
        candidate += 1
    return result


def batch_import(ffprobe, paths, existing_cameras, *, cancel_event=None, progress=None) -> list[tuple[int, MediaInfo]]:
    """Probe and validate the complete selection before returning any imports.

    This function owns no project, so a bad/cancelled file cannot leave a
    half-imported camera list.  Existing ids and the input source files are
    never changed. ``progress(percent, text)`` runs on the importing worker.
    """
    sources = [os.fspath(path) for path in paths]
    cameras = assign_camera_ids(existing_cameras, len(sources))
    if not sources:
        return []
    if not ffprobe:
        raise ValueError('未找到 ffprobe，无法读取视频信息')

    def cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise ExportCancelled('已取消批量导入机位')

    for source in sources:
        cancelled()
        if not source or any(character in source for character in '\x00\r\n') or not Path(source).is_file():
            raise ValueError('无法读取所选视频：'+str(source))
    result = []
    for index, (camera, source) in enumerate(zip(cameras, sources)):
        cancelled()
        if progress: progress(index/len(sources)*100, f'读取机位 {index+1}/{len(sources)}：{Path(source).name}')
        try:
            info = probe_media(str(ffprobe), source)
        except Exception as error:
            cancelled()
            raise ValueError(f'无法读取视频 {Path(source).name}：{error}') from error
        if (info.width <= 0 or info.height <= 0 or not math.isfinite(info.duration)
                or info.duration <= 0):
            raise ValueError(f'所选文件没有可读取的视频画面或时长：{Path(source).name}')
        result.append((camera, info))
        if progress: progress((index+1)/len(sources)*100, f'已读取机位 {index+1}/{len(sources)}')
    cancelled()
    return result
