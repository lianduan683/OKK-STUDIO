"""Stabilization settings and caches for non-destructive programme ranges.

The gyro backend uses the official Gyroflow executable and requires parsed
motion data and lens calibration. The image backend is loaded only when used;
it estimates motion from pictures and does not require gyro telemetry.

Public interfaces verified against Gyroflow 1.6.3's CLI and project schema:
https://docs.gyroflow.xyz/app/advanced-usage/command-line-cli
https://github.com/gyroflow/gyroflow/tree/v1.6.3/src
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field, fields, replace
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import unquote, urlparse

from exporter_core import CREATE_NO_WINDOW, ExportCancelled, probe_media
from cache_paths import cache_dir


@dataclass(slots=True)
class StabilizationSettings:
    mode: str = 'auto'
    smoothness: float = .5
    fov: float = 1.0
    zoom_mode: str = 'dynamic'
    zoom_speed: float = 4.0
    horizon_lock: float = 0.0
    horizon_roll: float = 0.0
    rolling_shutter_ms: float = 0.0
    gyro_path: str = ''
    lens_path: str = ''
    project_path: str = ''
    gyroflow_path: str = ''
    auto_sync: bool = True
    sync_search_size: float = 3.0
    sync_offset: float = 0.0
    engine: str = 'gyroflow'
    image_smoothing: float = .5
    image_shakiness: int = 5
    image_zoom: bool = True
    image_tripod: bool = False
    image_model: str = 'similarity'
    image_quality: str = 'fine'


@dataclass(slots=True)
class StabilizedRange:
    start: float
    end: float
    camera: int
    source_id: str
    settings: StabilizationSettings = field(default_factory=StabilizationSettings)
    cache_path: str = ''
    signature: str = ''
    source_start: float = 0.0
    source_end: float = 0.0


@dataclass(slots=True)
class GyroflowProbe:
    has_gyro: bool = False
    has_lens: bool = False
    motion_samples: int = 0
    source_name: str = ''
    has_accurate_timestamps: bool = False
    requires_sync: bool = False
    project_path: str = ''
    lens_name: str = ''
    duration: float = 0.0
    fps: float = 0.0
    width: int = 0
    height: int = 0
    problems: tuple[str, ...] = ()


@dataclass(slots=True)
class StabilizationResult:
    cache_path: str
    signature: str
    source_start: float
    source_end: float
    duration: float
    fps: float
    width: int
    height: int
    probe: GyroflowProbe


PATH_FIELDS = ('gyro_path', 'lens_path', 'project_path', 'gyroflow_path')
IMAGE_FIELDS = ('engine','image_smoothing','image_shakiness','image_zoom','image_tripod','image_model','image_quality')
_PERCENT = re.compile(r'\((\d+(?:\.\d+)?)%\)|progress:\s*(\d+(?:\.\d+)?)%', re.I)


def default_settings(mode='auto') -> StabilizationSettings:
    result = StabilizationSettings(mode=mode)
    validate_settings(result)
    return result


def _number(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label}必须在 {low:g} 到 {high:g} 之间')
    return float(value)


def _path(value, label, check_files=False):
    if not isinstance(value, str) or any(char in value for char in '\x00\r\n'):
        raise ValueError(f'{label}路径无效')
    if value and check_files and not Path(value).is_file():
        raise ValueError(f'找不到{label}：{value}')
    return value


def validate_settings(settings, check_files=False):
    if not isinstance(settings, StabilizationSettings):
        raise ValueError('Gyroflow 防抖设置结构无效')
    if settings.engine not in ('gyroflow','image'):
        raise ValueError('防抖引擎无效')
    _number(settings.image_smoothing,'画面防抖平滑时间',0,5)
    if type(settings.image_shakiness) is not int or not 1<=settings.image_shakiness<=10:
        raise ValueError('画面防抖抖动程度必须是 1 到 10 的整数')
    if type(settings.image_zoom) is not bool or type(settings.image_tripod) is not bool:
        raise ValueError('画面防抖缩放与机位锁定必须是布尔值')
    if settings.image_model not in ('similarity','translation'):
        raise ValueError('画面防抖运动模型无效')
    if settings.image_quality not in ('fast', 'fine'):
        raise ValueError('画面防抖处理质量必须是 fast 或 fine')
    if settings.mode not in ('auto', 'manual'):
        raise ValueError('防抖设置模式无效')
    if settings.zoom_mode not in ('dynamic', 'static', 'none'):
        raise ValueError('缩放模式无效')
    for name, label, low, high in (
        ('smoothness', '平滑强度', 0, 5), ('fov', '视野 FOV', .1, 3),
        ('zoom_speed', '动态缩放速度', .1, 60), ('horizon_lock', '水平锁定', 0, 100),
        ('horizon_roll', '水平滚转', -180, 180), ('rolling_shutter_ms', '传感器读出时间', -100, 100),
        ('sync_search_size', '同步搜索范围', .01, 60), ('sync_offset', '陀螺仪时间偏移', -3600, 3600),
    ):
        _number(getattr(settings, name), label, low, high)
    if type(settings.auto_sync) is not bool:
        raise ValueError('自动同步必须是布尔值')
    for name in PATH_FIELDS:
        _path(getattr(settings, name), name, check_files and settings.engine=='gyroflow')
    if settings.engine=='gyroflow' and settings.project_path and Path(settings.project_path).suffix.lower() != '.gyroflow':
        raise ValueError('工程文件必须是 .gyroflow')
    if settings.engine=='gyroflow' and settings.lens_path and Path(settings.lens_path).suffix.lower() != '.json':
        raise ValueError('镜头配置必须是 Gyroflow 的 .json 文件')


def settings_to_dict(settings, relative=None):
    validate_settings(settings)
    result = asdict(settings)
    if relative:
        for name in PATH_FIELDS:
            if result[name]: result[name] = relative(result[name])
    return result


def settings_from_dict(value, absolute=None):
    if not isinstance(value, dict) or set(value)-{item.name for item in fields(StabilizationSettings)}:
        raise ValueError('Gyroflow 设置包含未知字段')
    result = dict(value)
    if absolute:
        for name in PATH_FIELDS:
            if result.get(name): result[name] = absolute(result[name])
    settings = StabilizationSettings(**result)
    validate_settings(settings)
    return settings


def validate_range(record, check_files=False):
    if not isinstance(record, StabilizedRange): raise ValueError('片段防抖结构无效')
    for name in ('start', 'end', 'source_start', 'source_end'):
        _number(getattr(record, name), '片段时间', 0, 1e12)
    if record.end <= record.start or record.source_end <= record.source_start:
        raise ValueError('防抖片段的结束必须晚于开始')
    if type(record.camera) is not int or record.camera < 1:
        raise ValueError('防抖机位编号无效')
    if not isinstance(record.source_id, str) or not record.source_id or len(record.source_id) > 1000:
        raise ValueError('防抖原片标识无效')
    if not record.cache_path: raise ValueError('防抖片段缺少缓存文件')
    _path(record.cache_path, '防抖缓存', check_files)
    if not isinstance(record.signature, str) or not re.fullmatch(r'[0-9a-f]{64}', record.signature):
        raise ValueError('防抖缓存签名无效')
    validate_settings(record.settings, check_files=False)


def range_to_dict(record, relative=None):
    validate_range(record)
    result = asdict(record)
    result['settings'] = settings_to_dict(record.settings, relative)
    if result['cache_path'] and relative: result['cache_path'] = relative(result['cache_path'])
    return result


def range_from_dict(value, absolute=None):
    if not isinstance(value, dict) or set(value)-{item.name for item in fields(StabilizedRange)}:
        raise ValueError('防抖片段包含未知字段')
    result = dict(value)
    result['settings'] = settings_from_dict(result.get('settings', {}), absolute)
    if result.get('cache_path') and absolute: result['cache_path'] = absolute(result['cache_path'])
    try: record = StabilizedRange(**result)
    except TypeError as exc: raise ValueError('防抖片段缺少必要字段') from exc
    validate_range(record)
    return record


def find_gyroflow(preferred='', app_dir=None):
    candidates = [preferred]
    base = Path(app_dir) if app_dir else Path(__file__).resolve().parent
    candidates += [str(base/'bin'/'gyroflow'/'Gyroflow.exe'), str(base/'bin'/'Gyroflow.exe'),
                   str(base/'Gyroflow.exe')]
    found = shutil.which('Gyroflow') or shutil.which('Gyroflow.exe')
    if found: candidates.append(found)
    for value in candidates:
        if value and Path(value).is_file(): return str(Path(value).resolve())
    raise ValueError('未找到 Gyroflow.exe，请在防抖设置中指定官方 Gyroflow 程序。')


def _stamp(value):
    if not value: return None
    path = Path(value).resolve()
    try:
        stat = path.stat()
        return (str(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return (str(path), 'missing')


def _effective_project(source, settings):
    if settings.project_path: return settings.project_path
    sidecar = Path(source).with_suffix('.gyroflow')
    return str(sidecar) if sidecar.is_file() else ''


def cache_signature(source_path, source_start, source_end, settings):
    validate_settings(settings)
    start = _number(source_start, '原片开始', 0, 1e12)
    end = _number(source_end, '原片结束', 0, 1e12)
    if end <= start: raise ValueError('原片时间范围无效')
    if settings.engine=='image':
        from image_stabilization import image_cache_signature
        return image_cache_signature(source_path,start,end,settings)
    # Keep the original Gyroflow payload byte-for-byte compatible. Image
    # options are inert here and must not expire an existing Gyroflow cache.
    gyro_settings=settings_to_dict(settings)
    for name in IMAGE_FIELDS:gyro_settings.pop(name,None)
    payload = {'algorithm': 'Gyroflow CLI range cache v1', 'source': _stamp(source_path),
               'range': [round(start, 9), round(end, 9)], 'settings': gyro_settings,
               'dependencies': {name: _stamp(getattr(settings, name)) for name in PATH_FIELDS},
               'automatic_project': _stamp(_effective_project(source_path, settings))}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def cache_matches(record, source_path):
    try:
        validate_range(record, check_files=True)
        if not Path(source_path).is_file(): return False
        return record.signature == cache_signature(source_path, record.source_start, record.source_end, record.settings)
    except (OSError, ValueError): return False


def build_preset(settings, source_start=0.0, source_end=None):
    validate_settings(settings)
    zoom = settings.zoom_speed if settings.zoom_mode == 'dynamic' else -1.0 if settings.zoom_mode == 'static' else 0.0
    result = {'version': 3, 'stabilization': {
        'method': 'Default', 'fov': settings.fov,
        'smoothing_params': [{'name': 'smoothness', 'value': settings.smoothness},
                             {'name': 'trim_range_only', 'value': 1.0}],
        'adaptive_zoom_window': zoom, 'horizon_lock_amount': settings.horizon_lock,
        'horizon_lock_roll': settings.horizon_roll,
        'video_speed': 1.0, 'video_speed_affects_smoothing': True,
    }, 'keyframes': {}}
    if settings.mode == 'manual' or settings.rolling_shutter_ms:
        result['stabilization'].update(frame_readout_time=abs(settings.rolling_shutter_ms),
            frame_readout_direction='BottomToTop' if settings.rolling_shutter_ms < 0 else 'TopToBottom')
    if source_end is not None:
        start = _number(source_start, '原片开始', 0, 1e12)
        end = _number(source_end, '原片结束', 0, 1e12)
        if end <= start: raise ValueError('原片时间范围无效')
        result['trim_ranges_ms'] = [[start*1000.0, end*1000.0]]
    if not settings.auto_sync:
        result['offsets'] = {'0': settings.sync_offset*1000.0}
    return result


def _json_arg(value):
    # Gyroflow 1.6.3 also accepts single-quoted pseudo-JSON by replacing every
    # apostrophe. Escape real apostrophes in paths before that replacement.
    return json.dumps(value, ensure_ascii=True, separators=(',', ':')).replace("'", '\\u0027')


def build_command(executable, input_path, output_path, settings, source_start=0.0, source_end=None,
                  *, preset_path=None, width=None, height=None, bit_depth=8,
                  export_metadata=None, export_project=0, synchronize=None, source_path=None):
    validate_settings(settings)
    output = Path(output_path).resolve()
    if output == Path(input_path).resolve() or (source_path and output == Path(source_path).resolve()):
        raise ValueError('防抖输出不能覆盖原片或工程')
    out = {'codec': 'H.265/HEVC', 'output_path': output.as_posix(), 'use_gpu': True,
           'audio': False, 'bitrate': 150, 'pixel_format': 'P010LE' if bit_depth > 8 else 'YUV420P',
           'keyframe_distance': 1, 'preserve_other_tracks': False, 'pad_with_black': False,
           'export_trims_separately': False, 'interpolation': 'Lanczos4'}
    if width and height: out.update(output_width=int(width), output_height=int(height))
    sync = {'do_autosync': bool(settings.auto_sync), 'search_size': settings.sync_search_size,
            'initial_offset': settings.sync_offset, 'processing_resolution': 720,
            'max_sync_points': 3, 'auto_sync_points': True}
    if synchronize is not None: sync['do_autosync'] = bool(synchronize)
    inputs = [str(Path(input_path).resolve())]
    if source_path and Path(source_path).resolve() != Path(input_path).resolve():
        inputs.insert(0, str(Path(source_path).resolve()))
    command = [str(executable), *inputs, '-f', '-j', '1', '-d', '0',
               '--stdout-progress', '-p', _json_arg(out), '-s', _json_arg(sync)]
    if settings.lens_path: command.append(str(Path(settings.lens_path).resolve()))
    if settings.gyro_path: command += ['-g', str(Path(settings.gyro_path).resolve())]
    command += ['--preset', str(Path(preset_path).resolve()) if preset_path else _json_arg(build_preset(settings, source_start, source_end))]
    if export_metadata: command += ['--export-metadata', '2:'+str(Path(export_metadata).resolve())]
    if export_project: command += ['--export-project', str(int(export_project))]
    return command


def _run(command, *, cancel_event=None, progress=None, log=None, timeout=None):
    if cancel_event is not None and cancel_event.is_set(): raise ExportCancelled('已取消 Gyroflow 防抖')
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    lines = queue.Queue()
    tail = deque(maxlen=70)
    def read():
        try:
            for raw in iter(process.stdout.readline, b''):
                lines.put(raw.decode('utf-8', 'replace').rstrip())
        finally: lines.put(None)
    reader = threading.Thread(target=read, name='gyroflow-stdout', daemon=True)
    reader.start()
    began = time.monotonic()
    eof = False
    try:
        while not eof or process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                try: process.wait(3)
                except subprocess.TimeoutExpired: process.kill(); process.wait(3)
                raise ExportCancelled('已取消 Gyroflow 防抖')
            if timeout and time.monotonic()-began > timeout:
                process.kill(); process.wait(3)
                raise RuntimeError('Gyroflow 检测超时，请检查程序、素材和陀螺仪文件。')
            try: value = lines.get(timeout=.1)
            except queue.Empty: continue
            if value is None:
                eof = True
                continue
            tail.append(value)
            if log: log(value)
            match = _PERCENT.search(value)
            if match and progress:
                percent = float(next(part for part in match.groups() if part is not None))
                phase = '同步陀螺仪' if 'Synchronization' in value else 'Gyroflow 防抖'
                progress(min(99.9, max(0., percent)), phase)
        process.wait()
        text = '\n'.join(tail)
        if process.returncode:
            raise RuntimeError(f'Gyroflow 退出码 {process.returncode}\n{text[-5000:]}')
        # Stable Gyroflow 1.6.3 can return 0 after a failed queue item.
        if re.search(r'Rendering failed|\[ERROR\]|An error occurr?ed|convert_format:', text, re.I):
            raise RuntimeError('Gyroflow 处理失败：\n'+text[-5000:])
        return text
    finally:
        if process.poll() is None:
            process.terminate()
            try: process.wait(3)
            except subprocess.TimeoutExpired: process.kill(); process.wait(3)
        reader.join(timeout=2)
        if process.stdout: process.stdout.close()


def _file_url(value, base):
    if not isinstance(value, str) or not value: return value
    if value.startswith('file://'):
        parts = urlparse(value)
        path = unquote(parts.path)
        if os.name == 'nt' and re.match(r'^/[A-Za-z]:', path): path = path[1:]
        if parts.netloc: path = '//'+parts.netloc+path
        return Path(path).resolve().as_uri()
    path = Path(value)
    if not path.is_absolute(): path = base/path
    return path.resolve().as_uri()


def _prepare_input(source, settings, folder):
    project_path = _effective_project(source, settings)
    if not project_path: return str(source), ''
    path = Path(project_path).resolve()
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict): raise ValueError('Gyroflow 工程必须是 JSON 对象')
    original_video = data.get('videofile')
    video_info = data.get('video_info')
    if not isinstance(original_video, str) or not original_video or not isinstance(video_info, dict):
        raise ValueError('此 .gyroflow 文件不含完整视频信息；请选择由 Gyroflow 导出的完整工程，不能使用仅有设置的预设文件。')
    original_name = Path(unquote(urlparse(original_video).path) if '://' in original_video else original_video).name
    if original_name.casefold() != Path(source).name.casefold():
        raise ValueError(f'此 Gyroflow 工程对应 {original_name}，与所选原片 {Path(source).name} 不匹配。')
    if not all(isinstance(video_info.get(key), (int,float)) and video_info[key] > 0 for key in ('width','height','fps','duration_ms')):
        raise ValueError('Gyroflow 工程的视频尺寸、帧率或时长无效，请在官方 Gyroflow 中重新保存工程。')
    # Gyroflow 1.6.3's CLI has a native job-id race when a full project is
    # the primary input together with another preset. Keep the real video
    # as the job, and apply only the project's gyro/lens/settings as a
    # preset. This also keeps all output paths under our job directory.
    for key in ('videofile', 'video_info', 'output'): data.pop(key, None)
    gyro = data.get('gyro_source')
    if isinstance(gyro, dict) and gyro.get('filepath'):
        gyro['filepath'] = _file_url(gyro['filepath'], path.parent)
    prepared = Path(folder)/'input.gyroflow'
    prepared.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return str(prepared), str(path)


def _lens_valid(data):
    if not isinstance(data, dict): return False
    calibration = data.get('fisheye_params', {})
    matrix = calibration.get('camera_matrix', []) if isinstance(calibration, dict) else []
    dimension = data.get('calib_dimension', {})
    try:
        return (len(matrix) == 3 and all(len(row) == 3 for row in matrix)
                and float(matrix[0][0]) > 0 and float(matrix[1][1]) > 0
                and dimension.get('w', 0) > 0 and dimension.get('h', 0) > 0)
    except (ValueError, TypeError, IndexError): return False


def _probe_in_folder(source, settings, executable, ffprobe_path, folder, *, cancel_event=None, log=None):
    media = probe_media(ffprobe_path, str(source))
    input_path, project_path = _prepare_input(source, settings, folder)
    metadata_path = Path(folder)/'motion.json'
    # Probe metadata and lens matching using the real application. Detection
    # itself does not run an expensive optical synchronization analysis.
    metadata_command = build_command(executable, input_path, Path(folder)/'probe.mp4', settings,
        export_metadata=metadata_path, width=media.width, height=media.height, bit_depth=media.bit_depth,
        synchronize=False, source_path=source)
    _run(metadata_command, cancel_event=cancel_event, log=log, timeout=120)
    if not metadata_path.is_file(): raise RuntimeError('Gyroflow 没有输出陀螺仪检测数据。')
    metadata = json.loads(metadata_path.read_text(encoding='utf-8-sig'))
    raw = metadata.get('raw_imu', []) if isinstance(metadata, dict) else []
    quats = metadata.get('quaternions', {}) if isinstance(metadata, dict) else {}
    samples = max(len(raw) if isinstance(raw, list) else 0, len(quats) if isinstance(quats, (dict,list)) else 0)
    output_path = Path(folder)/'probe.mp4'
    project_command = build_command(executable, input_path, output_path, settings,
        export_project=1, width=media.width, height=media.height, bit_depth=media.bit_depth,
        synchronize=False, source_path=source)
    _run(project_command, cancel_event=cancel_event, log=log, timeout=120)
    candidates = [Path(folder)/'probe.gyroflow', *Path(folder).glob('*.gyroflow')]
    exported = next((path for path in candidates if path.is_file() and path.name != 'input.gyroflow'), None)
    if exported is None: raise RuntimeError('Gyroflow 没有输出镜头配置检测数据。')
    project = json.loads(exported.read_text(encoding='utf-8-sig'))
    lens = project.get('calibration_data', {})
    accurate = bool(metadata.get('has_accurate_timestamps', False))
    offsets = project.get('offsets', {})
    problems = []
    if samples < 2: problems.append('未识别到可用陀螺仪数据；请导入匹配的陀螺仪文件或含运动数据的 .gyroflow 工程。')
    if not _lens_valid(lens): problems.append('未识别到镜头配置；请导入与拍摄分辨率、镜头设置匹配的 Gyroflow .json 镜头配置。')
    if settings.auto_sync is False and not accurate and not offsets:
        problems.append('此素材缺少准确同步信息，请启用自动同步或手动设置陀螺仪时间偏移。')
    probe = GyroflowProbe(has_gyro=samples >= 2, has_lens=_lens_valid(lens), motion_samples=samples,
        source_name=str(metadata.get('detected_source') or ''), has_accurate_timestamps=accurate,
        requires_sync=not accurate and not offsets, project_path=project_path,
        lens_name=str(lens.get('name') or lens.get('camera_model') or ''), duration=media.duration,
        fps=media.fps, width=media.width, height=media.height, problems=tuple(problems))
    return probe, media, input_path


def probe_source(source_path, settings=None, *, gyroflow_path='', ffprobe_path='', cancel_event=None, log=None):
    settings = settings or default_settings()
    validate_settings(settings, check_files=True)
    source = Path(source_path).resolve(strict=True)
    executable = find_gyroflow(gyroflow_path or settings.gyroflow_path)
    if not ffprobe_path: raise ValueError('未找到 ffprobe.exe，无法检测防抖素材。')
    with tempfile.TemporaryDirectory(prefix='okk_gyroflow_probe_',dir=cache_dir('stabilization/probes')) as folder:
        probe, _, _ = _probe_in_folder(source, settings, executable, ffprobe_path, folder,
                                       cancel_event=cancel_event, log=log)
        return probe


def run_stabilization(source_path, source_start, source_end, settings, output_path, *,
                      gyroflow_path='', ffprobe_path='', ffmpeg_path='', cancel_event=None, progress=None, log=None):
    validate_settings(settings, check_files=True)
    if settings.engine=='image':
        from image_stabilization import run_image_stabilization
        return run_image_stabilization(source_path,source_start,source_end,settings,output_path,
            ffprobe_path=ffprobe_path,ffmpeg_path=ffmpeg_path,cancel_event=cancel_event,
            progress=progress,log=log)
    source = Path(source_path).resolve(strict=True)
    output = Path(output_path).resolve()
    protected = {source, *(Path(getattr(settings, name)).resolve() for name in PATH_FIELDS if getattr(settings, name))}
    if output in protected: raise ValueError('防抖输出不能覆盖原片、陀螺仪、镜头配置或工程')
    if not output.parent.is_dir(): output.parent.mkdir(parents=True, exist_ok=True)
    executable = find_gyroflow(gyroflow_path or settings.gyroflow_path)
    if not ffprobe_path: raise ValueError('未找到 ffprobe.exe，无法验证防抖缓存。')
    if progress: progress(0., '检测陀螺仪与镜头配置')
    with tempfile.TemporaryDirectory(prefix='okk_gyroflow_job_', dir=output.parent) as folder:
        probe, media, input_path = _probe_in_folder(source, settings, executable, ffprobe_path, folder,
                                                  cancel_event=cancel_event, log=log)
        if probe.problems: raise ValueError('\n'.join(probe.problems))
        fps = _number(media.fps, '原片帧率', .01, 1000)
        requested_start = _number(source_start, '原片开始', 0, media.duration)
        requested_end = _number(source_end, '原片结束', 0, media.duration+1/fps)
        if requested_end <= requested_start: raise ValueError('防抖时间范围无效')
        actual_start = math.floor(requested_start*fps+1e-7)/fps
        actual_end = min(media.duration, math.ceil(requested_end*fps-1e-7)/fps)
        if actual_end <= actual_start: raise ValueError('防抖范围不足一帧')
        pending = Path(folder)/'stabilized.mp4'
        command = build_command(executable, input_path, pending, settings, actual_start, actual_end,
            width=media.width, height=media.height, bit_depth=media.bit_depth, export_project=4,
            source_path=source)
        if progress: progress(1., 'Gyroflow 防抖计算')
        _run(command, cancel_event=cancel_event, progress=progress, log=log)
        if probe.requires_sync and settings.auto_sync:
            synchronized_project = Path(folder)/'stabilized.gyroflow'
            if not synchronized_project.is_file():
                raise RuntimeError('Gyroflow 未输出同步结果，已停止应用防抖。')
            synchronized_data = json.loads(synchronized_project.read_text(encoding='utf-8-sig'))
            if not synchronized_data.get('offsets'):
                raise ValueError('Gyroflow 未找到可靠同步点；请手动设置陀螺仪时间偏移，或先在 Gyroflow 中同步后导入工程。')
        if not pending.is_file() or pending.stat().st_size < 100:
            raise RuntimeError('Gyroflow 未生成有效的防抖视频；不会把原片标记为防抖成功。')
        rendered = probe_media(ffprobe_path, str(pending))
        expected = actual_end-actual_start
        # Gyroflow 1.6.3's encoder flush can append 2–3 frames after a trim.
        # A lossless packet remux limits the cache to the requested source
        # frames; the stabilization itself remains entirely Gyroflow's.
        frame_count = max(1, math.floor(expected*fps+.5))
        if rendered.duration < expected-1.1/fps or rendered.duration > expected+5.1/fps:
            raise RuntimeError(f'防抖缓存时长不匹配：应为 {expected:.3f} 秒，实际 {rendered.duration:.3f} 秒。')
        executable_ffmpeg = ffmpeg_path or str(Path(ffprobe_path).with_name('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'))
        if not Path(executable_ffmpeg).is_file(): executable_ffmpeg = shutil.which(executable_ffmpeg) or ''
        if not executable_ffmpeg: raise ValueError('未找到 FFmpeg，无法为防抖缓存精确限制帧数。')
        normalized = Path(folder)/'normalized.mp4'
        normalize = [executable_ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(pending),
                     '-map', '0:v:0', '-map_metadata', '0', '-an', '-c:v', 'copy', '-frames:v', str(frame_count), str(normalized)]
        _run(normalize, cancel_event=cancel_event, log=log, timeout=120)
        rendered = probe_media(ffprobe_path, str(normalized))
        if abs(rendered.duration-frame_count/fps) > max(.002, .1/fps):
            raise RuntimeError('防抖缓存无法保持精确的片段帧数，已停止应用。')
        if rendered.width != media.width or rendered.height != media.height:
            raise RuntimeError('Gyroflow 改变了缓存画幅，已停止应用以免时间线预览与导出不一致。')
        if abs(rendered.fps-fps) > max(.01, fps*.001):
            raise RuntimeError('Gyroflow 改变了缓存帧率，已停止应用。')
        os.replace(normalized, output)
        signature = cache_signature(source, actual_start, actual_end, settings)
        result = StabilizationResult(str(output), signature, actual_start, actual_end,
            rendered.duration, rendered.fps, rendered.width, rendered.height, probe)
        if progress: progress(100., '所选片段防抖完成')
        return result
