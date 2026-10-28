"""Frame-clock planning and alpha-preserving final rendering of camera layers.

This mode deliberately ignores programme camera cuts and their transitions.
Every active camera contributes a layer; original media and full-quality
treatments are used, never preview proxies. The final delivery remains opaque.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
from pathlib import Path

from creative_core import grade_filter
from gyroflow_core import cache_matches
from library_overlay import overlay_cache_matches
from program_edits import grade_at
from subtitle_core import subtitle_filter
from video_stack import pixel_format_has_alpha


def _frame(seconds, fps):
    return math.floor(float(seconds) * fps + .5)


@dataclass(frozen=True, slots=True)
class StackLayer:
    camera: int
    path: str
    original_path: str
    source_start: float
    phase_origin: float
    has_alpha: bool = False


@dataclass(frozen=True, slots=True)
class StackSegment:
    start: float
    duration: float
    frames: int
    layers: tuple[StackLayer, ...]  # top to bottom
    # The common export scheduler treats these as a complex picture job.
    camera: int = 0
    path: None = None
    source_start: float = 0.0
    phase_origin: None = None

    @property
    def end(self):
        return self.start + self.duration


def _layer(project, clip, position):
    source = max(0.0, position - clip.start)
    picture = clip.path
    origin = max(0.0, _frame(clip.start, project.fps) / project.fps)
    stabilized = project.stabilization_at(clip.camera, position, clip.id)
    if stabilized is not None:
        if not Path(stabilized.cache_path).is_file() or not cache_matches(stabilized, clip.path):
            raise ValueError('防抖缓存缺失或失效，请重新生成或移除所选片段防抖。')
        source = max(0.0, source - stabilized.source_start)
        picture = stabilized.cache_path
        origin = max(0.0, clip.start + stabilized.source_start)
    overlay = project.library_overlay_at(clip.camera, position, clip.id)
    if overlay is not None:
        if not overlay_cache_matches(overlay, project, strict=True):
            raise ValueError('素材叠加缓存失效，请重新应用或移除所选素材特效。')
        source = max(0.0, position - clip.start - overlay.source_start)
        picture = overlay.cache_path
        origin = max(0.0, clip.start + overlay.source_start)
    return StackLayer(clip.camera, picture, clip.path, source, origin, clip.has_alpha)


def plan_stack_segments(project):
    """Open only active layers, with half-open boundaries on one frame clock."""
    if not project.clips:
        return []
    fps = float(project.fps)
    end = project.duration if project.range_end is None else project.range_end
    first, last = _frame(project.range_start, fps), _frame(end, fps)
    if first >= last:
        raise ValueError('成片范围不足一帧')
    cameras = project.stacked_cameras()
    by_camera = {camera: [clip for clip in project.clips if clip.camera == camera]
                 for camera in cameras}
    edges = {first, last}
    for clips in by_camera.values():
        for clip in clips:
            edges.update((_frame(clip.start, fps), _frame(clip.end, fps)))
    for items in (project.clip_grades, project.stabilizations, project.library_overlays):
        for item in items:
            edges.update((_frame(item.start, fps), _frame(item.end, fps)))
    boundaries = sorted(value for value in edges if first <= value <= last)
    result = []
    for left, right in zip(boundaries, boundaries[1:]):
        layers = []
        for camera, clips in by_camera.items():
            clip = max((clip for clip in clips if _frame(clip.start, fps) <= left
                        and _frame(clip.end, fps) >= right),
                       key=lambda clip: (clip.start, clip.id), default=None)
            if clip is not None:
                layers.append(_layer(project, clip, left / fps))
        result.append(StackSegment(left / fps, (right-left) / fps, right-left, tuple(layers)))
    return result


def validate_stack_media(segments, media):
    """Do not flatten a transparent camera via an opaque treatment cache."""
    for segment in segments:
        for layer in segment.layers:
            original, treated = media[layer.original_path], media[layer.path]
            alpha = (layer.has_alpha or getattr(original, 'has_alpha', False)
                     or pixel_format_has_alpha(original.pixel_format))
            if alpha and layer.path != layer.original_path and not (
                    getattr(treated, 'has_alpha', False) or pixel_format_has_alpha(treated.pixel_format)):
                raise ValueError(f'机位 {layer.camera} 的防抖/素材特效缓存没有透明通道。'
                                 '请移除该处理或重新生成保留透明通道的素材后再叠加导出。')


def build_stack_command(segment, project, base, width, height, pixel_format,
                        rate_text, encode_args, chunk, ass_path=None, *,
                        source_frame_rates=None, media_by_path=None):
    """CPU alpha composition, followed by the chosen software/NVENC encoder."""
    rates = source_frame_rates or {}
    media = media_by_path or {}
    rate = Fraction(rate_text)
    deep = pixel_format.endswith('10le')
    alpha_format = 'yuva444p10le' if deep else 'rgba'
    overlay_format = 'yuv444p10' if deep else 'rgb'
    colour_format = 'yuv444p10le' if deep else 'rgb24'
    command = list(base) + ['-filter_complex_threads', '1']
    graph = [f'color=c=black:s={width}x{height}:r={rate_text},'
             f'format={colour_format},trim=end_frame={segment.frames},settb=AVTB,'
             f'setpts=N/({rate_text}*TB)[stack0]']
    for index, layer in enumerate(reversed(segment.layers)):
        offset = max(0, _frame(segment.start-layer.phase_origin, project.fps))
        source_rate = rates.get(layer.path)
        quantum = (rate / source_rate).numerator if source_rate else None
        prefix = offset % quantum if quantum else offset
        seek = max(0.0, layer.source_start-prefix / project.fps)
        command += ['-threads', '1', '-ss', f'{seek:.9f}']
        info = media.get(layer.path)
        # The native VP8/9 decoders do not expose WebM's separate alpha stream.
        if info is not None and (getattr(info, 'has_alpha', False) or layer.has_alpha):
            decoder = {'vp8': 'libvpx', 'vp9': 'libvpx-vp9'}.get(info.video_codec)
            if decoder:
                command += ['-c:v', decoder]
        command += ['-i', layer.path]
        normalize = (f'[{index}:v:0]setpts=PTS-STARTPTS,fps=fps={rate_text}:start_time=0,'
                     f'format={alpha_format},scale={width}:{height}:'
                     'force_original_aspect_ratio=decrease:force_divisible_by=2,'
                     f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black@0,setsar=1,'
                     f'tpad=stop_mode=clone:stop_duration={segment.duration+prefix/project.fps+1:.9f},'
                     f'trim=start_frame={prefix}:end_frame={prefix+segment.frames},'
                     f'settb=AVTB,setpts=N/({rate_text}*TB),format={alpha_format}')
        grades = [project.camera_grades.get(layer.camera)]
        effects = ','.join(filter(None, (grade_filter(grade) for grade in grades if grade is not None)))
        if effects:
            # RGB LUT/curves can discard alpha. Keep the original mask on a
            # separate branch and restore it after all per-layer colour work.
            graph += [normalize + f',split=2[colour{index}][maskin{index}]',
                      f'[maskin{index}]alphaextract[mask{index}]',
                      f'[colour{index}]{effects}[graded{index}]',
                      f'[graded{index}][mask{index}]alphamerge,format={alpha_format}[layer{index}]']
        else:
            graph.append(normalize + f'[layer{index}]')
        graph.append(f'[stack{index}][layer{index}]overlay=x=0:y=0:format={overlay_format}:'
                     f'alpha=straight:eof_action=pass:repeatlast=0[stack{index+1}]')
    # A ClipGrade belongs to a programme time range, not to any camera. Apply
    # it to the finished blend once; nonlinear curves cannot be distributed
    # across translucent layers without changing the picture.
    local = grade_at(project, segment.start)
    final = ([grade_filter(local)] if local is not None else []) + [grade_filter(project.global_grade)]
    if ass_path is not None:
        final += [f'setpts=PTS+{segment.start:.12g}/TB', subtitle_filter(ass_path), 'setpts=PTS-STARTPTS']
    final += [f'format={pixel_format}']
    graph.append(f'[stack{len(segment.layers)}]' + ','.join(filter(None, final)) + '[picture]')
    command += ['-filter_complex', ';'.join(graph), '-map', '[picture]', '-an', '-sn', '-dn']
    command += list(encode_args)
    command += ['-pix_fmt', pixel_format, '-r', rate_text, '-frames:v', str(segment.frames),
                '-map_metadata', '-1', '-video_track_timescale', str(rate.numerator), str(chunk)]
    return command
