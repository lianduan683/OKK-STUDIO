"""Alpha metadata and stable top-to-bottom ordering for timeline video layers."""
from __future__ import annotations


def pixel_format_has_alpha(pixel_format):
    value = str(pixel_format or '').lower()
    return (value.startswith(('yuva', 'gbrap', 'rgba', 'bgra', 'argb', 'abgr', 'ayuv'))
            or value in {'ya8', 'ya16be', 'ya16le', 'pal8'})


def stream_has_alpha(stream):
    if pixel_format_has_alpha(stream.get('pix_fmt')):
        return True
    tags = {str(key).lower(): str(value).lower() for key, value in (stream.get('tags') or {}).items()}
    return tags.get('alpha_mode') in {'1', 'true', 'yes'}


def ordered_cameras(project, *, include_hidden=False):
    available = set(project.cameras)
    result = list(dict.fromkeys(camera for camera in project.video_stack_order if camera in available))
    result.extend(camera for camera in project.cameras if camera not in result)
    if not include_hidden:
        hidden = set(project.video_stack_hidden)
        result = [camera for camera in result if camera not in hidden]
    return result


def validate_stack(project):
    if type(project.video_stack_enabled) is not bool:
        raise ValueError('图层叠加开关必须是布尔值')
    available = set(project.cameras)
    for name, values in (('图层顺序', project.video_stack_order), ('隐藏图层', project.video_stack_hidden)):
        if not isinstance(values, list) or len(values) > 100000:
            raise ValueError(name + '必须是机位列表')
        if any(type(camera) is not int or camera not in available for camera in values):
            raise ValueError(name + '引用了不存在的机位')
        if len(set(values)) != len(values):
            raise ValueError(name + '不能重复机位')


def prune_stack(project):
    """Remove deleted camera references after an explicit timeline mutation."""
    available = set(project.cameras)
    project.video_stack_order = list(dict.fromkeys(camera for camera in project.video_stack_order if camera in available))
    project.video_stack_hidden = list(dict.fromkeys(camera for camera in project.video_stack_hidden if camera in available))
