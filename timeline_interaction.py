"""Media-to-timeline interactions without replacing the existing workspace UI.

Screen coordinates are converted through Canvas.canvasx/canvasy exactly once.
Media probing runs on a worker; all project edits and Tk calls stay on Tk's
thread. Each completed drop is one undoable edit.
"""
from __future__ import annotations

import math
import json
from bisect import bisect_left
import subprocess
import threading
import uuid
from pathlib import Path
from tkinter import messagebox

from camera_import import batch_import
from editor_core import format_signed_time, probe_media_duration
from exporter_core import CREATE_NO_WINDOW, find_binary
from multicam_core import CameraClip
from program_edits import set_camera_on_range
from timeline_boundary import (boundary_limits, move_program_boundary,
                               boundary_has_library_overlay, OVERLAY_BOUNDARY_MESSAGE)


def probe_timeline_master(ffprobe, path):
    result = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'a:0',
        '-show_entries', 'stream=index', '-of', 'json', path],
        capture_output=True, text=True, encoding='utf-8', timeout=30,
        creationflags=CREATE_NO_WINDOW)
    if result.returncode or not json.loads(result.stdout).get('streams'):
        raise ValueError('主音频素材不包含可读取的音轨。')
    duration = probe_media_duration(ffprobe, path)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('主音频素材时长无效。')
    return path, duration


