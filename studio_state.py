"""Lossless embedding of OKK's newer edit model in companion .rtxproj files."""
from dataclasses import asdict
from pathlib import Path
import copy
import math
from multicam_core import MulticamProject, CameraClip, Cut
from lossless_core import CutSegment, SegmentModel
from lossless_ffmpeg import ExternalTrack
from editor_core import format_time

_PREVIEW_SETTINGS = {'output_var', 'proxy_var', 'export_mode', 'gpu_live_var'}


def _is_saved_setting(name):
    return isinstance(name, str) and (name.startswith('delivery_') or name in _PREVIEW_SETTINGS)


def collect_okk(app):
    workspace = getattr(app, 'multicam_workspace', None)
    if workspace is None:
        return None
    base = str(Path(__file__).resolve().parent)
    project = copy.deepcopy(workspace.project)
    project.range_start, project.range_end, project.fps = workspace._read_range()
    variables = {}
    for name, value in vars(workspace).items():
        if _is_saved_setting(name) and hasattr(value, 'get'):
            variables[name] = value.get()
    return {'base_directory': base, 'project': project.to_payload(base), 'settings': variables}


def validate_okk(value):
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get('base_directory'), str):
        raise ValueError('OKK 多机位工程结构损坏')
    if not isinstance(value.get('settings', {}), dict):
        raise ValueError('OKK 交付参数结构损坏')
    for name, setting in value.get('settings', {}).items():
        if not _is_saved_setting(name) or not isinstance(setting, (str, int, float, bool)):
            raise ValueError('OKK 交付参数包含无效字段或值')
        if isinstance(setting, float) and not math.isfinite(setting):
            raise ValueError('OKK 交付参数必须是有限数值')
        if isinstance(setting, str) and '\x00' in setting:
            raise ValueError('OKK 交付参数含有无效字符')
        if name in {'proxy_var', 'gpu_live_var', 'delivery_auto_parallel_var'} and type(setting) is not bool:
            raise ValueError('OKK 预览开关必须是布尔值')
    return MulticamProject.from_payload(value.get('project'), value['base_directory'])


def restore_okk(app, value, legacy=None):
    workspace = getattr(app, 'multicam_workspace', None)
    if workspace is None:
        return []
    project = validate_okk(value) if value else MulticamProject()
    warnings = []
    if value is None and legacy:
        from exporter_core import probe_media
        items = legacy.get('sources', []) if isinstance(legacy, dict) else legacy
        for camera, item in enumerate(items, 1):
            path = item['path']
            try:
                media = probe_media(app.caps.ffprobe_path, path)
                if media.width <= 0 or media.duration <= 0:
                    raise ValueError('没有有效视频')
                # Both models place source zero at a positive timeline offset.
                project.clips.append(CameraClip(f'legacy-{camera}', camera, path, float(item.get('offset', 0)), media.duration,
                                               has_alpha=media.has_alpha))
            except Exception as exc:
                warnings.append(f'旧多机位素材未恢复：{path}：{exc}')
        if project.clips:
            project.cuts = [Cut(0, project.cameras[0])]
    workspace._reset_quick_proxy()
    workspace.stop_monitors()
    workspace.project = project
    workspace.project_path = ''
    workspace.dirty = False
    workspace.selected_program_range = None
    workspace.camera_page = 0
    workspace._camera_metadata_attempted.clear()
    workspace.grade_target = None
    workspace._grade_target_chosen = False
    workspace.sync_results.clear()
    workspace.undo_stack.clear()
    workspace.redo_stack.clear()
    workspace.current = 0.0
    workspace._loading_fields = True
    try:
        workspace.start_var.set(format_time(project.range_start))
        workspace.end_var.set(format_time(project.range_end) if project.range_end is not None else '')
        workspace.fps_var.set(f'{project.fps:g}')
        for name, setting in (value or {}).get('settings', {}).items():
            if _is_saved_setting(name):
                variable = getattr(workspace, name, None)
                if hasattr(variable, 'set'):
                    variable.set(setting)
    finally:
        workspace._loading_fields = False
    workspace.project_label.configure(text='完整工程中的多机位剪辑')
    workspace._refresh()
    warnings.extend(f'多机位素材离线：{clip.path}' for clip in project.clips if not Path(clip.path).is_file())
    return warnings


def collect_lossless_edits(workspace):
    state = workspace.state
    return {'segments': [asdict(s) for s in state.model.segments],
            'active_id': state.active_id,
            'track_selection': {str(key): value for key, value in state.track_selection.items()},
            'media_duration': state.duration or None,
            'external_tracks': [asdict(track) for track in state.external_tracks]}


def validate_lossless_edits(data):
    if not isinstance(data, dict):
        raise ValueError('无损分段设置必须是对象')
    if 'segments' not in data:
        return None
    items = data['segments']
    if not isinstance(items, list) or len(items) > 100000:
        raise ValueError('无损分段列表无效')
    model = SegmentModel([CutSegment(**item) for item in items], media_duration=data.get('media_duration'))
    tracks = data.get('track_selection', {})
    if not isinstance(tracks, dict) or any(not str(key).isdigit() or not isinstance(v, bool) for key,v in tracks.items()):
        raise ValueError('无损轨道选择无效')
    external = data.get('external_tracks', [])
    if not isinstance(external, list) or len(external) > 10000:
        raise ValueError('无损外部轨道列表无效')
    for item in external:
        if not isinstance(item, dict) or not isinstance(item.get('path'), str) or '\x00' in item['path']:
            raise ValueError('无损外部轨道路径无效')
        ExternalTrack(**item)
    return model


def restore_lossless_edits(workspace, data):
    model = validate_lossless_edits(data)
    if model is None:
        return
    state = workspace.state
    state.model = model
    active = data.get('active_id')
    state.active_id = active if any(s.id == active for s in model.segments) else (model.segments[0].id if model.segments else None)
    state.track_selection = {int(k): v for k,v in data.get('track_selection', {}).items()}
    state.external_tracks = [ExternalTrack(**item) for item in data.get('external_tracks', [])]
    # A pending media probe must not replace edits embedded in this project.
    state.edit_generation += 1
    workspace.refresh()
