"""Exact, short transition caches shared by ordinary playback and export.

The UI schedules this work outside Tk.  Every cache is signed against the
complete pixel-producing project snapshot; the player rejects stale results.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, is_dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess

from creative_render import TransitionSegment, build_transition_command, plan_creative_segments
from exporter_core import CREATE_NO_WINDOW, ExportCancelled, ExportSettings
from multicam_frame_clock import canonical_frame_rate, probe_partition_rates
from subtitle_core import write_ass


def transition_signature(project) -> str:
    """Stable pixel snapshot, including source/LUT replacement on disk."""
    if is_dataclass(project):
        payload = asdict(project)
    elif isinstance(project, dict):
        payload = copy.deepcopy(project)
    else:
        payload = vars(project).copy()
    # Audio samples and preview proxy selection do not produce video pixels.
    for name in ('master_audio', 'range_start', 'range_end','alignment_reference_camera','color_reference_camera'):
        payload.pop(name, None)
    for clip in payload.get('clips',[]):
        for name in ('camera_make','camera_model','camera_metadata_source','proxy_path'):
            clip.pop(name,None)
    paths = set()

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {'path', 'lut_path', 'output_path', 'stabilized_path', 'cache_path', 'mask_path', 'asset_path'} and isinstance(item, str) and item:
                    paths.add(item)
                else:
                    collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    collect(payload)
    stamps = {}
    for name in sorted(paths):
        try:
            info = Path(name).stat()
            stamps[name] = (info.st_size, info.st_mtime_ns)
        except OSError:
            stamps[name] = None
    raw = json.dumps({'version': 1, 'project': payload, 'files': stamps},
                     sort_keys=True, ensure_ascii=False, allow_nan=False, default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def transition_windows(project) -> list[tuple[float, float]]:
    """Use the export planner's integer frame windows, including odd lengths."""
    fps = float(project.fps)
    return [(job.start, job.end) for job in plan_creative_segments(
        project, project.segments(start=0, end=project.duration)) if isinstance(job, TransitionSegment)]


def _run(command, cancel_event):
    # A file avoids stderr-pipe deadlocks on a corrupt source, while keeping
    # cancellation responsive even during expensive source decoding.
    import tempfile
    from cache_paths import cache_dir
    with tempfile.TemporaryFile(dir=cache_dir('temporary')) as error:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=error, creationflags=CREATE_NO_WINDOW)
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.wait(.05):
                    raise ExportCancelled('已取消转场预览准备')
                if cancel_event is None:
                    try: process.wait(timeout=.05)
                    except subprocess.TimeoutExpired: pass
            if process.returncode:
                error.seek(0)
                raise RuntimeError('转场预览生成失败：' + error.read().decode('utf-8', 'replace')[-2400:])
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=2)


def render_transition_previews(project, cache_dir, ffmpeg, ffprobe=None, *, cancel_event=None,
                               progress=None, width=960, height=540, use_nvenc=False):
    """Return ``path/start/end`` entries for baked colour/transition/subtitles.

    The programme clock and master audio are unchanged. Only exact centred
    transition windows are rendered; ordinary footage stays on its GPU path.
    ``progress(done,total)`` is optional and runs on the caller's worker.
    """
    snapshot = copy.deepcopy(project)
    snapshot.validate()
    if type(width) is not int or type(height) is not int or width < 16 or height < 16 or width % 2 or height % 2:
        raise ValueError('转场预览尺寸必须是至少16的偶数')
    jobs = [job for job in plan_creative_segments(snapshot,
            snapshot.segments(start=0, end=snapshot.duration)) if isinstance(job, TransitionSegment)]
    if not jobs: return []
    signature = transition_signature(snapshot)
    folder = Path(cache_dir).resolve() / signature
    folder.mkdir(parents=True, exist_ok=True)
    rate_text = str(canonical_frame_rate(snapshot.fps))
    paths = {name for job in jobs for name in (job.path, job.other_path)}
    rates = probe_partition_rates(str(ffprobe), paths, cancel_event) if ffprobe else {}
    track = snapshot.subtitles
    ass_path = (write_ass(track, folder/'subtitles.ass', width, height)
                if track.enabled and track.cues else None)
    settings = ExportSettings(codec='h264_nvenc' if use_nvenc else 'libx264',
                              preset='p2' if use_nvenc else 'veryfast', audio='none',
                              quality=18, bit_depth='8-bit', decode_mode='cpu', bframes=0)
    encode = (['-c:v', 'h264_nvenc', '-preset', 'p2', '-rc', 'vbr', '-cq', '18', '-b:v', '0', '-bf', '0']
              if use_nvenc else ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-bf', '0', '-threads', '2'])
    result = []
    for index, job in enumerate(jobs):
        if cancel_event is not None and cancel_event.is_set():
            raise ExportCancelled('已取消转场预览准备')
        output = folder / f'transition_{index:05d}_{width}x{height}.mp4'
        if not output.is_file() or output.stat().st_size < 128:
            temporary = output.with_name(output.stem+'.pending.mp4')
            command = build_transition_command(job, snapshot,
                [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y'],
                settings, width, height, 'yuv420p', rate_text, encode, temporary,
                ass_path=ass_path, source_frame_rates=rates)
            try:
                _run(command, cancel_event)
                os.replace(temporary, output)
            finally:
                temporary.unlink(missing_ok=True)
        result.append({'path': str(output), 'start': job.start, 'end': job.end})
        if progress is not None: progress(index+1, len(jobs))
    return result
