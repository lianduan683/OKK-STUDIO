"""Frame-accurate paused scrubbing and bounded keyboard jog controls."""
from __future__ import annotations
import math
import threading
import time
import tkinter as tk
from tkinter import ttk


class TransportControlsMixin:
    HOLD_DELAY_MS = 250
    JOG_TICK_MS = 33

    def _init_transport_controls(self):
        self._transport_request_id = 0
        self._transport_hold_after = None
        self._transport_held_direction = 0
        self._transport_hold_frame = 0
        self._transport_hold_anchor = 0.
        self._timeline_scrubbing = False
        self._cache_unload_token = 0
        self._transport_retiring_players = []
        self._transport_bindings = []
        self._preview_epoch_serial = 0
        self._preview_epoch = None
        self._preview_epoch_first_request = 0

    def _bind_transport_controls(self):
        self._transport_tag = 'OKKTransport' + str(id(self))
        for key in ('Left', 'Right'):
            self.bind_class(self._transport_tag, '<KeyPress-'+key+'>', self._key)
        self._tag_transport_widgets(self)
        for event, callback in (('<KeyRelease>', self._transport_key_release),
                                ('<FocusOut>', self._transport_focus_out)):
            identifier = self.app.bind(event, callback, add='+')
            self._transport_bindings.append((event, identifier))
        self.bind('<Unmap>', lambda event: self._stop_transport_hold()
                  if event.widget is self else None, add='+')
        for surface in (self.program_surface, *self.program_views, *self.comparison_views):
            surface.bind('<Button-1>', self._focus_preview, add='+')

    @staticmethod
    def _transport_input_widget(widget):
        return isinstance(widget, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Text,
                                   tk.Spinbox, ttk.Spinbox, tk.Scale, ttk.Scale))

    def _tag_transport_widgets(self, widget):
        if not self._transport_input_widget(widget):
            tag = vars(self).get('_transport_tag')
            if tag and tag not in widget.bindtags():
                widget.bindtags((tag, *widget.bindtags()))
            for child in widget.winfo_children(): self._tag_transport_widgets(child)

    def _focus_preview(self, _event=None):
        self.timeline.focus_set()

    def _transport_focus_out(self, _event=None):
        self._stop_transport_hold()
        if self._timeline_scrubbing and '_timeline_drag_geometry' in vars(self):
            self._timeline_drag_geometry = None
        self._timeline_scrubbing = False

    def _request_transport_seek(self, position, pause=True):
        value = float(position)
        if not math.isfinite(value): raise ValueError('定位时间必须是有效数字。')
        fps = float(self.project.fps)
        value = max(0., min(float(self.project.duration), value))
        self.current = min(float(self.project.duration), math.floor(value*fps+.5)/fps)
        if pause: self.playing = False
        self._transport_request_id += 1
        if self.player:
            options = {}
            if self._preview_epoch is not None and self.project.video_stack_enabled:
                options['preview_epoch'] = self._preview_epoch
            self.player.submit('scrub', time=self.current, request_id=self._transport_request_id, pause=bool(pause), **options)
        self._show_time(); self.draw_playhead()
        return self.current

    def _accept_transport_state(self, payload):
        received = payload.get('request_id', 0)
        return type(received) is int and received >= self._transport_request_id

    def _begin_preview_epoch(self):
        if not self.project.video_stack_enabled or self.player is None: return
        if self._preview_epoch is None:
            self._preview_epoch_serial += 1
            self._preview_epoch = self._preview_epoch_serial
            self._preview_epoch_first_request = self._transport_request_id + 1

    def _finish_preview_epoch(self, *, seek=True):
        active = self._preview_epoch is not None
        self._preview_epoch = None
        self._preview_epoch_first_request = 0
        if active and seek and self.alive and self.player is not None:
            # A new ordinary request immediately fences out sampled gesture
            # pictures and requests the exact final project frame.
            self._request_transport_seek(self.current, pause=True)
        return active

    def _accept_stack_preview_frame(self, payload):
        epoch = payload.get('preview_epoch')
        if epoch is None: return self._accept_transport_state(payload)
        request = payload.get('request_id')
        return (self.project.video_stack_enabled and self._preview_epoch is not None
                and type(epoch) is int and epoch == self._preview_epoch
                and type(request) is int
                and self._preview_epoch_first_request <= request <= self._transport_request_id)

    def _last_transport_frame(self):
        return max(0, math.ceil(self.project.duration*self.project.fps-1e-8)-1)

    def step_frame(self, direction):
        if not self._available() or self.project.duration <= 0: return False
        if not self._can_start_transport_monitors(): return False
        direction = 1 if direction > 0 else -1
        frame = math.floor(self.current*self.project.fps+.5)
        frame = max(0, min(self._last_transport_frame(), frame+direction))
        if self.player is None: self.start_monitors()
        self._request_transport_seek(frame/self.project.fps, pause=True)
        return True

    def _transport_key_press(self, direction):
        direction = 1 if direction > 0 else -1
        if direction == self._transport_held_direction: return 'break'
        self._stop_transport_hold()
        if not self.step_frame(direction): return 'break'
        self._transport_held_direction = direction
        self._transport_hold_frame = math.floor(self.current*self.project.fps+.5)
        self._transport_hold_anchor = time.monotonic()+self.HOLD_DELAY_MS/1000.
        self._transport_hold_after = self.after(self.HOLD_DELAY_MS, self._transport_hold_tick)
        return 'break'

    def _transport_hold_tick(self):
        self._transport_hold_after = None
        direction = self._transport_held_direction
        if not direction or not self.alive or not self.winfo_ismapped() or not self._available():
            self._stop_transport_hold(); return
        if self._transport_input_widget(self.focus_get()):
            self._stop_transport_hold(); return
        # Elapsed time determines the latest desired frame; slow rendering
        # can omit intermediate previews but never accumulates key repeats.
        elapsed = max(0., time.monotonic()-self._transport_hold_anchor)
        advanced = max(1, math.floor(elapsed*self.project.fps)+1)
        target = max(0, min(self._last_transport_frame(), self._transport_hold_frame+direction*advanced))
        if abs(self.current*self.project.fps-target) > .1:
            self._begin_preview_epoch()
            self._request_transport_seek(target/self.project.fps, pause=True)
        if target in (0, self._last_transport_frame()): return
        self._transport_hold_after = self.after(self.JOG_TICK_MS, self._transport_hold_tick)

    def _transport_key_release(self, event):
        direction = {'Left': -1, 'Right': 1}.get(event.keysym)
        if direction == self._transport_held_direction:
            self._stop_transport_hold(); return 'break'

    def _stop_transport_hold(self, *, finalize=True):
        pending = self._transport_hold_after
        self._transport_hold_after = None
        self._transport_held_direction = 0
        if pending is not None:
            try: self.after_cancel(pending)
            except tk.TclError: pass
        self._finish_preview_epoch(seek=finalize)

    def _reset_transport_controls(self):
        self._stop_transport_hold(finalize=False)
        self._timeline_scrubbing = False
        self._transport_request_id = 0

    def _close_transport_controls(self):
        self._stop_transport_hold(finalize=False)
        self._cache_unload_token += 1
        for player in self._transport_retiring_players:
            player.close()
        for event, identifier in self._transport_bindings:
            try: self.app.unbind(event, identifier)
            except tk.TclError: pass
        self._transport_bindings.clear()
        tag = vars(self).get('_transport_tag')
        if tag:
            for key in ('Left', 'Right'): self.unbind_class(tag, '<KeyPress-'+key+'>')

    def unload_preview_cache(self):
        if not self._available(): return False
        self._stop_transport_hold()
        previous, position, project = self.player, self.current, self.project
        self.stop_monitors()
        # Retain an owner through timeouts and failed native shutdowns. A
        # second click retries those resources instead of opening another set.
        if previous is not None: self._track_stopped_player(previous)
        pending = tuple(self._transport_retiring_players)
        if not pending:
            self.status.set('当前没有加载监看缓存；开始监看时会重新读取画面。')
            return True
        self._cache_unload_token += 1
        token = self._cache_unload_token
        self.busy = True
        self.status.set('正在释放预览解码缓存和显存，保留原片、代理与剪辑…')
        jobs = self.jobs
        def release():
            try:
                for player in pending:
                    player.close()
                    if not player.wait_closed(timeout=10):
                        raise RuntimeError('播放器仍在释放资源；旧资源退出前不会新建监看，可稍后重试释放缓存。')
                jobs.put(('transport_cache_released', (token, project, position, '')))
            except Exception as error:
                jobs.put(('transport_cache_released', (token, project, position, str(error))))
        threading.Thread(target=release, name='release-preview-cache', daemon=False).start()
        return True

    def _track_stopped_player(self, player):
        if callable(getattr(player, 'wait_closed', None)) and player not in self._transport_retiring_players:
            self._transport_retiring_players.append(player)

    def _can_start_transport_monitors(self, report=True):
        pending = []
        errors = []
        for player in self._transport_retiring_players:
            try:
                if not player.wait_closed(timeout=0): pending.append(player)
            except Exception as error:
                pending.append(player)
                errors.append(str(error))
        self._transport_retiring_players = pending
        if pending:
            if report:
                detail = ('；'.join(errors) + '。请重试释放缓存。') if errors else '请等待旧播放器退出后再开启监看。'
                self.status.set('预览资源尚未释放：' + detail)
            return False
        retired = vars(self).get('_retired_camera_frames')
        if retired and self.player is None: self._release_camera_frames(list(retired))
        return True

    def _handle_transport_job(self, kind, payload):
        if kind != 'transport_cache_released': return False
        token, project, position, error = payload
        if token != self._cache_unload_token or not self.alive: return True
        self.busy = False
        if error:
            self.status.set('释放预览缓存：'+error)
            self.app._log(error)
        elif self._can_start_transport_monitors() and project is self.project:
            self.current = min(self.project.duration, position)
            self.start_monitors()
            self._request_transport_seek(self.current, pause=True)
            self.status.set('已释放预览缓存并重新加载当前帧；保持暂停。')
        return True
