"""Connect persistent workspace panels to the shared docking controller."""
from __future__ import annotations

from pathlib import Path
import json
import os
import sys
import tkinter as tk
from tkinter import ttk

from docking import DockManager


class WorkspaceDockingMixin:
    def _init_workspace_docking(self):
        self._dock_save_after = None
        self._dock_saving_suspended = True
        self._dock_closed = False
        self._dock_float_bindings = []
        self._dock_shortcut_windows = set()
        self._dock_layout_path = getattr(self.app, 'workspace_layout_path', None)
        self.dock_manager = DockManager(self, scale=self._px,
            on_change=self._workspace_layout_changed, on_focus=self._show_dock_home)
        records = (
            ('media', '素材', self.media_tab, None),
            ('cuts', '切点', self.cut_tab, None),
            ('source', '多机位监看', self.source_panel, self.source_title),
            ('program', '成片监看', self.program_panel, self.program_title),
            ('timeline', '时间线', self.timeline_section, self.timeline_bar),
            ('clip', '片段检查器', self.program_inspector_tab, None),
            ('creative', '调色、转场与字幕', self.creative_tab, None),
            ('library', '特效库', self.library_tab, None),
            ('sync', '同步与代理', self.clip_tab, None),
            ('delivery', '交付', self.delivery_tab, None),
            ('grading', '精细调色', self.color_workspace, self.color_workspace.header),
        )
        self._dock_widgets = {key: widget for key, _title, widget, _header in records}
        self._dock_page_keys = {str(widget): key for key, widget in self._dock_widgets.items()}
        self._dock_titles = {key: title for key, title, _widget, _header in records}
        for key, title, widget, header in records:
            options = ({'default_hidden': True,
                'home': {'manager': 'panedwindow', 'container': self.workspace_split,
                         'index': 1, 'options': {'weight': 3}}} if key == 'grading' else {})
            self.dock_manager.register(key, title, widget, header=header,
                minsize=(240, 160), **options)
            # Existing widgets keep their keyboard bindings and video HWNDs
            # when their outer Frame becomes a native floating window.
            widget.bind('<KeyRelease>', self._transport_key_release, add='+')
            widget.bind('<FocusOut>', self._transport_focus_out, add='+')
        self._build_workspace_layout_menu()
        self._dock_saving_suspended = False
        self.after_idle(self._load_workspace_layout)

    def _build_workspace_layout_menu(self):
        menu = tk.Menu(self, tearoff=False, bg='#292b2e', fg='#dedfe3',
                       activebackground='#3c5263', activeforeground='#dedfe3')
        for key, title in self._dock_titles.items():
            item = tk.Menu(menu, tearoff=False, bg='#292b2e', fg='#dedfe3')
            item.add_command(label='显示 / 定位', command=lambda value=key: self._focus_dock_panel(value))
            item.add_command(label='浮动窗口', command=lambda value=key: self._float_dock_panel(value))
            item.add_command(label='浮动整个标签组', command=lambda value=key: self._float_dock_group(value))
            item.add_command(label='隐藏', command=lambda value=key: self.dock_manager.hide(value))
            menu.add_cascade(label=title, menu=item)
        menu.add_separator()
        menu.add_command(label='保存当前布局', command=self.save_workspace_layout)
        menu.add_command(label='载入已保存布局', command=self._load_workspace_layout)
        menu.add_command(label='恢复默认布局', command=self.reset_workspace_layout)
        self.workspace_layout_menu = menu
        self.workspace_layout_button = ttk.Menubutton(self.workspace_bar, text='布局 ▾',
            menu=menu, style='Compact.TMenubutton')
        self.workspace_layout_button.pack(side='left', padx=self._px(3), pady=self._px(5))

    def _show_dock_home(self, key):
        if key in {'media', 'cuts'} and not self._media_visible:
            self.toggle_media_pool()
        elif key in {'clip', 'creative', 'library', 'sync', 'delivery'}:
            self._show_inspector_host()

    def _show_inspector_host(self):
        if not self._inspector_visible:
            self.body.add(self.inspector, weight=0)
            self._inspector_visible = True
            self.update_idletasks()
            panes = self.body.panes()
            if len(panes) > 1:
                self.body.sashpos(len(panes)-2, max(self._px(180),
                    self.body.winfo_width()-min(self._inspector_width, round(self.body.winfo_width()*.36))))
        self.inspector_button.configure(text='检查器 ●')

    def _focus_dock_panel(self, key):
        if key == 'library' and vars(self).get('effects_browser') is None:
            return self.show_effects_library()
        if key == 'grading' and not self._color_mode:
            self.enter_color_workspace()
        self.dock_manager.focus(key)
        widget = self._dock_widgets[key]
        self._tag_transport_widgets(widget)
        return widget

    def _float_dock_panel(self, key):
        if key == 'library' and vars(self).get('effects_browser') is None:
            self.show_effects_library()
        self.dock_manager.float_panel(key)
        self._tag_transport_widgets(self._dock_widgets[key])

    def _float_dock_group(self, key):
        if key == 'library' and vars(self).get('effects_browser') is None:
            self.show_effects_library()
        self.dock_manager.float_group(key)
        self._tag_transport_widgets(self)
        self._bind_floating_transport()

    def _bind_floating_transport(self):
        enumerate_windows = getattr(self.dock_manager, 'iter_floating_windows', None)
        windows = (enumerate_windows() if enumerate_windows is not None else
                   (widget.winfo_toplevel() for widget in self._dock_widgets.values()))
        for top in windows:
            if top is self.app or str(top) in self._dock_shortcut_windows: continue
            self._dock_shortcut_windows.add(str(top))
            for event, callback in (('<KeyPress>', self._key),
                                    ('<KeyRelease>', self._transport_key_release),
                                    ('<FocusOut>', self._transport_focus_out)):
                identifier = top.bind(event, callback, add='+')
                self._dock_float_bindings.append((top, event, identifier))

    def _select_workspace_page(self, notebook, page):
        manager = vars(self).get('dock_manager')
        key = vars(self).get('_dock_page_keys', {}).get(str(page))
        if manager is not None and key:
            self._focus_dock_panel(key)
        else:
            notebook.select(page)

    def _workspace_layout_changed(self):
        if self._dock_closed or self._dock_saving_suspended: return
        self._sync_docked_color_mode()
        if not self.dock_manager.home_has_content('media') and self._media_visible:
            self.toggle_media_pool()
        if not self.dock_manager.home_has_content('clip') and self._inspector_visible:
            self.hide_inspector()
        for column, key in enumerate(('source', 'program')):
            occupied = self.dock_manager.home_has_content(key)
            self.monitor_body.grid_columnconfigure(column, weight=1 if occupied else 0,
                minsize=0, uniform='monitor' if occupied else '')
        if vars(self).get('_transport_tag'):
            self._tag_transport_widgets(self)
            self._bind_floating_transport()
        if self._dock_layout_path is not None:
            if self._dock_save_after is not None:
                self.after_cancel(self._dock_save_after)
            self._dock_save_after = self.after(500, self.save_workspace_layout)

    def _sync_docked_color_mode(self):
        if vars(self).get('_dock_syncing_color'): return
        self._dock_syncing_color = True
        try:
            visible = self.dock_manager.panel_location('grading') != 'hidden'
            if visible and not self._color_mode:
                self.enter_color_workspace()
            elif not visible and self._color_mode:
                self.exit_color_workspace()
        finally:
            self._dock_syncing_color = False

    def _layout_file(self):
        if self._dock_layout_path is not None:
            return Path(self._dock_layout_path)
        base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
        return base / 'workspace-layout.json'

    def save_workspace_layout(self):
        if self._dock_save_after is not None:
            self.after_cancel(self._dock_save_after)
            self._dock_save_after = None
        try:
            payload = self.dock_manager.serialize()
            payload['workspace_chrome'] = self._workspace_chrome_state()
            path = self._layout_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + '.pending')
            try:
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
                os.replace(temporary, path)
            finally:
                if temporary.exists(): temporary.unlink()
            self.status.set('工作区布局已保存；面板位置独立于剪辑工程。')
            return True
        except (OSError, ValueError, tk.TclError) as error:
            self.status.set('保存工作区布局失败：' + str(error))
            return False

    def _workspace_chrome_state(self):
        def ratios(pane, horizontal):
            length = pane.winfo_width() if horizontal else pane.winfo_height()
            return [pane.sashpos(index) / max(1, length)
                    for index in range(max(0, len(pane.panes())-1))]
        return {'media_visible': self._media_visible, 'inspector_visible': self._inspector_visible,
                'camera_rows': self.camera_rows, 'camera_columns': self.camera_columns,
                'body_sashes': ratios(self.body, True),
                'main_sashes': ratios(self.workspace_split, False)}

    def _restore_workspace_chrome(self, data):
        if not isinstance(data, dict): return
        try: rows, columns = self._checked_camera_grid(data.get('camera_rows', 3), data.get('camera_columns', 3))
        except ValueError: rows, columns = 3, 3
        self.set_camera_grid(rows, columns, persist=False)
        if bool(data.get('media_visible', True)) != self._media_visible:
            self.toggle_media_pool()
        if data.get('inspector_visible', False): self._show_inspector_host()
        else: self.hide_inspector()
        def apply_sashes():
            if self._dock_closed: return
            for pane, field, horizontal in ((self.body, 'body_sashes', True),
                                            (self.workspace_split, 'main_sashes', False)):
                values = data.get(field, ())
                if not isinstance(values, (list, tuple)): continue
                length = pane.winfo_width() if horizontal else pane.winfo_height()
                for index, value in enumerate(values[:max(0, len(pane.panes())-1)]):
                    if isinstance(value, (int, float)) and 0. < value < 1.:
                        pane.sashpos(index, round(length*value))
        self.after_idle(apply_sashes)

    def _load_workspace_layout(self):
        if self._dock_closed: return False
        # Lightweight Tk harnesses do not opt into application preferences.
        if self._dock_layout_path is None: return False
        path = self._layout_file()
        if not path.is_file(): return False
        self._dock_saving_suspended = True
        try:
            self.exit_color_workspace()
            payload = json.loads(path.read_text(encoding='utf-8'))
            self.dock_manager.restore(payload)
            self._sync_docked_color_mode()
            self._restore_workspace_chrome(payload.get('workspace_chrome', {}))
            self._tag_transport_widgets(self)
            self._bind_floating_transport()
            self.status.set('已恢复保存的工作区布局。')
            return True
        except (OSError, ValueError, tk.TclError) as error:
            self.status.set('工作区布局无法载入，保留当前布局：' + str(error))
            return False
        finally:
            self._dock_saving_suspended = False

    def reset_workspace_layout(self):
        self._dock_saving_suspended = True
        try:
            self.exit_color_workspace()
            self.dock_manager.restore_defaults()
            self.set_camera_grid(3, 3, persist=False)
            if not self._media_visible: self.toggle_media_pool()
            self.hide_inspector()
            self._layout_initialized = False
            self._initial_layout()
            self.status.set('已恢复默认布局；拖动面板标签或标题可重新组合。')
        finally:
            self._dock_saving_suspended = False
        self._workspace_layout_changed()

    def _close_workspace_docking(self):
        if vars(self).get('_dock_closed', True): return
        if self._dock_save_after is not None:
            self.after_cancel(self._dock_save_after)
            self._dock_save_after = None
        if self._dock_layout_path is not None:
            self.save_workspace_layout()
        self._dock_closed = True
        for window, event, identifier in self._dock_float_bindings:
            if window.winfo_exists(): window.unbind(event, identifier)
        self._dock_float_bindings.clear()
        self.dock_manager.close()
