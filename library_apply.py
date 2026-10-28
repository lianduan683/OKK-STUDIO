"""Undoable application of bundled effects to selected programme ranges."""
from __future__ import annotations
import copy
import math
import threading
from tkinter import messagebox

from creative_core import Transition
from creative_render import plan_creative_segments
from effects_library import transition_mask_path
from program_edits import program_spans, split_at


class LibraryApplyMixin:
    def apply_library_mask(self, mask, duration=0.5):
        if not self._available() or self.selected_program_range is None:
            self.status.set('请先选中节目轨片段，在其末尾添加遮罩转场。')
            return False
        start, end = self.selected_program_range
        following = next((span for span in program_spans(self.project)
                          if round(span.start*self.project.fps) == round(end*self.project.fps)), None)
        if following is None:
            self.status.set('所选片段后没有片段，不能添加转场。')
            return False
        try:
            duration = float(duration)
            if not math.isfinite(duration) or not .05 <= duration <= 5:
                raise ValueError('转场时长请输入 0.05–5 秒。')
            duration = min(duration, 2*(end-start), 2*following.duration)
            candidate = copy.deepcopy(self.project)
            split_at(candidate, end)
            candidate.transitions = [t for t in candidate.transitions
                                     if round(t.time*candidate.fps) != round(end*candidate.fps)]
            candidate.transitions.append(Transition(end, 'fade', duration, transition_mask_path(mask)))
            candidate.transitions.sort(key=lambda item: item.time)
            candidate.validate()
            plan_creative_segments(candidate, candidate.segments())
        except Exception as exc:
            messagebox.showerror('应用遮罩转场', str(exc), parent=self)
            return False
        self._remember()
        self.project = candidate
        self._gpu_live_failed = False
        self._refresh()
        self.status.set(f'已应用转场：{mask.name}；正在准备准确预览，可撤销。')
        return True

    def apply_library_overlay(self, asset, opacity=1.0):
        if not self._available() or self.selected_program_range is None:
            self.status.set('请先选中节目轨中要应用特效的片段。')
            return False
        start, end = self.selected_program_range
        try:
            opacity = float(opacity)
            if not math.isfinite(opacity) or not 0 < opacity <= 1:
                raise ValueError('特效强度必须大于 0 且不超过 100%。')
            if not self.app.caps.ffmpeg_path or not self.app.caps.ffprobe_path:
                raise ValueError('FFmpeg 尚未就绪，请稍后再试。')
            from library_overlay import prepare_library_overlay
        except Exception as exc:
            messagebox.showerror('应用视频特效', str(exc), parent=self)
            return False
        self.pause()
        token = getattr(self, '_library_apply_token', 0)+1
        self._library_apply_token = token
        original = self.project
        snapshot = copy.deepcopy(original)
        ffmpeg, ffprobe = self.app.caps.ffmpeg_path, self.app.caps.ffprobe_path
        cancel = self.app.cancel_event
        cancel.clear()
        self.busy = True
        self.app._set_working(True)
        self.app._begin_shared_progress('准备特效画面…')
        self.status.set('正在后台准备原分辨率特效画面：'+asset.name)

        def progress(*values):
            self.jobs.put(('library_progress', (token, values)))

        def worker():
            try:
                result = prepare_library_overlay(snapshot, start, end, asset, opacity,
                    ffmpeg, ffprobe, cancel_event=cancel, progress=progress)
                self.jobs.put(('library_ready', (token, original, result, asset.name)))
            except Exception as exc:
                self.jobs.put(('library_error', (token, str(exc), cancel.is_set())))
        threading.Thread(target=worker, name='library-overlay', daemon=True).start()
        return True

    def remove_library_overlay(self):
        if not self._available() or self.selected_program_range is None:
            return False
        start, end = self.selected_program_range
        overlays = getattr(self.project, 'library_overlays', [])
        kept = [item for item in overlays if item.end <= start+1e-6 or item.start >= end-1e-6]
        if len(kept) == len(overlays):
            self.status.set('所选片段没有库叠加特效。')
            return False
        self._remember()
        self.project.library_overlays = kept
        self._refresh()
        self.status.set('所选范围内的库叠加特效已移除，可撤销。')
        return True

    def _handle_library_job(self, kind, payload):
        if not kind.startswith('library_'):
            return False
        if payload[0] != getattr(self, '_library_apply_token', 0):
            return True
        if kind == 'library_progress':
            values = payload[1]
            percent = max(0., min(100., float(values[0]))) if values else 0.
            phase = str(values[1]) if len(values) > 1 else '准备原分辨率特效画面'
            self.app.progress_var.set(percent)
            self.app.progress_text_var.set(f'{phase} {percent:.1f}%')
            return True
        self.busy = False
        self.app._set_working(False)
        if kind == 'library_error':
            _, detail, cancelled = payload
            self.status.set('已取消特效准备。' if cancelled else '特效准备失败：'+detail)
            self.app.progress_text_var.set('已取消' if cancelled else '特效准备失败')
            if not cancelled:
                self.app._log('特效库：'+detail)
                messagebox.showerror('特效库', detail, parent=self)
            return True
        if kind == 'library_ready':
            _, original, result, title = payload
            if original is not self.project or self.app.cancel_event.is_set():
                return True
            candidate = copy.deepcopy(self.project)
            candidate.library_overlays.append(result)
            try:
                candidate.validate()
            except Exception as exc:
                self.status.set('特效未应用：'+str(exc))
                return True
            self._remember()
            self.project = candidate
            self._refresh()
            self.seek(result.start)
            self.app.progress_var.set(100)
            self.app.progress_text_var.set('特效已就绪')
            self.status.set(f'已应用：{title}；预览和导出使用同一原分辨率效果，可撤销。')
        return True
