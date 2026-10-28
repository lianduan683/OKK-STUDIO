"""Context insertion of persistent video tracks and media at a frozen time."""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import filedialog


class TimelineInsertMixin:
    def _timeline_insert_target(self, event):
        canvas = self.timeline
        seconds = self._timeline_origin() + (canvas.canvasx(event.x)-self._timeline_left)/self._timeline_scale()
        seconds = max(0., math.floor(seconds*self.project.fps+.5)/self.project.fps)
        y = canvas.canvasy(event.y)
        camera = next((camera for camera, (top, bottom) in self._timeline_track_bounds.items()
                       if top <= y < bottom), None)
        return seconds, camera

    def _place_inserted_video_layer(self, camera, placement):
        """Inside the caller's undo transaction; front of order is topmost."""
        side, reference = placement
        order = [value for value in self.project.stacked_cameras(include_hidden=True) if value != camera]
        if reference in order and side in {'above', 'below'}:
            index = order.index(reference) + (side == 'below')
        else:
            index = len(order) if side == 'bottom' else 0
        order.insert(index, camera)
        self.project.video_stack_order = order
        self.project.video_stack_hidden = [value for value in self.project.video_stack_hidden if value != camera]
        self.project.video_stack_enabled = True

    def insert_video_track(self, placement=('top', None)):
        if not self._available(): return None
        self.pause()
        camera = max(self.project.timeline_cameras, default=0)+1
        self._remember()
        self.project.timeline_empty_tracks.append(camera)
        # An empty track must not change the existing multicam programme.
        enabled = self.project.video_stack_enabled
        self._place_inserted_video_layer(camera, placement)
        self.project.video_stack_enabled = enabled
        self._refresh()
        self.status.set(f'已插入空视频轨道 V{camera}；可在任意时间放入素材，上方轨道叠在上层。Ctrl+Z 撤销。')
        return camera

    def remove_empty_video_track(self, camera):
        if not self._available() or camera not in self.project.timeline_empty_tracks: return
        if any(clip.camera == camera for clip in self.project.clips): return
        self._remember()
        self.project.timeline_empty_tracks.remove(camera)
        self._refresh()

    def _insert_timeline_media(self, position, camera=None, placement=None):
        if not self._available(): return False
        paths = filedialog.askopenfilenames(parent=self, title='在此位置插入时间线素材',
            filetypes=[('视频', '*.mp4 *.mov *.mkv *.mxf *.avi *.mts *.m2ts *.webm'), ('所有文件', '*.*')])
        if not paths: return False
        return self.import_timeline_paths(paths, start=position, camera=camera, layer_placement=placement)

    def _append_timeline_insert_menu(self, menu, event):
        position, camera = self._timeline_insert_target(event)
        menu.add_separator()
        tracks = tk.Menu(menu, tearoff=False, bg='#292b2e', fg='#dedfe3')
        tracks.add_command(label='插入最上方（顶层）', command=lambda: self.insert_video_track(('top', None)))
        if camera is not None:
            tracks.add_command(label=f'插入 V{camera} 上方', command=lambda: self.insert_video_track(('above', camera)))
            tracks.add_command(label=f'插入 V{camera} 下方', command=lambda: self.insert_video_track(('below', camera)))
        tracks.add_command(label='插入最下方', command=lambda: self.insert_video_track(('bottom', None)))
        menu.add_cascade(label='插入视频轨道', menu=tracks)
        media = tk.Menu(menu, tearoff=False, bg='#292b2e', fg='#dedfe3')
        media.add_command(label='新建顶层轨道', command=lambda: self._insert_timeline_media(position, placement=('top', None)))
        if camera is not None:
            media.add_command(label=f'当前轨道 V{camera}', command=lambda: self._insert_timeline_media(position, camera))
            media.add_command(label=f'新轨道 · V{camera} 上方',
                command=lambda: self._insert_timeline_media(position, placement=('above', camera)))
            media.add_command(label=f'新轨道 · V{camera} 下方',
                command=lambda: self._insert_timeline_media(position, placement=('below', camera)))
        menu.add_cascade(label=f'在此位置插入素材…（{position:.3f} 秒）', menu=media)
        if camera in self.project.timeline_empty_tracks and camera not in self.project.cameras:
            menu.add_command(label=f'删除空轨道 V{camera}', command=lambda: self.remove_empty_video_track(camera))

    def _show_timeline_insert_menu(self, event):
        self.timeline.focus_set()
        self.pause()
        menu = tk.Menu(self, tearoff=False, bg='#292b2e', fg='#dedfe3', activebackground='#3c5263')
        self._append_timeline_insert_menu(menu, event)
        self.timeline_context_menu = menu
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()
        return True
