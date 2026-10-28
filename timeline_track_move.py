"""Validate vertical drag targets without copying projects on pointer motion."""


def track_move_targets(project, gesture, y):
    order = gesture['track_order']
    original = gesture['clip_cameras']
    anchor = gesture['anchor_camera']
    if not original or anchor not in order: return original
    bounds = gesture['track_bounds']
    target = min(order, key=lambda camera: abs(sum(bounds[camera])/2-y))
    offset = order.index(target)-order.index(anchor)
    indexes = [order.index(camera) for camera in original.values()]
    offset = max(-min(indexes), min(len(order)-1-max(indexes), offset))
    return {identifier: order[order.index(camera)+offset] for identifier, camera in original.items()}


def validate_track_move(project, gesture, targets, delta):
    for clip in project.clips:
        if clip.id not in targets: continue
        if clip.camera == targets[clip.id]: continue
        start = gesture['clips'][clip.id]+delta
        for item in project.library_overlays:
            if item.source_id == clip.id or (item.camera == targets[clip.id]
                    and start < item.end-1e-8 and start+clip.duration > item.start+1e-8):
                raise ValueError('目标轨道含有库叠加处理，请先移除重叠的特效，再拖动素材。')


def apply_track_move(project, gesture, targets):
    for clip in project.clips:
        if clip.id in targets: clip.camera = targets[clip.id]
    for item in project.stabilizations:
        if item.source_id in targets: item.camera = targets[item.source_id]
    if targets != gesture['clip_cameras']:
        # Retain the emptied source rows so moving back has a stable target.
        project.timeline_empty_tracks = list(dict.fromkeys(
            project.timeline_empty_tracks + list(gesture['clip_cameras'].values())))
        project.video_stack_order = list(gesture['track_order'])
        project.video_stack_enabled = True

    elif gesture.get('track_changed'):
        project.timeline_empty_tracks = list(gesture['empty_tracks'])
        project.video_stack_order = list(gesture['stack_order'])
        project.video_stack_enabled = gesture['stack_enabled']