class TimelineInteractionMixin:
    def _init_timeline_interaction(self):
        self._media_drag = None
        self._timeline_drag_geometry = None
        self._timeline_redraw_after = None
        self._timeline_track_bounds = {}
        self._timeline_program_bounds = (0, 0)
        self._timeline_boundary_times = []
        self._program_boundary_drag = None

    def _bind_timeline_interaction(self):
        self.media_tree.bind('<ButtonPress-1>', self._media_drag_press, add='+')
        self.media_tree.bind('<B1-Motion>', self._media_drag_motion, add='+')
        self.media_tree.bind('<ButtonRelease-1>', self._media_drag_release, add='+')
        self.media_tree.bind('<Escape>', self._media_drag_cancel, add='+')
        self.timeline.bind('<ButtonPress-2>', self._timeline_pan_press)
        self.timeline.bind('<B2-Motion>', self._timeline_pan_motion)
        self.timeline.bind('<MouseWheel>', self._timeline_vertical_wheel)
        # Replace the original widget binding: Ctrl+wheel must not also scroll
        # vertically, and its zoom anchor follows the pointer.
        self.timeline.bind('<Control-MouseWheel>', self._timeline_zoom_wheel)
        self.timeline_vscroll.configure(command=self._timeline_vertical_scroll)
        self.timeline.configure(xscrollcommand=self._timeline_xview_changed,
                                yscrollcommand=self._timeline_yview_changed)
        self.timeline.bind('<Motion>', self._timeline_hover)
        self.timeline.bind('<Leave>', lambda _event: self.timeline.configure(cursor='')
                           if self._program_boundary_drag is None else None)

    def _timeline_pan_press(self, event):
        self.timeline.scan_mark(event.x, event.y)
        return 'break'

    def _timeline_pan_motion(self, event):
        self.timeline.scan_dragto(event.x, event.y, gain=1)
        self._queue_timeline_redraw()
        return 'break'

    def _timeline_vertical_wheel(self, event):
        if not event.delta:
            return 'break'
        self.timeline.yview_scroll(-3 if event.delta > 0 else 3, 'units')
        self.draw_playhead()
        self._queue_timeline_redraw()
        return 'break'

    def _timeline_vertical_scroll(self, *args):
        self.timeline.yview(*args)
        self.draw_playhead()
        self._queue_timeline_redraw()

    def _timeline_xview_changed(self, first, last):
        self.scroll.set(first, last)
        self._pin_timeline_headers()
        view = (float(first), float(last))
        if vars(self).get('_timeline_last_xview') != view:
            self._timeline_last_xview = view
            self._queue_timeline_redraw()

    def _timeline_yview_changed(self, first, last):
        self.timeline_vscroll.set(first, last)
        view = (float(first), float(last))
        if vars(self).get('_timeline_last_yview') != view:
            self._timeline_last_yview = view
            self._queue_timeline_redraw()

    def _timeline_zoom_wheel(self, event):
        if event.delta:
            self.change_zoom(2 if event.delta > 0 else .5, anchor_x=event.x)
        return 'break'

    def _timeline_boundary_at(self, event):
        x, y = self.timeline.canvasx(event.x), self.timeline.canvasy(event.y)
        if not self._timeline_program_bounds[0] <= y <= self._timeline_program_bounds[1]:
            return None
        scale, origin = self._timeline_scale(), self._timeline_origin()
        position = origin + (x-self._timeline_left)/scale
        times = self._timeline_boundary_times
        index = bisect_left(times, position)
        # Sorted programme edges make hover independent of project length.
        candidates = [(abs(self._timeline_left+(value-origin)*scale-x), value)
                      for value in times[max(0, index-1):index+1]]
        distance, position = min(candidates, default=(float('inf'), None))
        return position if distance <= self._px(6) else None

    def _timeline_hover(self, event):
        if getattr(self, '_selection_gesture', None): return
        if self._video_stack_hover(event): return
        if self._program_boundary_drag is not None:
            return
        if self.timeline_drag is not None or self.subtitle_drag is not None:
            return
        position = self._timeline_boundary_at(event)
        if position is not None and boundary_has_library_overlay(self.project, position):
            self.timeline.configure(cursor='')
            self.status.set(OVERLAY_BOUNDARY_MESSAGE)
            return
        self.timeline.configure(cursor='sb_h_double_arrow' if position is not None else '')
        if position is not None:
            self.status.set(f'拖动剪辑切点 {format_signed_time(position)} · 两侧至少保留一帧 · Esc 取消')

    def _begin_program_boundary_drag(self, event):
        position = self._timeline_boundary_at(event)
        if position is None:
            return False
        try:
            limits = boundary_limits(self.project, position)
        except ValueError as error:
            self.status.set(str(error))
            return 'blocked'
        self._program_boundary_drag = dict(original=position, current=position,
            x=self.timeline.canvasx(event.x), limits=limits, changed=False,
            selection=self.selected_program_range, dirty=self.dirty,
            redo=list(self.redo_stack), undo=list(self.undo_stack))
        self._capture_timeline_geometry()
        self.timeline.configure(cursor='sb_h_double_arrow')
        self.seek(position)
        self.status.set('拖动节目轨切点，调整两侧片段长度；Esc 取消。')
        return True

    def _move_program_boundary_drag(self, event):
        drag = self._program_boundary_drag
        if drag is None:
            return False
        delta = self.timeline.canvasx(event.x)-drag['x']
        if abs(delta) < self._px(3) and not drag['changed']:
            return True
        target = drag['original']+delta/self._timeline_scale()
        low, high = drag['limits']
        frame = max(low, min(high, math.floor(target*self.project.fps+.5)))
        if frame == math.floor(drag['current']*self.project.fps+.5):
            return True
        if not drag['changed']:
            self._remember()
            drag['changed'] = True
        previous = drag['current']
        value = move_program_boundary(self.project, previous, target, limits=drag['limits'])
        if self.selected_program_range is not None:
            self.selected_program_range = tuple(value if math.floor(point*self.project.fps+.5)
                == math.floor(previous*self.project.fps+.5) else point for point in self.selected_program_range)
        drag['current'] = value
        self.seek(value)
        self._queue_timeline_redraw()
        self.status.set(f'切点 {format_signed_time(value)} · Ctrl+Z 撤销 · Esc 取消拖动')
        return True

    def _end_program_boundary_drag(self, *, cancel=False):
        drag, self._program_boundary_drag = self._program_boundary_drag, None
        if drag is None:
            return False
        self._cancel_timeline_redraw()
        self._timeline_drag_geometry = None
        self.timeline.configure(cursor='')
        reverted = cancel or math.floor(drag['original']*self.project.fps+.5) == math.floor(drag['current']*self.project.fps+.5)
        if drag['changed'] and reverted:
            self.project = self.undo_stack.pop()
            self.undo_stack = drag['undo']
            self.redo_stack = drag['redo']
            self.dirty = drag['dirty']
            self.project_label.configure(text=(Path(self.project_path).name if self.project_path else '未保存工程')
                                         + (' *' if self.dirty else ''))
            self.selected_program_range = drag['selection']
        self._rekey_timeline_boundary(drag, reverted)
        self._refresh(reconfigure=drag['changed'])
        self.seek(drag['original'] if reverted else drag['current'])
        if drag['changed']:
            self.status.set('已取消切点拖动。' if reverted else
                            f'切点已移至 {format_signed_time(drag["current"])}；Ctrl+Z 撤销。')
        return True

    def _queue_timeline_redraw(self):
        if self._timeline_redraw_after is None:
            self._timeline_redraw_after = self.after(16, self._flush_timeline_redraw)

    def _flush_timeline_redraw(self):
        self._timeline_redraw_after = None
        if self.alive:
            self.draw_timeline()

    def _cancel_timeline_redraw(self):
        if self._timeline_redraw_after is not None:
            self.after_cancel(self._timeline_redraw_after)
            self._timeline_redraw_after = None

    def _capture_timeline_geometry(self):
        self._timeline_drag_geometry = (self._timeline_origin(), self._timeline_scale(), self.project.duration)

    def _can_move_timeline_clip(self, clip, value=None):
        if value is not None and abs(value-clip.start) < 1e-9:
            return True
        for item in getattr(self.project, 'library_overlays', ()):
            conflict = item.source_id == clip.id
            if value is not None and item.camera == clip.camera:
                conflict = conflict or (value < item.end-1e-8 and value+clip.duration > item.start+1e-8)
            if conflict:
                self.status.set('所选素材已应用或会覆盖库叠加特效，请先在特效库移除叠加效果，再移动素材或重新同步。')
                return False
        return True

    def _timeline_drop_position(self, x_root, y_root):
        canvas = self.timeline
        if not canvas.winfo_ismapped():
            return None
        x, y = x_root-canvas.winfo_rootx(), y_root-canvas.winfo_rooty()
        if not (0 <= x < canvas.winfo_width() and 0 <= y < canvas.winfo_height()):
            return None
        position = self._timeline_origin() + (canvas.canvasx(x)-self._timeline_left)/self._timeline_scale()
        position = max(0., math.floor(position*self.project.fps+.5)/self.project.fps)
        content_y = canvas.canvasy(y)
        camera = next((camera for camera, (top, bottom) in self._timeline_track_bounds.items()
                       if top <= content_y < bottom), None)
        return position, camera

    def timeline_drop_clear_hover(self):
        self.timeline.delete('media-drop')

    def timeline_drop_hover(self, x_root, y_root):
        self.timeline_drop_clear_hover()
        target = self._timeline_drop_position(x_root, y_root)
        if target is None:
            return False
        position, camera = target
        canvas = self.timeline
        x = self._timeline_x(position)
        top = canvas.canvasy(0)
        bottom = canvas.canvasy(canvas.winfo_height())
        canvas.create_line(x, top, x, bottom, fill='#58a6d6', width=self._px(2), tags='media-drop')
        label = f'放入 V{camera}' if camera is not None else '放入节目时间线'
        canvas.create_text(x+self._px(6), top+self._px(8), text=label+' · '+format_signed_time(position),
                           fill='#b9ddf2', anchor='nw', tags='media-drop')
        return True

    def timeline_drop_at(self, paths, x_root, y_root, *, master_audio=None):
        """External media-pool adapter; return whether this canvas accepted it."""
        target = self._timeline_drop_position(x_root, y_root)
        self.timeline_drop_clear_hover()
        if target is None:
            return False
        start, camera = target
        self.import_timeline_paths(paths, start=start, camera=camera, master_audio=master_audio)
        return True

    def import_timeline_paths(self, paths, *, start=None, camera=None, master_audio=None, layer_placement=None):
        """Probe a batch atomically, then place it in the current programme."""
        if not self._available():
            return False
        paths = tuple(str(path) for path in paths)
        if not paths and not master_audio:
            return False
        start = self.current if start is None else float(start)
        if not math.isfinite(start) or start < 0:
            raise ValueError('时间线起点必须是非负有限数字')
        if camera is not None and (type(camera) is not int or camera < 1):
            raise ValueError('机位编号必须是正整数')
        ffprobe = self.app.caps.ffprobe_path or find_binary('ffprobe', Path(__file__).parent)
        if not ffprobe:
            messagebox.showerror('拖入时间线', '未找到 ffprobe.exe。', parent=self)
            return False
        self.pause()
        self.busy = True
        self.app.cancel_event.clear()
        self.app._set_working(True)
        self.status.set(f'正在读取 {len(paths)} 个时间线素材…')
        project = self.project
        cancel = self.app.cancel_event

        def worker():
            try:
                assignments = batch_import(ffprobe, paths, (), cancel_event=cancel)
                master = probe_timeline_master(ffprobe, str(master_audio)) if master_audio else None
                self.jobs.put(('timeline_import_ready', (project, start, camera, [info for _, info in assignments], master, layer_placement)))
            except Exception as error:
                self.jobs.put(('timeline_import_error', (str(error), cancel.is_set())))

        threading.Thread(target=worker, name='timeline-media-import', daemon=True).start()
        return True

    def _finish_timeline_import(self, payload):
        self.busy = False
        self.app._set_working(False)
        project, start, camera, infos, *master_values = payload
        master = master_values[0] if master_values else None
        layer_placement = master_values[1] if len(master_values) > 1 else None
        if project is not self.project or self.app.cancel_event.is_set():
            self.status.set('已取消拖入，保留原时间线。')
            return
        if not infos and not master:
            return
        alpha = any(bool(getattr(info, 'has_alpha', False)) for info in infos)
        if alpha and camera in project.cameras:
            camera = max(project.timeline_cameras, default=0)+1
        stop = start + sum(info.duration for info in infos)
        if camera is not None and any(item.camera == camera and start < item.end-1e-8
            and stop > item.start+1e-8 for item in getattr(project, 'library_overlays', ())):
            self.status.set('此轨道已有库叠加特效，请先移除特效，再在该范围插入素材。')
            return
        self._remember()
        if master:
            project.master_audio, project.master_duration = master
        if not infos:
            self._refresh()
            self.status.set('已拖入连续主音频；Ctrl+Z 撤销。')
            return
        if not project.clips:
            project.fps = infos[0].fps or 30
            self.fps_var.set(f'{project.fps:.8g}')
            if start == 0 and infos[0].has_audio and not project.master_audio:
                project.master_audio = infos[0].path
                project.master_duration = infos[0].duration
        start = math.floor(start*project.fps+.5)/project.fps
        # Alpha dropped over an existing camera needs its own layer so it
        # cannot replace the background source on that same camera track.
        if alpha and camera in project.cameras:
            camera = max(project.timeline_cameras, default=0)+1
        camera = camera if camera is not None else max(project.timeline_cameras, default=0)+1
        clips = []
        position = start
        for info in infos:
            clip = CameraClip(uuid.uuid4().hex, camera, info.path, position, info.duration,
                              has_alpha=bool(getattr(info, 'has_alpha', False)))
            clips.append(clip)
            project.clips.append(clip)
            position += info.duration
        # Ordinary multicam drops become programme edits. Layered drops keep
        # that edit intact and make the new source visible via composition.
        stop = math.floor(position*project.fps+.5)/project.fps
        if layer_placement is not None:
            self._place_inserted_video_layer(camera, layer_placement)
        elif alpha: self._activate_alpha_layers([camera])
        elif camera in project.timeline_empty_tracks:
            project.video_stack_enabled = True
        if not project.cuts: project.set_cut(0, min(project.cameras))
        if stop > start and not project.video_stack_enabled:
            set_camera_on_range(project, start, stop, camera)
        self.selected_program_range = (start, stop) if stop > start else None
        self._refresh()
        self._reveal_timeline_clip(clips[0])
        self.seek(start)
        self.status.set(f'已将 {len(clips)} 个素材放入 V{camera}，可拖动微调；Ctrl+Z 撤销。')

    def _media_drag_press(self, event):
        self._media_drag = None
        if self.busy or self.app.working:
            return
        if self.media_tree.identify_region(event.x, event.y) not in {'tree', 'cell'}:
            return
        identifier = self.media_tree.identify_row(event.y)
        if any(clip.id == identifier for clip in self.project.clips):
            self._media_drag = dict(id=identifier, x=event.x_root, y=event.y_root, active=False)

    def _media_drag_motion(self, event):
        drag = self._media_drag
        if not drag:
            return
        if not drag['active']:
            if max(abs(event.x_root-drag['x']), abs(event.y_root-drag['y'])) < self._px(6):
                return
            drag['active'] = True
            self.pause()
            self.media_tree.configure(cursor='hand2')
        self.timeline_drop_hover(event.x_root, event.y_root)
        return 'break'

    def _media_drag_cancel(self, _event=None):
        drag, self._media_drag = self._media_drag, None
        self.timeline_drop_clear_hover()
        self.media_tree.configure(cursor='')
        return 'break' if drag and drag['active'] else None

    def _media_drag_release(self, event):
        drag = self._media_drag
        self._media_drag_cancel()
        if not drag or not drag['active']:
            return
        target = self._timeline_drop_position(event.x_root, event.y_root)
        if target is None or not self._available():
            return 'break'
        clip = next((clip for clip in self.project.clips if clip.id == drag['id']), None)
        if clip is None:
            return 'break'
        start, _camera = target
        if not self._can_move_timeline_clip(clip, start):
            return 'break'
        self._remember()
        self._move_clip_start(clip, start)
        if clip.has_alpha: self._activate_alpha_layers([clip.camera])
        stop = math.floor(clip.end*self.project.fps+.5)/self.project.fps
        if stop > start and not self.project.video_stack_enabled:
            set_camera_on_range(self.project, start, stop, clip.camera)
        self._refresh()
        self._reveal_timeline_clip(clip)
        self.seek(start)
        self.status.set(f'已将素材放到 {format_signed_time(start)} · V{clip.camera}；Ctrl+Z 撤销。')
        return 'break'

    def _reveal_timeline_clip(self, clip):
        self.media_tree.selection_set(clip.id)
        self.media_tree.see(clip.id)
        canvas = self.timeline
        x = self._timeline_x(clip.start)
        left, right = canvas.canvasx(0), canvas.canvasx(canvas.winfo_width())
        bounds = tuple(float(value) for value in canvas.cget('scrollregion').split())
        if x < left+self._px(12) or x > right-self._px(30):
            canvas.xview_moveto(max(0., (x-self._px(30))/max(1., bounds[2])))
        row = self._timeline_track_bounds.get(clip.camera)
        if row:
            top, bottom = canvas.canvasy(0), canvas.canvasy(canvas.winfo_height())
            if row[0] < top or row[1] > bottom:
                canvas.yview_moveto(max(0., (row[0]-self._px(8))/max(1., bounds[3])))
        self.draw_timeline()

    def _close_timeline_interaction(self):
        self._cancel_timeline_redraw()
        self._media_drag_cancel()
