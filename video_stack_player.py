"""Player integration for continuous alpha composition and bounded UI frames."""
from __future__ import annotations
import copy
import ctypes
import os
from pathlib import Path

from exporter_core import find_binary
from video_stack_preview import StackPreview


class VideoStackPlayerMixin:
    def _init_stack_preview(self):
        self._stack_enabled = False
        self._stack_preview = None
        self._stack_revision = 0
        self._stack_job_key = None
        self._stack_was_playing = False

    def take_stack_frame(self):
        preview = getattr(self, '_stack_preview', None)
        frame = preview.take_frame() if preview is not None else None
        if frame is not None and frame['request_id'] < self._submitted_request_id:
            epoch = frame.get('preview_epoch')
            if epoch is None or epoch != getattr(self, '_submitted_preview_epoch', None): return None
        return frame

    def _show_programme_native(self, visible):
        session = getattr(self, '_gpu_session', None)
        if session is None: return
        set_visible = getattr(session, 'set_visible', None)
        if callable(set_visible):
            set_visible(visible)
            return
        backend = getattr(session, '_backend', None)
        hwnd = getattr(backend, '_output_hwnd', 0)
        if os.name == 'nt' and hwnd:
            api = ctypes.WinDLL('user32', use_last_error=True)
            api.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            api.ShowWindow(hwnd, 4 if visible else 0)

    def _configure_stack_mode(self):
        enabled = bool(getattr(self._project, 'video_stack_enabled', False))
        if self._stack_preview is not None: self._stack_preview.cancel()
        self._stack_revision += 1
        self._stack_job_key = None
        if enabled == self._stack_enabled: return False
        self._stack_enabled = enabled
        if enabled:
            self._audition = None
            self._comparison_enabled = False
            for slot in (self._program, self._program_standby, self._comparison, self._comparison_standby):
                if slot is not None and slot.active and slot.controller:
                    slot.controller.command(['set_property', 'pause', True])
            self._show_programme_native(False)
        else:
            if self._stack_preview is not None: self._stack_preview.cancel()
            self._show_programme_native(True)
        # The Tk overlay receives mode before any frame or child restoration.
        self._emit('stack_mode', enabled)
        return True

    def _stack_project(self):
        project = copy.deepcopy(self._project)
        project.range_start = 0.
        project.range_end = None
        if self._grade_draft:
            target, grade = self._grade_draft
            if target[0] == 'global': project.global_grade = grade
            elif target[0] == 'camera': project.camera_grades[target[1]] = grade
            elif target[0] == 'selection':
                from program_edits import set_grade_on_range
                first, last = target[1:]
                set_grade_on_range(project, first, last, grade)
        return project

    def _sync_stack_preview(self):
        if not self._stack_enabled: return
        if self._stack_preview is None:
            app_dir = Path(self.executable).resolve().parent.parent
            ffmpeg, ffprobe = find_binary('ffmpeg', app_dir), find_binary('ffprobe', app_dir)
            if not ffmpeg or not ffprobe: raise RuntimeError('透明图层实时预览需要 FFmpeg 和 ffprobe。')
            self._stack_preview = StackPreview(ffmpeg, ffprobe, self._emit)
        preview = self._stack_preview
        preview.update_clock(self._position)
        key = (self._stack_revision, self._active_navigation_serial)
        resumable = preview.set_playing(self._playing)
        starting = self._playing and not self._stack_was_playing
        self._stack_was_playing = self._playing
        if key != self._stack_job_key or (starting and not resumable):
            if self._stack_job_key is not None and key[0] != self._stack_job_key[0]: preview.cancel()
            self._stack_job_key = key
            preview.request(self._stack_project(), self._position, self._playing, self._request_id,
                            ass_path=self._subtitle_ass_path or None, use_proxies=self._use_proxies,
                            preview_epoch=getattr(self, '_active_preview_epoch', None))
            self._emit('status', '透明图层实时预览 · 720p · CPU 合成 · 最高 30fps')

    def _resync_stack_preview(self, position):
        preview = self._stack_preview
        if preview is None: return
        if preview.refresh_presentation(position, self._playing):
            # Only stale decode work is replaced. Reuse this preview owner,
            # its source probes/proxy cache, and the separate master audio.
            preview.request(self._stack_project(), position, self._playing, self._request_id,
                ass_path=self._subtitle_ass_path or None, use_proxies=self._use_proxies,
                preview_epoch=None)
            self._stack_job_key = (self._stack_revision, self._active_navigation_serial)
            self._stack_was_playing = self._playing
