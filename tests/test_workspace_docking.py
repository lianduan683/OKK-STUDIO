"""Offscreen workspace docking without recreating media/model objects."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from tkinter import messagebox, ttk
import unittest
from unittest.mock import patch
import rtx5090_exporter as app_module
import studio_project

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from . import test_timeline_drag_merge as fixture
    from .test_transport_controls import RecordingPlayer
except ImportError:
    import test_timeline_drag_merge as fixture
    from test_transport_controls import RecordingPlayer


class WorkspaceDockingTests(unittest.TestCase):
    def setUp(self):
        fixture.TimelineDropTests.setUp(self)
        self.manager = self.workspace.dock_manager
        self.screen_patch = patch.object(self.manager, '_screen_bounds', return_value=(-20000, -20000, 40000, 40000))
        self.screen_patch.start()
        self.addCleanup(self.screen_patch.stop)
        self.layout_folder = tempfile.TemporaryDirectory(prefix='okk-docking-test-')
        self.workspace._dock_layout_path = Path(self.layout_folder.name) / 'workspace-layout.json'
        self.addCleanup(self.layout_folder.cleanup)
        self.project_before = copy.deepcopy(self.workspace.project)
        self.handles = self.video_handles()

    def tearDown(self):
        fixture.TimelineDropTests.tearDown(self)

    def video_handles(self):
        ws = self.workspace
        return [ws.program_surface.winfo_id(), ws.audio_surface.winfo_id(),
                *(view.winfo_id() for view in ws.program_views),
                *(view.winfo_id() for view in ws.comparison_views),
                *(view.winfo_id() for view in ws.camera_surfaces.values())]

    def assert_leaf_receives_pointer(self, widget):
        # Win32 omits fully transparent windows from hit testing. The fixture
        # stays beyond the desktop while its opacity is briefly enabled.
        windows = [self.root, *self.manager.iter_floating_windows()]
        opacity = [(window, window.tk.call('wm', 'attributes', window._w, '-alpha')) for window in windows]
        for window in windows: window.tk.call('wm', 'attributes', window._w, '-alpha', 1)
        self.root.update()
        x = widget.winfo_rootx() + max(1, widget.winfo_width() // 2)
        y = widget.winfo_rooty() + max(1, widget.winfo_height() // 2)
        try:
            hit = self.workspace.winfo_containing(x, y)
        finally:
            for window, value in opacity:
                window.tk.call('wm', 'attributes', window._w, '-alpha', value)
        self.assertIsNotNone(hit)
        ancestors = []
        while hit is not None:
            ancestors.append(hit)
            hit = hit.master
        self.assertIn(widget, ancestors, 'a newly created docking container covers the live panel')

    def float_panel(self, key):
        self.manager.float_panel(key, geometry='440x340+10000+10000')
        widget = self.workspace._dock_widgets[key]
        widget.tk.call('wm', 'attributes', widget._w, '-alpha', 0)
        self.root.update_idletasks()
        return widget

    def float_group(self, key):
        self.manager.float_group(key, geometry='620x430+10000+10000')
        window = self.manager.floating_group_window(key)
        self.assertIsNotNone(window)
        window.attributes('-alpha', 0)
        self.root.update_idletasks()
        return window

    def test_initial_layout_has_eleven_movable_panels_and_original_notebooks(self):
        ws = self.workspace
        self.assertEqual(set(ws._dock_widgets), {
            'media', 'cuts', 'source', 'program', 'timeline',
            'clip', 'creative', 'library', 'sync', 'delivery', 'grading'})
        self.assertEqual(self.manager.panel_location('grading'), 'hidden')
        self.assertEqual(ws.tools.tabs(), (str(ws.media_tab), str(ws.cut_tab)))
        self.assertEqual(ws.inspector_tabs.tabs(), tuple(map(str, (
            ws.program_inspector_tab, ws.creative_tab, ws.library_tab, ws.clip_tab, ws.delivery_tab))))
        self.assertTrue(all(widget.master is ws for widget in ws._dock_widgets.values()))
        self.assertTrue(ws.source_panel.winfo_ismapped())
        self.assertTrue(ws.program_panel.winfo_ismapped())
        self.assertTrue(ws.timeline.winfo_ismapped())
        self.assertEqual(self.errors, [])

    def test_each_leaf_can_float_and_restore_without_changing_native_video_handles(self):
        ws = self.workspace
        for key in ws._dock_widgets:
            with self.subTest(panel=key):
                widget = ws._dock_widgets[key]
                handle = widget.winfo_id()
                self.float_panel(key)
                self.assertEqual(self.manager.panel_location(key), 'floating')
                self.assertEqual(widget.winfo_id(), handle)
                self.assertEqual(self.video_handles(), self.handles)
                self.manager.restore_defaults()
                self.root.update_idletasks()
                self.assertEqual(self.manager.panel_location(key), 'hidden' if key == 'grading' else 'home')
                self.assertEqual(widget.winfo_id(), handle)
        self.assertEqual(ws.project, self.project_before)
        self.assertEqual(ws.undo_stack, [])
        self.assertEqual(self.errors, [])

    def test_monitor_tabs_combine_focus_and_restore_with_running_player_untouched(self):
        ws = self.workspace
        player = ws.player = RecordingPlayer()
        self.manager.dock('source', 'program', side='center')
        self.root.update_idletasks()
        self.assertEqual(self.manager.panel_location('source'), 'group')
        self.assertEqual(ws.source_panel.winfo_manager(), 'notebook')
        self.assertEqual(ws.program_panel.winfo_manager(), 'notebook')
        self.manager.focus('source')
        self.root.update_idletasks()
        self.assertTrue(ws.source_panel.winfo_ismapped())
        self.manager.focus('program')
        self.root.update_idletasks()
        self.assertTrue(ws.program_panel.winfo_ismapped())
        self.assert_leaf_receives_pointer(ws.program_surface)
        self.assertEqual(ws.monitor_body.grid_columnconfigure(0, 'weight'), 0)
        self.assertEqual(ws.monitor_body.grid_columnconfigure(1, 'weight'), 1)
        self.assertIs(ws.player, player)
        self.assertFalse(player.closed)
        self.assertEqual(self.video_handles(), self.handles)
        self.manager.restore_defaults()
        self.root.update_idletasks()
        self.assertEqual(ws.source_panel.winfo_manager(), 'grid')
        self.assertEqual(ws.program_panel.winfo_manager(), 'grid')
        self.assertEqual(ws.project, self.project_before)
        self.assertEqual(self.errors, [])

    def test_floating_inspector_page_is_focused_in_place_by_existing_toolbar(self):
        ws = self.workspace
        ws.show_inspector('color')
        self.float_panel('creative')
        ws.hide_inspector()
        ws.show_inspector_tool('subtitle')
        self.assertEqual(self.manager.panel_location('creative'), 'floating')
        self.assertEqual(ws.creative_panel.tabs.select(), str(ws.creative_panel.pages['subtitle']))
        self.assertFalse(ws._inspector_visible)
        self.assertTrue(ws.timeline.winfo_ismapped())
        self.assertEqual(self.video_handles(), self.handles)
        self.assertEqual(self.errors, [])

    def test_library_docked_beside_timeline_stays_there_when_opened_again(self):
        from effects_browser import PRESETS
        ws = self.workspace
        with patch('effects_browser.library_presets', return_value=PRESETS):
            browser = ws.show_effects_library()
            browser_handle = browser.winfo_id()
            self.manager.dock('library', 'timeline', side='right')
            ws.hide_inspector()
            ws.show_effects_library()
            self.root.update_idletasks()
            self.assertEqual(self.manager.panel_location('library'), 'group')
            self.assertFalse(ws._inspector_visible)
            self.assertTrue(browser.winfo_ismapped())
            self.assertTrue(ws.timeline.winfo_ismapped())
            self.assertEqual(browser.winfo_id(), browser_handle)
            self.assertIn(ws._transport_tag, browser.list.bindtags())
        self.assertEqual(self.errors, [])

    def test_first_library_tab_selection_after_docking_builds_its_content(self):
        from effects_browser import PRESETS
        ws = self.workspace
        self.assertIsNone(vars(ws).get('effects_browser'))
        self.manager.dock('source', 'program', side='center')
        with patch('effects_browser.library_presets', return_value=PRESETS):
            self._select_group_library_without_toolbar()
        self.assertIsNotNone(vars(ws).get('effects_browser'))
        self.assertTrue(ws.effects_browser.winfo_ismapped())
        self.assertIn(ws._transport_tag, ws.effects_browser.list.bindtags())
        self.assertEqual(ws.project, self.project_before)
        self.assertEqual(self.errors, [])

    def _select_group_library_without_toolbar(self):
        self.workspace._show_inspector_host()
        self.manager.focus('library')
        self.root.update_idletasks()

    def test_save_and_reload_layout_preserves_model_proxy_and_panel_identity(self):
        ws = self.workspace
        ws.project.clips[0].proxy_path = 'one-720p.mp4'
        model = copy.deepcopy(ws.project)
        handles = {key: widget.winfo_id() for key, widget in ws._dock_widgets.items()}
        self.float_panel('source')
        self.manager.dock('sync', 'timeline', side='left')
        self.assertTrue(ws.save_workspace_layout())
        self.assertTrue(ws._layout_file().is_file())
        self.manager.restore_defaults()
        self.assertTrue(ws._load_workspace_layout())
        self.root.update_idletasks()
        self.assertEqual(self.manager.panel_location('source'), 'floating')
        self.assertEqual(self.manager.panel_location('sync'), 'group')
        self.assertEqual(ws.project, model)
        self.assertEqual({key: widget.winfo_id() for key, widget in ws._dock_widgets.items()}, handles)
        self.assertEqual(self.video_handles(), self.handles)
        self.assertEqual(self.errors, [])

    def test_floating_timeline_survives_fine_color_mode_and_default_restore(self):
        ws = self.workspace
        self.float_panel('timeline')
        ws.select_program_range(0., 4.)
        ws.show_creative('color')
        self.assertTrue(ws._color_mode)
        self.assertEqual(self.manager.panel_location('timeline'), 'floating')
        ws.exit_color_workspace()
        self.assertEqual(self.manager.panel_location('timeline'), 'floating')
        ws.reset_workspace_layout()
        self.root.update_idletasks()
        self.assertTrue(self.manager.is_home('timeline'))
        self.assertIn(str(ws.timeline_section), ws.workspace_split.panes())
        self.assertFalse(ws._inspector_visible)
        self.assertTrue(ws._media_visible)
        self.assertEqual(self.video_handles(), self.handles)
        self.assertEqual(self.errors, [])

    def test_focus_hidden_home_and_hidden_floating_panel_preserves_widgets(self):
        ws = self.workspace
        ws.hide_inspector()
        ws.show_inspector('clip')
        self.assertTrue(ws._inspector_visible)
        self.assertEqual(ws.inspector_tabs.select(), str(ws.clip_tab))
        self.float_panel('program')
        self.manager.hide('program')
        self.assertFalse(self.manager.is_visible('program'))
        self.manager.show('program')
        self.root.update_idletasks()
        self.assertTrue(self.manager.is_visible('program'))
        self.assertEqual(self.video_handles(), self.handles)
        self.assertEqual(self.errors, [])

    def test_fine_color_floats_docks_and_closes_with_mode_and_timeline_synchronized(self):
        ws = self.workspace
        ws.select_program_range(0., 4.)
        original_tabs = ws.inspector_tabs.tabs()
        ws.show_creative('color')
        self.assertTrue(ws._color_mode)
        self.assertEqual(ws.inspector_tabs.tabs(), original_tabs)
        self.assertIn(str(ws.color_workspace), ws.workspace_split.panes())
        self.assertNotIn(str(ws.timeline_section), ws.workspace_split.panes())
        self.float_panel('grading')
        self.assertTrue(ws._color_mode)
        self.root.update_idletasks()
        self.assertTrue(ws.timeline.winfo_ismapped())
        ws.exit_color_workspace()
        self.assertFalse(ws._color_mode)
        self.assertEqual(self.manager.panel_location('grading'), 'hidden')
        ws.show_creative('color')
        self.assertTrue(ws._color_mode)
        self.assertEqual(self.manager.panel_location('grading'), 'floating')
        self.manager.dock('grading', 'timeline', side='top')
        self.assertTrue(ws._color_mode)
        self.assertEqual(self.manager.panel_location('grading'), 'group')
        self.assertEqual(self.video_handles(), self.handles)
        self.assertEqual(ws.project, self.project_before)
        ws.reset_workspace_layout()
        self.assertFalse(ws._color_mode)
        self.assertEqual(self.manager.panel_location('grading'), 'hidden')
        self.assertEqual(self.errors, [])

    def test_float_entire_monitor_group_preserves_tabs_transport_and_video_handles(self):
        ws = self.workspace
        player = ws.player = RecordingPlayer()
        self.manager.dock('source', 'program', side='center')
        window = self.float_group('program')
        self.assertEqual(set(self.manager.group_members('program')), {'source', 'program'})
        self.assertIs(self.manager.floating_group_window('source'), window)
        self.assertIn(window, tuple(self.manager.iter_floating_windows()))
        self.assertTrue(window.bind('<KeyPress>'))
        self.assertTrue(window.bind('<KeyRelease>'))
        self.assertTrue(window.bind('<FocusOut>'))
        for key in ('source', 'program'):
            self.manager.focus(key)
            self.root.update_idletasks()
            self.assertEqual(self.manager.panel_location(key), 'floating')
            self.assertIsNone(self.manager.focus_home(key))
            self.assertTrue(self.manager.is_visible(key))
            self.assert_leaf_receives_pointer(ws.camera_surfaces[1] if key == 'source' else ws.program_surface)
        ws._transport_key_press(1)
        position = ws.current
        script = window.bind('<KeyRelease>')
        script = script.replace('%K', 'Right').replace('%A', '{}').replace('%s', '0')
        window.bind('<<DockReleaseTest>>', script)
        window.event_generate('<<DockReleaseTest>>', when='now')
        self.assertIsNone(ws._transport_hold_after)
        self.assertEqual(ws.current, position)
        window.unbind('<<DockReleaseTest>>')
        ws._transport_key_press(-1)
        position = ws.current
        # FocusOut is a separate safety path for a native shell title.
        window.event_generate('<FocusOut>', when='now')
        self.assertIsNone(ws._transport_hold_after)
        self.assertEqual(ws.current, position)
        self.assertFalse(ws.playing)
        self.assertIs(ws.player, player)
        self.assertFalse(player.closed)
        self.assertEqual(self.video_handles(), self.handles)
        self.manager.dock_group('program', 'timeline', side='top')
        self.root.update_idletasks()
        self.assertEqual(self.manager.panel_location('source'), 'group')
        self.assertEqual(self.manager.panel_location('program'), 'group')
        self.assertEqual(set(self.manager.group_members('program')), {'source', 'program'})
        self.assertEqual(self.video_handles(), self.handles)
        self.assertEqual(ws.project, self.project_before)
        self.assertEqual(self.errors, [])

    def test_whole_group_menu_and_saved_layout_restore_same_panels_and_shell_shortcuts(self):
        ws = self.workspace
        menu = ws.workspace_layout_menu
        program_index = next(index for index in range(menu.index('end') + 1)
            if menu.type(index) == 'cascade' and menu.entrycget(index, 'label') == ws._dock_titles['program'])
        submenu = self.root.nametowidget(menu.entrycget(program_index, 'menu'))
        group_index = next(index for index in range(submenu.index('end') + 1)
            if submenu.entrycget(index, 'label') == '浮动整个标签组')
        self.manager.dock('source', 'program')
        with patch.object(ws, '_float_dock_group', wraps=ws._float_dock_group) as floating:
            submenu.invoke(group_index)
            floating.assert_called_once_with('program')
        window = self.manager.floating_group_window('program')
        window.geometry('620x430+10000+10000')
        window.attributes('-alpha', 0)
        self.assertTrue(ws.save_workspace_layout())
        self.manager.restore_defaults()
        self.assertTrue(ws._load_workspace_layout())
        self.root.update_idletasks()
        restored = self.manager.floating_group_window('program')
        self.assertIsNotNone(restored)
        self.assertIs(self.manager.floating_group_window('source'), restored)
        self.assertTrue(restored.bind('<KeyRelease>'))
        self.assertEqual(set(self.manager.group_members('program')), {'source', 'program'})
        self.assertEqual(self.video_handles(), self.handles)
        self.assertEqual(ws.project, self.project_before)
        self.assertEqual(self.errors, [])

    def test_fine_color_inside_floating_group_keeps_mode_and_hides_only_its_own_tab(self):
        ws = self.workspace
        ws.select_program_range(0., 4.)
        ws.show_creative('color')
        self.manager.dock('grading', 'timeline', side='center')
        self.float_group('grading')
        self.assertTrue(ws._color_mode)
        self.assertEqual(set(self.manager.group_members('grading')), {'grading', 'timeline'})
        ws.exit_color_workspace()
        self.assertFalse(ws._color_mode)
        self.assertEqual(self.manager.panel_location('grading'), 'hidden')
        self.assertTrue(self.manager.is_visible('timeline'))
        ws.show_creative('color')
        self.assertTrue(ws._color_mode)
        self.assertEqual(self.manager.panel_location('grading'), 'floating')
        self.assertEqual(set(self.manager.group_members('grading')), {'grading', 'timeline'})
        self.assertEqual(self.video_handles(), self.handles)
        self.assertEqual(self.errors, [])


class RealApplicationDockingStyleTests(unittest.TestCase):
    """Exercise the real theme that intentionally hides top-level app tabs."""
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='okk-dock-theme-')
        self.addCleanup(self.directory.cleanup)
        location = Path(self.directory.name)
        real_tk = next(cls for cls in app_module.ExporterApp.__mro__ if cls.__name__ == 'Tk')
        original_init = real_tk.__init__

        def hidden_init(window, *args, **kwargs):
            original_init(window, *args, **kwargs)
            window.withdraw()
            window.attributes('-alpha', 0)

        self.patches = [
            patch.object(real_tk, '__init__', hidden_init),
            patch.object(studio_project, 'app_directory', return_value=location),
            patch.object(app_module, 'initialize_cache_policy', return_value=location),
            patch.object(app_module.ExporterApp, '_detect_async'),
            patch.object(app_module.ExporterApp, '_offer_session_recovery'),
            patch.object(app_module.ExporterApp, 'state'),
            patch.object(app_module.MulticamWorkspace, '_schedule_camera_metadata'),
            patch.object(app_module.MulticamWorkspace, '_schedule_transition_previews'),
        ]
        self.patches.extend(patch.object(messagebox, name, return_value=False)
            for name in ('showerror', 'showwarning', 'showinfo', 'askyesno', 'askyesnocancel'))
        for item in self.patches: item.start()
        self.app = app_module.ExporterApp()
        self.app.mpv_path = ''
        self.app.overrideredirect(True)
        self.app.geometry('1500x900+10000+10000')
        self.errors = []
        self.app.report_callback_exception = lambda *details: self.errors.append(details)
        self.app.deiconify()
        self.app.update()

    def tearDown(self):
        if hasattr(self, 'app'):
            self.app._closing = True
            self.app.lossless_workspace.close()
            self.app.media_pool_workspace.close()
            self.app.multicam_workspace.close()
            for pending in self.app.tk.call('after', 'info'):
                self.app.tk.call('after', 'cancel', pending)
            self.app.destroy()
        for item in reversed(self.patches): item.stop()

    def test_real_app_hidden_top_tabs_do_not_hide_docked_panel_tab_labels(self):
        ws = self.app.multicam_workspace
        manager = ws.dock_manager
        manager.dock('source', 'program', side='center')
        self.app.update()

        def walk(widget):
            yield widget
            for child in widget.winfo_children(): yield from walk(child)

        notebook = next(widget for widget in walk(ws)
            if isinstance(widget, ttk.Notebook) and str(ws.program_panel) in widget.tabs())
        style = ttk.Style(self.app)
        def has_label(layout):
            return any('label' in name.lower() or has_label(options.get('children', ()))
                       for name, options in layout)
        self.assertFalse(has_label(style.layout('Workspace.TNotebook.Tab')))
        self.assertTrue(has_label(style.layout(str(notebook.cget('style')) + '.Tab')))
        visible_labels = set()
        for y in range(3, min(70, notebook.winfo_height()), 3):
            for x in range(3, min(750, notebook.winfo_width()), 4):
                if 'label' in notebook.identify(x, y):
                    visible_labels.add(notebook.index(f'@{x},{y}'))
        self.assertEqual(visible_labels, {0, 1}, 'both source/program tab labels must have real hit regions')
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
