"""Undoable timeline layer order while retaining the existing multicam edit."""
from __future__ import annotations

import tkinter as tk
from video_stack import prune_stack


class VideoStackUIMixin:
    def _init_video_stack_ui(self):
        self.video_stack_var = tk.BooleanVar(self, value=False)
        self._video_stack_drag = None

    def _build_video_stack_control(self, parent):
        self.video_stack_button = tk.Checkbutton(parent, text='图层叠加',
            variable=self.video_stack_var, command=self._toggle_video_stack,
            bg='#292b2e', fg='#73aa9c', activebackground='#292b2e',
            activeforeground='#dedfe3', selectcolor='#18191b', font=('Microsoft YaHei UI', 9))
        self.video_stack_button.pack(side='left', padx=self._px(3))

    def _refresh_video_stack_ui(self):
        prune_stack(self.project)
        self.video_stack_var.set(self.project.video_stack_enabled)

    def _toggle_video_stack(self):
        value = bool(self.video_stack_var.get())
        if not self._available():
            self.video_stack_var.set(self.project.video_stack_enabled)
            return
        if value == self.project.video_stack_enabled: return
        self._remember()
        self.project.video_stack_enabled = value
        if value:
            self.project.video_stack_order = self.project.stacked_cameras(include_hidden=True)
        self._refresh()
        self.status.set('图层叠加已开启：上方轨道在画面上层；拖动左侧轨道标签可调序，右键可隐藏。'
                        if value else '已返回多机位切换；原有节目切点和图层顺序均已保留。')

    def _activate_alpha_layers(self, cameras):
        """Called inside an existing import/drag undo transaction."""
        cameras = list(dict.fromkeys(cameras))
        order = self.project.stacked_cameras(include_hidden=True)
        self.project.video_stack_enabled = True
        self.project.video_stack_order = cameras + [camera for camera in order if camera not in cameras]
        self.project.video_stack_hidden = [camera for camera in self.project.video_stack_hidden if camera not in cameras]

    def _timeline_camera_order(self):
        return self.project.stacked_cameras(include_hidden=True)

    def _stack_track_at(self, event, *, label_only=True):
        if label_only and not 0 <= event.x < self._timeline_left: return None
        y = self.timeline.canvasy(event.y)
        return next((camera for camera, (top, bottom) in self._timeline_track_bounds.items()
                     if top <= y < bottom), None)

    def _draw_stack_track_header(self, camera, top, height, font):
        left = self.timeline.canvasx(0)
        hidden = camera in self.project.video_stack_hidden
        alpha = any(clip.camera == camera and clip.has_alpha for clip in self.project.clips)
        text = f'V{camera}' + (' 隐藏' if hidden else ' α' if alpha else '')
        self.timeline.create_rectangle(left, top, left+self._timeline_left-2, top+height,
            fill='#25272a', outline='#3b4045', tags=('stack-header',))
        self.timeline.create_text(left+self._px(8), top+height/2, text=text,
            fill='#73777e' if hidden else '#73aa9c', anchor='w', font=font, tags=('stack-header',))

    def move_video_layer(self, camera, direction):
        if not self._available(): return False
        order = self.project.stacked_cameras(include_hidden=True)
        if camera not in order: return False
        index = order.index(camera)
        target = 0 if direction == 'top' else len(order)-1 if direction == 'bottom' else index+int(direction)
        target = max(0, min(len(order)-1, target))
        if target == index: return False
        order.insert(target, order.pop(index))
        self._remember(); self.project.video_stack_order = order
        self._refresh()
        self.status.set(f'V{camera} 图层顺序已调整；上方轨道在画面上层。')
        return True

    def toggle_video_layer(self, camera):
        if not self._available() or camera not in self.project.timeline_cameras: return False
        self._remember()
        hidden = self.project.video_stack_hidden
        if camera in hidden: hidden.remove(camera)
        else: hidden.append(camera)
        self._refresh()
        self.status.set(f'V{camera} 已' + ('隐藏' if camera in hidden else '显示') + '；原片和切点保留，可撤销。')
        return True

    def _video_stack_context(self, event):
        camera = self._stack_track_at(event, label_only=True)
        if camera is None or not self._available(): return False
        self.pause()
        order = self.project.stacked_cameras(include_hidden=True)
        index = order.index(camera)
        menu = tk.Menu(self, tearoff=False, bg='#292b2e', fg='#dedfe3', activebackground='#3c5263')
        for label, direction, enabled in (('图层上移', -1, index > 0),
                ('图层下移', 1, index < len(order)-1), ('置于最上层', 'top', index > 0),
                ('置于最下层', 'bottom', index < len(order)-1)):
            menu.add_command(label=label, state='normal' if enabled else 'disabled',
                             command=lambda value=direction: self.move_video_layer(camera, value))
        menu.add_separator()
        menu.add_command(label='显示图层' if camera in self.project.video_stack_hidden else '隐藏图层',
                         command=lambda: self.toggle_video_layer(camera))
        self._append_timeline_insert_menu(menu, event)
        self.video_stack_context_menu = menu
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()
        return True

    def _begin_video_stack_drag(self, event):
        camera = self._stack_track_at(event)
        if camera is None: return False
        self.pause()
        self._video_stack_drag = {'project': self.project, 'camera': camera,
            'order': self.project.stacked_cameras(include_hidden=True),
            'y': self.timeline.canvasy(event.y), 'target': camera, 'moved': False}
        self.timeline.configure(cursor='fleur')
        self.status.set(f'拖动 V{camera} 标签调整图层；上方在画面上层，Esc 取消。')
        return True

    def _move_video_stack_drag(self, event):
        drag = self._video_stack_drag
        if drag is None: return False
        if drag['project'] is not self.project or drag['order'] != self.project.stacked_cameras(include_hidden=True):
            self._end_video_stack_drag(cancel=True)
            self.status.set('图层已变化，请重新拖动轨道标签。')
            return True
        y = self.timeline.canvasy(event.y)
        if abs(y-drag['y']) > self._px(3): drag['moved'] = True
        target = min(self._timeline_track_bounds,
            key=lambda camera: abs(sum(self._timeline_track_bounds[camera])/2-y), default=None)
        if target is not None: drag['target'] = target
        self.timeline.delete('stack-insertion')
        if target is not None and drag['moved']:
            top, bottom = self._timeline_track_bounds[target]
            line_y = bottom if drag['order'].index(target) > drag['order'].index(drag['camera']) else top
            left = self.timeline.canvasx(0)
            self.timeline.create_line(left, line_y, left+self.timeline.winfo_width(), line_y,
                fill='#58a6d6', width=self._px(3), tags=('stack-insertion',))
        return True

    def _end_video_stack_drag(self, *, cancel=False):
        drag, self._video_stack_drag = self._video_stack_drag, None
        if drag is None: return False
        self.timeline.delete('stack-insertion'); self.timeline.configure(cursor='')
        if cancel or not drag['moved'] or drag['target'] == drag['camera']: return True
        if drag['project'] is not self.project or drag['order'] != self.project.stacked_cameras(include_hidden=True):
            self.status.set('图层已变化，请重新拖动轨道标签。'); return True
        target = drag['order'].index(drag['target'])
        source = drag['order'].index(drag['camera'])
        self.move_video_layer(drag['camera'], target-source)
        return True

    def _video_stack_hover(self, event):
        if self._video_stack_drag is not None: return True
        camera = self._stack_track_at(event)
        if camera is None: return False
        self.timeline.configure(cursor='fleur')
        self.status.set(f'V{camera} · 拖动标签调序，右键显示/隐藏；上方轨道在画面上层。')
        return True
