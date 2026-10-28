"""Background source concatenation, with one undoable timeline publication."""
from pathlib import Path
import threading
from tkinter import messagebox

from exporter_core import find_binary


class TimelineMergeMixin:
    def _merge_timeline_selection(self):
        from timeline_merge import plan_source_merge, render_source_merge
        if not self._available(): return False
        identifiers = [key[1] for key in self._timeline_selection if key[0] == 'clip']
        try:
            plan = plan_source_merge(self.project, identifiers)
        except ValueError as error:
            messagebox.showerror('合并素材', str(error), parent=self)
            return False
        app_dir = Path(__file__).resolve().parent
        ffmpeg = getattr(self.app.caps, 'ffmpeg_path', '') or find_binary('ffmpeg', app_dir)
        ffprobe = getattr(self.app.caps, 'ffprobe_path', '') or find_binary('ffprobe', app_dir)
        if not ffmpeg or not ffprobe:
            messagebox.showerror('合并素材', '需要 bin 目录中的 ffmpeg.exe 和 ffprobe.exe。', parent=self)
            return False
        self.pause()
        project = self.project
        self.busy = True
        self.app.cancel_event.clear()
        self.app._set_working(True)
        self.status.set(f'正在检查并合并 {len(identifiers)} 段素材，后续片段自动首尾接齐…')
        begin = getattr(self.app, '_begin_shared_progress', None)
        if callable(begin): begin('合并素材 0%')

        def worker():
            try:
                result = render_source_merge(plan, ffmpeg, ffprobe, app_dir/'merged_media',
                    cancel_event=self.app.cancel_event,
                    progress=lambda percent, phase: self.jobs.put(('timeline_merge_progress', (percent, phase))))
                self.jobs.put(('timeline_merge_ready', (project, plan, result)))
            except Exception as error:
                self.jobs.put(('timeline_merge_error', (str(error), self.app.cancel_event.is_set())))
        threading.Thread(target=worker, name='timeline-source-merge', daemon=False).start()
        return True

    def _handle_timeline_merge_job(self, kind, payload):
        if not kind.startswith('timeline_merge_'): return False
        if kind == 'timeline_merge_progress':
            percent, phase = payload
            self.app.progress_var.set(percent)
            self.app.progress_text_var.set(str(phase))
            self.status.set(str(phase))
            return True
        self.busy = False
        self.app._set_working(False)
        if kind == 'timeline_merge_error':
            error, cancelled = payload
            self.status.set('已取消合并，原素材与时间线已保留。' if cancelled else error)
            if not cancelled: messagebox.showerror('合并素材', error, parent=self)
            return True
        if kind == 'timeline_merge_ready':
            from timeline_merge import apply_source_merge
            project, plan, result = payload
            if project is not self.project or self.app.cancel_event.is_set():
                self.status.set('合并素材已生成，当前工程未改动：' + result.path)
                return True
            try:
                candidate, clip = apply_source_merge(self.project, plan, result)
            except ValueError as error:
                messagebox.showerror('合并素材', str(error), parent=self)
                return True
            self._remember_timeline_change()
            self.project = candidate
            self._restore_timeline_range_fields()
            self._refresh()
            self._set_timeline_selection({('clip', clip.id)})
            self._reveal_timeline_clip(clip)
            self.seek(max(0., clip.start))
            self.app.progress_var.set(100)
            self.status.set(f'已首尾接齐并合并 {len(plan.source_ids)} 段；原片保留，Ctrl+Z 撤销。')
            return True
        return False
