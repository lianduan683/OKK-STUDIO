"""Original-resolution library overlays shared by preview and final delivery.

Only the overlay is baked. Colour grades, subtitles and transitions remain
editable and are applied by the normal programme renderer after this cache.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
import threading
import time

from cache_paths import cache_dir
from cache_publish import publish_completed
from exporter_core import ExportCancelled, probe_media
from effects_library import asset_path as library_asset_path
from gyroflow_core import cache_matches as stabilization_matches


@dataclass(slots=True)
class LibraryOverlayRange:
    start: float
    end: float
    camera: int
    source_id: str
    asset_path: str
    kind: str
    opacity: float
    source_start: float
    source_end: float
    cache_path: str
    signature: str
    width: int
    height: int
    fps: float
    encoding: str = ''
    asset_name: str = ''
    cache_size: int = 0
    cache_mtime_ns: int = 0


def validate_library_overlay(record):
    if not isinstance(record, LibraryOverlayRange):
        raise ValueError('素材叠加结构无效')
    for name in ('start', 'end', 'source_start', 'source_end', 'fps', 'opacity'):
        value = getattr(record, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('素材叠加时间或参数无效')
    if not (0 <= record.start < record.end and 0 <= record.source_start < record.source_end
            and 0 <= record.opacity <= 1 and 0 < record.fps <= 1000):
        raise ValueError('素材叠加范围或透明度无效')
    if type(record.camera) is not int or record.camera < 1:
        raise ValueError('素材叠加机位无效')
    for name in ('source_id', 'asset_path', 'cache_path', 'signature'):
        value = getattr(record, name)
        if not isinstance(value, str) or not value or '\x00' in value:
            raise ValueError('素材叠加标识或路径无效')
    if record.kind not in {'alpha', 'screen'} or not re.fullmatch(r'[0-9a-f]{64}', record.signature):
        raise ValueError('素材叠加类型或缓存签名无效')
    if any(type(value) is not int or value <= 0 for value in (record.width, record.height)):
        raise ValueError('素材叠加缓存尺寸无效')
    if any(type(value) is not int or value < 0 for value in (record.cache_size, record.cache_mtime_ns)):
        raise ValueError('素材叠加缓存元数据无效')
    if not isinstance(record.encoding, str) or not isinstance(record.asset_name, str):
        raise ValueError('素材叠加描述无效')


def overlay_from_dict(data, absolute=lambda value: value):
    if not isinstance(data, dict) or set(data) - {item.name for item in fields(LibraryOverlayRange)}:
        raise ValueError('素材叠加包含未知字段')
    values = dict(data)
    for name in ('asset_path', 'cache_path'):
        values[name] = absolute(values.get(name, ''))
    result = LibraryOverlayRange(**values)
    validate_library_overlay(result)
    return result


def _stamp(path):
    item = Path(path).resolve()
    info = item.stat()
    if not item.is_file():
        raise ValueError('素材叠加依赖不存在：' + str(item))
    return str(item), info.st_size, info.st_mtime_ns


def _input(project, clip, start, end):
    """Resolve one continuous original/stabilized source; proxies never enter."""
    relevant = [item for item in project.stabilizations if item.camera == clip.camera
                and item.source_id == clip.id and item.start < end and item.end > start]
    if not relevant:
        return clip.path, 0., clip.duration, ''
    treated = project.stabilization_at(clip.camera, (start + end) / 2, clip.id)
    if (treated is None or treated.start > start + 1e-6 or treated.end < end - 1e-6
            or any(item is not treated for item in relevant)):
        raise ValueError('所选叠加范围跨越不同防抖区域，请分段应用素材特效。')
    if not stabilization_matches(treated, clip.path):
        raise ValueError('请先重新生成所选片段的防抖缓存。')
    return treated.cache_path, treated.source_start, treated.source_end, treated.signature


def _signature(project, record):
    clip = next((item for item in project.clips if item.id == record.source_id), None)
    if clip is None or clip.camera != record.camera:
        raise ValueError('叠加原片不存在')
    path, origin, stop, stabilized = _input(project, clip, record.start, record.end)
    data = {'algorithm': 'okk-original-resolution-overlay-v2',
            'original': _stamp(clip.path), 'input': _stamp(path), 'asset': _stamp(record.asset_path),
            'clip_start': clip.start, 'clip_duration': clip.duration,
            'stabilization': (origin, stop, stabilized), 'range': (record.start, record.end),
            'source_range': (record.source_start, record.source_end),
            'kind': record.kind, 'opacity': record.opacity,
            'size': (record.width, record.height), 'fps': record.fps, 'project_fps': project.fps}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


_valid_cache = {}
_valid_lock = threading.Lock()


def overlay_cache_matches(record, project, *, strict=False):
    """Playback checks at most twice per second; exports always check afresh."""
    clip = next((item for item in project.clips if item.id == record.source_id), None)
    key = (id(project), record.signature, record.cache_path,
           clip.start if clip else None,
           tuple((item.signature, item.start, item.end, item.cache_path) for item in project.stabilizations))
    now = time.monotonic()
    with _valid_lock:
        cached = _valid_cache.get(key)
        if not strict and cached and now < cached[0]:
            return cached[1]
    try:
        valid = record.signature == _signature(project, record)
        _, size, modified = _stamp(record.cache_path)
        valid = valid and size > 0 and (not record.cache_size or size == record.cache_size)
        valid = valid and (not record.cache_mtime_ns or modified == record.cache_mtime_ns)
    except (OSError, ValueError, TypeError):
        valid = False
    with _valid_lock:
        if len(_valid_cache) >= 2048:
            _valid_cache.clear()
        _valid_cache[key] = (now + .5, valid)
    return valid


def _command(ffmpeg, input_path, origin, record, output, media, encoder, lead_frames=0):
    rate = Fraction(record.fps).limit_denominator(1000000)
    rate_text = f'{rate.numerator}/{rate.denominator}'
    count = max(1, round((record.source_end - record.source_start) * record.fps))
    deep = media.bit_depth > 8
    pixel = 'yuv420p10le' if deep else 'yuv420p'
    base = (f'[0:v]setpts=PTS-STARTPTS,fps={rate_text},setsar=1,'
            f'format={pixel}[base]')
    # Handles precede the selected range. Hold the effect's first frame there,
    # so its animation starts at the selected cut rather than inside a handle.
    overlay = (f'[1:v]setpts=PTS-STARTPTS,fps={rate_text},'
               f'tpad=start={lead_frames}:start_mode=clone,'
               f'scale={record.width}:{record.height},setsar=1')
    if record.kind == 'alpha':
        overlay += f',format=rgba,colorchannelmixer=aa={record.opacity:.9f}[effect]'
        blend = '[base][effect]overlay=shortest=1:format=auto'
    else:
        rgb = 'gbrp10le' if deep else 'gbrp'
        base += f';[base]format={rgb}[rgbbase]'
        overlay += f',format={rgb}[effect]'
        blend = f'[rgbbase][effect]blend=all_mode=screen:all_opacity={record.opacity:.9f}:shortest=1'
    graph = base + ';' + overlay + ';' + blend + f',format={pixel}[v]'
    command = [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y', '-filter_complex_threads', '1',
               '-ss', f'{record.source_start-origin:.9f}', '-i', str(input_path),
               '-stream_loop', '-1', '-i', record.asset_path, '-filter_complex', graph,
               '-map', '[v]', '-an', '-frames:v', str(count), '-fps_mode', 'passthrough']
    if encoder == 'hevc_nvenc':
        command += ['-c:v', encoder, '-preset', 'p2', '-tune', 'lossless', '-rc', 'constqp', '-qp', '0',
                    '-pix_fmt', pixel, '-g', '30', '-bf', '0']
    else:
        command += ['-c:v', 'libx265', '-preset', 'ultrafast', '-x265-params',
                    'lossless=1:log-level=error:pools=2:frame-threads=2', '-pix_fmt', pixel, '-g', '30', '-bf', '0']
    for name, value in (('color_primaries', media.color_primaries), ('color_trc', media.color_transfer),
                        ('colorspace', media.color_space), ('color_range', media.color_range)):
        if value and value not in {'unknown', 'unspecified'}:
            command += ['-' + name, value]
    command += ['-progress', 'pipe:1', '-nostats', str(output)]
    return command, count


def prepare_library_overlay(project, start, end, asset, opacity, ffmpeg, ffprobe,
                            cancel_event=None, progress=None):
    """Create one reusable, full-resolution effect cache without editing project."""
    from creative_render import _continuous_clip
    from image_stabilization import _run
    project.validate()
    if cancel_event is not None and cancel_event.is_set():
        raise ExportCancelled('已取消素材叠加')
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
               for value in (start, end, opacity)):
        raise ValueError('素材叠加范围或透明度无效')
    if not (0 <= start < end <= project.duration + 1e-6 and 0 <= opacity <= 1):
        raise ValueError('请选择有效片段范围和 0–1 的不透明度。')
    fps = float(project.fps)
    first, last = math.floor(start*fps+.5), math.floor(end*fps+.5)
    if last <= first:
        raise ValueError('素材叠加至少需要一帧。')
    start, end = first/fps, last/fps
    camera = project.camera_at(start)
    if camera is None or any(project.camera_at(point) != camera for point in
            [start, (last-1)/fps, *(cut.time for cut in project.cuts if start < cut.time < end)]):
        raise ValueError('请一次选择同一机位的连续片段应用素材特效。')
    clip = _continuous_clip(project, camera, first, last)
    if any(item.source_id == clip.id and item.start < end and item.end > start
           for item in getattr(project, 'library_overlays', [])):
        raise ValueError('所选范围已有素材叠加，请先移除旧叠加再应用。')
    path, origin, limit, _ = _input(project, clip, start, end)
    media = probe_media(ffprobe, path)
    if media.width <= 0 or media.height <= 0 or media.width % 2 or media.height % 2:
        raise ValueError('素材叠加需要宽高为偶数的视频素材。')
    asset_file = library_asset_path(asset)
    if not Path(asset_file).is_file() or asset.kind not in {'alpha', 'screen'}:
        raise ValueError('素材特效文件缺失或类型无效。')
    # Include source handles so centred transitions can still read either side.
    handles = max([1., *(item.duration for item in project.transitions)])
    source_start = max(origin, math.floor((start-clip.start-handles)*fps)/fps)
    source_end = min(limit, math.ceil((end-clip.start+handles)*fps)/fps)
    record = LibraryOverlayRange(start, end, camera, clip.id, asset_file, asset.kind, float(opacity),
        source_start, source_end, '', '0'*64, media.width, media.height, fps, asset_name=asset.name)
    record.signature = _signature(project, record)
    folder = cache_dir('effects/library')
    destination = folder/(record.signature + '.mkv')
    record.cache_path = str(destination)
    if destination.is_file():
        try:
            cached = probe_media(ffprobe, str(destination))
            if (cached.width == record.width and cached.height == record.height
                    and abs(cached.duration-(source_end-source_start)) <= 2/fps):
                _, record.cache_size, record.cache_mtime_ns = _stamp(destination)
                record.encoding = 'HEVC lossless'
                if progress: progress(100., '素材叠加缓存已就绪')
                return record
        except (ValueError, OSError, RuntimeError):
            pass
    with tempfile.TemporaryDirectory(prefix='overlay_', dir=folder) as temporary:
        output = Path(temporary)/'render.mkv'
        for encoder in ('hevc_nvenc', 'libx265'):
            lead_frames = max(0, round((start - clip.start - source_start) * fps))
            command, count = _command(ffmpeg, path, origin, record, output, media, encoder, lead_frames)
            if progress: progress(0., '生成原分辨率素材叠加 · ' + encoder)
            try:
                _run(command, temporary, cancel_event=cancel_event,
                     progress=(lambda value: progress(value*98., '生成素材叠加 · ' + encoder)) if progress else None,
                     frame_count=count)
                record.encoding = encoder + ' lossless'
                break
            except ExportCancelled:
                raise
            except RuntimeError as exc:
                if encoder != 'hevc_nvenc':
                    raise RuntimeError(f'素材叠加生成失败：{exc}') from exc
        result = probe_media(ffprobe, str(output))
        if (result.width != record.width or result.height != record.height
                or abs(result.duration-(source_end-source_start)) > 2/fps):
            raise RuntimeError('素材叠加缓存尺寸或时长校验失败，未替换当前剪辑。')
        if record.signature != _signature(project, record):
            raise ValueError('生成期间原片、叠加素材或防抖发生变化，请重试。')
        publish_completed(output, destination, cancel_event=cancel_event)
    _, record.cache_size, record.cache_mtime_ns = _stamp(destination)
    if progress: progress(100., '素材叠加已就绪')
    return record
