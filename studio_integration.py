"""Add companion-exporter tools without replacing OKK's editing workspace."""
from pathlib import Path
import os
import tkinter as tk
from tkinter import ttk, messagebox

from studio_project import StudioProjectMixin
from project_file import AUTOSAVE_INTERVAL_MS, autosave_path
from media_pool_workspace import MediaPoolWorkspace


class StudioIntegrationMixin(StudioProjectMixin):
    def _build_studio_menu(self, parent):
        button = tk.Menubutton(parent, text='工程 ▾', bg='#202124', fg='#dedfe3',
                               activebackground='#2c2e32', activeforeground='#dedfe3',
                               relief='flat', bd=0, font=('Microsoft YaHei UI', 9))
        button.pack(side='left', padx=12)
        menu = tk.Menu(button, tearoff=False)
        button.configure(menu=menu)
        for title, method in [('新建完整工程', '_project_new'), ('打开完整工程…', '_project_open'),
                              ('保存完整工程', '_project_save'), ('完整工程另存为…', '_project_save_as')]:
            menu.add_command(label=title, command=lambda key=method: getattr(self, key)())
        self._project_recent_menu = tk.Menu(menu, tearoff=False)
        menu.add_cascade(label='最近的完整工程', menu=self._project_recent_menu)
        menu.add_command(label='恢复上次会话…', command=lambda: self._project_restore_autosave())
        menu.add_separator()
        menu.add_command(label='媒体库…', command=lambda: self._show_media_library())
        menu.add_command(label='无损分段', command=lambda: self._show_lossless_tools())
        menu.add_command(label='生成剪辑代理', command=lambda: self._show_proxy_tools())

    def _init_studio_integration(self):
        self._project_launcher = None
        self._closing = False
        self._pool_timeline_hover = False
        style = ttk.Style(self)
        style.configure('Pool.Treeview', background='#191a1c', fieldbackground='#191a1c',
                        foreground='#e1e3e6', rowheight=28, borderwidth=0)
        style.configure('Pool.Treeview.Heading', background='#242629', foreground='#e1e3e6')
        style.map('Pool.Treeview', background=[('selected', '#3c5263')], foreground=[('selected', '#e1e3e6')])
        self._library_window = tk.Toplevel(self)
        self._library_window.withdraw()
        self._library_window.title('OKK STUDIO · 媒体库')
        self._library_window.geometry('1160x520')
        self._library_window.protocol('WM_DELETE_WINDOW', self._library_window.withdraw)
        self.media_pool_workspace = MediaPoolWorkspace(self, self._library_window)
        self.media_pool_workspace.pack(fill='both', expand=True)
        self._init_project_state()
        self.after(AUTOSAVE_INTERVAL_MS, self._project_autosave_tick)
        # Keep existing multicam Ctrl+S; whole-session shortcuts are distinct.
        self.bind('<Control-Shift-S>', lambda event: self._project_save_as())
        self.bind('<Control-Shift-O>', lambda event: self._project_open())
        self.bind('<Control-Shift-L>', lambda event: self._show_media_library())
        self.after(1200, self._offer_session_recovery)

    def _offer_session_recovery(self):
        from studio_project import app_directory
        if autosave_path(app_directory()).is_file() and not self._project_is_dirty():
            if messagebox.askyesno('恢复会话', '发现上次自动保存的完整工程，是否恢复？', parent=self):
                self._project_restore_autosave()

    def _show_media_library(self):
        if self.working:
            return
        self._library_window.deiconify()
        self._library_window.lift()

    def _show_lossless_tools(self):
        self._select_page('editor')
        self.editor_toolbox.select(self.lossless_panel)

    def _show_proxy_tools(self):
        workspace = self.multicam_workspace
        selected = set(workspace.media_tree.selection())
        clips = [c for c in workspace.project.clips if c.id in selected] if selected else workspace.project.clips
        self._add_proxy_paths([c.path for c in clips])
        self._select_page('editor')
        self.editor_toolbox.select(self.editor_proxy_panel)

    def _pool_add_paths(self, paths):
        if self.media_pool_workspace is not None:
            self.media_pool_workspace.add_paths(list(paths))

    def _add_join_paths(self, paths):
        if self.working:
            return
        known = {os.path.normcase(str(Path(p).resolve())) for p in self.editor_join_files}
        for value in paths:
            path = str(Path(value).resolve())
            key = os.path.normcase(path)
            if key not in known:
                self.editor_join_files.append(path)
                known.add(key)
        self._refresh_join_list()
        self._select_page('editor')

    def _set_editor_audio(self, path):
        self.editor_audio_var.set(path)
        self.editor_join_alignments.clear()
        self.editor_join_alignment_audio = ''
        self.editor_join_timeline_probe_key = None
        self._refresh_join_list()

    def _timeline_contains_screen_point(self, x, y):
        canvas = self.editor_join_timeline_canvas
        return (canvas.winfo_ismapped() and canvas.winfo_rootx() <= x < canvas.winfo_rootx()+canvas.winfo_width()
                and canvas.winfo_rooty() <= y < canvas.winfo_rooty()+canvas.winfo_height())

    def _media_pool_timeline_hover(self, x, y):
        if self.multicam_workspace.timeline_drop_hover(x, y):
            return True
        inside = not self.working and self._timeline_contains_screen_point(x, y)
        if inside != self._pool_timeline_hover:
            self._pool_timeline_hover = inside
            self.editor_join_timeline_canvas.configure(highlightthickness=2 if inside else 0,
                                                       highlightbackground='#69a8ce')
        return inside

    def _media_pool_timeline_clear_hover(self):
        self.multicam_workspace.timeline_drop_clear_hover()
        self._pool_timeline_hover = False
        self.editor_join_timeline_canvas.configure(highlightthickness=0)

    def _media_pool_timeline_drop(self, clips, x, y):
        if self.working:
            return
        online = [c for c in clips if Path(c.path).is_file()]
        videos = [c.path for c in online if not self.media_pool_workspace._audio_only(c)]
        audio = [c.path for c in online if self.media_pool_workspace._audio_only(c)]
        if len(audio) > 1:
            messagebox.showinfo('主音频', '请一次拖入一条连续主音频。', parent=self)
            return
        workspace = self.multicam_workspace
        if workspace.timeline_drop_hover(x, y):
            workspace.timeline_drop_clear_hover()
            workspace.timeline_drop_at(videos, x, y, master_audio=audio[0] if audio else None)
            return
        if not self._timeline_contains_screen_point(x, y):
            return
        if audio:
            self._set_editor_audio(audio[0])
        old = list(self.editor_join_files)
        canvas = self.editor_join_timeline_canvas
        position = self.editor_join_timeline_origin + (canvas.canvasx(x-canvas.winfo_rootx())-64)/max(1e-6,self.editor_join_timeline_pixels_per_second)
        model = self._get_join_timeline_model()
        index = next((c.index for c in model.clips if position < c.start+c.duration/2), len(old))
        self._add_join_paths(videos)
        added = self.editor_join_files[len(old):]
        self.editor_join_files = old[:index]+added+old[index:]
        self._refresh_join_list()
        if added:
            self.editor_join_listbox.selection_clear(0, 'end')
            self.editor_join_listbox.selection_set(index, index+len(added)-1)
            self.editor_join_listbox.see(index)
            self._fit_join_timeline()
        self.editor_status_var.set(f'已拖入时间线：{len(added)} 个视频'+('、主音频' if audio else ''))

    def _handle_studio_event(self, kind, payload):
        handlers = {
            'lossless_media': self.lossless_workspace.handle_media,
            'lossless_media_error': self.lossless_workspace.handle_media_error,
            'lossless_job_done': self.lossless_workspace.handle_job_done,
            'lossless_job_error': self.lossless_workspace.handle_job_error,
            'lossless_detection': self.lossless_workspace.handle_detection,
        }
        pool = self.media_pool_workspace
        if pool is not None:
            handlers.update(media_pool_probe=pool.handle_probe, media_pool_probe_error=pool.handle_probe_error)
        handler = handlers.get(kind)
        if handler:
            handler(payload)
            return True
        return False
