"""Offscreen integration for the persistent programme inspector modules."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from creative_core import ColorGrade
from multicam_core import Cut
from program_edits import grade_at, set_grade_on_range
try:
    from . import test_timeline_drag_merge as fixture
except ImportError:
    import test_timeline_drag_merge as fixture


class ClipInspectorTests(unittest.TestCase):
    def setUp(self):
        fixture.TimelineDropTests.setUp(self)
        self.dialog_patch = patch('clip_inspector.messagebox.showerror')
        self.dialog = self.dialog_patch.start()
        self.workspace.project.cuts = [Cut(0., 1), Cut(4., 2), Cut(7., 1)]
        self.workspace._refresh()
        self.workspace.select_program_range(0., 4.)
        self.root.update_idletasks()
        self.panel = self.workspace.clip_inspector

    def tearDown(self):
        self.dialog_patch.stop()
        fixture.TimelineDropTests.tearDown(self)

    def test_selecting_programme_exposes_six_modules_without_hiding_monitors_or_timeline(self):
        ws = self.workspace
        self.assertTrue(ws._inspector_visible)
        self.assertEqual(ws.inspector_tabs.select(), str(ws.program_inspector_tab))
        self.assertEqual(set(self.panel.sections), {'camera', 'color', 'image', 'gyro', 'transition', 'overlay'})
        self.assertIn(str(ws.monitors), ws.body.panes())
        self.assertTrue(ws.program_surface.winfo_ismapped())
        self.assertTrue(ws.timeline.winfo_ismapped())
        self.assertFalse(ws._color_mode)
        self.assertIn('00:00:04.000', self.panel.target_text.get())
        for name in self.panel.sections:
            self.panel.scroll_to(name)
            self.root.update_idletasks()
            section = self.panel.sections[name]
            self.assertLess(section.winfo_rooty(), self.panel.canvas.winfo_rooty()+self.panel.canvas.winfo_height())
            self.assertGreater(section.winfo_rooty()+section.winfo_height(), self.panel.canvas.winfo_rooty())
        self.assertEqual(self.errors, [])

    def test_camera_and_color_modules_change_only_selected_range_with_undo(self):
        ws, panel = self.workspace, self.panel
        sources = copy.deepcopy(ws.project.clips)
        panel.camera_var.set(next(name for name, camera in panel.camera_choices.items() if camera == 2))
        panel.apply_camera()
        self.assertEqual(ws.project.camera_at(2.), 2)
        self.assertEqual(ws.project.camera_at(8.), 1)
        ws.undo()
        self.assertEqual(ws.project.camera_at(2.), 1)
        ws.select_program_range(0., 4.)
        panel.grade_vars['exposure'].set('0.75')
        panel.grade_vars['contrast'].set('1.1')
        panel.apply_color()
        self.assertEqual(grade_at(ws.project, 2.).exposure, .75)
        self.assertIsNone(grade_at(ws.project, 5.))
        self.assertEqual(ws.project.clips, sources)
        ws.undo()
        self.assertIsNone(grade_at(ws.project, 2.))
        ws.redo()
        self.assertEqual(grade_at(ws.project, 2.).contrast, 1.1)
        self.dialog.assert_not_called()

    def test_image_and_gyro_parameters_reach_existing_real_processing_interfaces(self):
        panel = self.panel
        panel.image_smoothing.set('1.25')
        panel.image_shakiness.set('7')
        panel.image_quality.set('精细')
        panel.image_zoom.set(False)
        panel.image_tripod.set(True)
        with patch.object(self.workspace, 'start_stabilization', return_value=True) as start:
            self.assertTrue(panel.start_image())
            settings = start.call_args.args[0]
            self.assertEqual((settings.engine, settings.mode), ('image', 'manual'))
            self.assertEqual((settings.image_smoothing, settings.image_shakiness, settings.image_quality), (1.25, 7, 'fine'))
            self.assertFalse(settings.image_zoom)
            self.assertTrue(settings.image_tripod)
            panel.gyro_smoothness.set('0.8')
            panel.gyro_fov.set('1.2')
            panel.gyro_horizon.set('40')
            panel.gyro_zoom.set('固定缩放')
            self.assertTrue(panel.start_gyro())
            settings = start.call_args.args[0]
            self.assertEqual((settings.engine, settings.mode), ('gyroflow', 'manual'))
            self.assertEqual((settings.smoothness, settings.fov, settings.horizon_lock, settings.zoom_mode), (.8, 1.2, 40., 'static'))
        self.dialog.assert_not_called()

    def test_invalid_stabilization_parameters_do_not_start_a_worker_or_modify_project(self):
        original = copy.deepcopy(self.workspace.project)
        self.panel.image_shakiness.set('2.5')
        with patch.object(self.workspace, 'start_stabilization') as start:
            self.panel.start_image()
            start.assert_not_called()
        self.assertEqual(self.workspace.project, original)
        self.assertEqual(self.workspace.undo_stack, [])
        self.dialog.assert_called_once()

    def test_transition_catalog_has_library_masks_and_applies_selected_end(self):
        ws, panel = self.workspace, self.panel
        masks = {name: value for name, value in panel.transition_choices.items() if not isinstance(value, str)}
        self.assertGreater(len(masks), 9)
        name, mask = next(iter(masks.items()))
        panel.transition_query.set(mask.name)
        self.assertIn(name, panel.transition_combo.cget('values'))
        panel.transition_kind.set(name)
        panel.transition_duration.set('0.75')
        with patch.object(ws, 'apply_library_mask', return_value=True) as apply_mask:
            self.assertTrue(panel.apply_transition())
            apply_mask.assert_called_once_with(mask, .75)
        panel.transition_query.set('')
        panel.transition_kind.set('向左擦除')
        panel.transition_duration.set('0.6')
        panel.apply_transition()
        self.assertEqual([(item.time, item.kind, item.duration) for item in ws.project.transitions], [(4., 'wipeleft', .6)])
        self.assertEqual(len(ws.undo_stack), 1)
        ws.undo()
        self.assertEqual(ws.project.transitions, [])
        self.dialog.assert_not_called()

    def test_selection_changes_refresh_range_values_without_touching_project(self):
        ws = self.workspace
        set_grade_on_range(ws.project, 4., 7., ColorGrade(exposure=1.25))
        original = copy.deepcopy(ws.project)
        self.panel.grade_vars['exposure'].set('2')
        ws.select_program_range(4., 7.)
        self.assertEqual(self.panel.grade_vars['exposure'].get(), '1.25')
        self.assertIn('00:00:07.000', self.panel.target_text.get())
        self.assertEqual(ws.project, original)

    def test_library_and_color_pages_stay_in_right_inspector(self):
        from effects_browser import PRESETS
        ws = self.workspace
        initial_media_tabs = ws.tools.tabs()
        with patch('effects_browser.library_presets', return_value=PRESETS):
            browser = ws.show_effects_library()
        self.root.update_idletasks()
        self.assertEqual(browser.master, ws.library_tab)
        self.assertEqual(ws.inspector_tabs.select(), str(ws.library_tab))
        self.assertEqual(ws.tools.tabs(), initial_media_tabs)
        ws.show_inspector('color')
        self.assertEqual(ws.inspector_tabs.select(), str(ws.creative_tab))
        self.assertEqual(ws.creative_panel.tabs.select(), str(ws.creative_panel.pages['color']))
        self.assertEqual(ws.grade_target, 'selection')
        self.assertFalse(ws._color_mode)
        self.assertTrue(ws.timeline.winfo_ismapped())
        self.assertTrue(ws.program_surface.winfo_ismapped())
        ws.show_inspector_tool('transition')
        self.assertEqual(ws.inspector_tabs.select(), str(ws.program_inspector_tab))
        self.assertGreater(len(self.panel.transition_choices), 9)
        self.assertTrue(ws.timeline.winfo_ismapped())
        self.assertEqual(self.errors, [])

    def test_proxy_toolbar_uses_its_own_row_and_reset_hook_is_wired(self):
        ws = self.workspace
        toolbar = ws.quick_proxy_button.master
        self.assertEqual(toolbar.grid_info()['row'], 1)
        self.assertEqual(ws.camera_grid_toolbar.grid_info()['row'], 2)
        self.assertEqual(ws.camera_grid.grid_info()['row'], 3)
        self.assertTrue(ws.quick_proxy_button.winfo_ismapped())
        self.assertIn('1080p', ws.quick_proxy_button.cget('text'))
        for button in (ws.quick_proxy_button, ws.proxy_link_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertEqual(button.winfo_width(), button.winfo_reqwidth())
            self.assertLessEqual(button.winfo_x()+button.winfo_width(), toolbar.winfo_width())
        with patch.object(ws, 'link_existing_proxy', return_value=True) as link:
            self.assertTrue(ws.choose_proxy())
            link.assert_called_once_with()
        with patch.object(ws, '_reset_quick_proxy') as reset, patch.object(ws, '_confirm_discard', return_value=True):
            ws.new()
            reset.assert_called_once()
        self.assertFalse(ws._handle_quick_proxy_job('irrelevant', None))


if __name__ == '__main__':
    unittest.main()
