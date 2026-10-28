"""Real Tk gestures for the colour page, without controlling the user's UI."""
from __future__ import annotations

import copy
import math
from pathlib import Path
import sys
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from color_workspace import ColorWorkspace, balance_from_point, point_from_balance
from creative_core import ColorGrade
from multicam_core import CameraClip, Cut, MulticamProject


class WheelMappingTests(unittest.TestCase):
    def test_positions_are_bounded_and_round_trip(self):
        for angle in range(0, 360, 15):
            x, y = math.cos(math.radians(angle)) * .7, math.sin(math.radians(angle)) * .7
            rgb = balance_from_point(x, y)
            self.assertAlmostEqual(sum(rgb), 0, places=12)
            self.assertTrue(all(-.5 <= value <= .5 for value in rgb))
            px, py = point_from_balance(*rgb)
            self.assertAlmostEqual(px, x, places=12)
            self.assertAlmostEqual(py, y, places=12)

    def test_oversized_radius_stays_inside_control_range(self):
        self.assertEqual(balance_from_point(5, 0), balance_from_point(1, 0))
        self.assertEqual(point_from_balance(.1, .1, .1), (0, 0))


class ColorPageTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()
        self.root.attributes('-alpha', 0)
        self.root.overrideredirect(True)
        self.root.geometry('1280x620+10000+10000')
        self.root.tk.call('tk', 'scaling', 96 / 72)
        self.grade = ColorGrade()
        self.applied = []
        self.selections = []
        self.project = MulticamProject(
            clips=[CameraClip('one', 1, 'a.mp4', 0, 12), CameraClip('two', 2, 'b.mp4', 0, 12)],
            cuts=[Cut(0, 1), Cut(3, 2), Cut(7, 1)], fps=30)
        self.ws = SimpleNamespace(project=self.project, selected_program_range=(3., 7.),
                                  current=4., _available=lambda: True, _px=lambda n: round(n),
                                  split_program_at_playhead=Mock(), preview_effects=Mock(),
                                  exit_color_workspace=Mock(), seek=Mock())
        self.ws.get_selected_grade = lambda: copy.deepcopy(self.grade)
        def apply(grade):
            self.applied.append(copy.deepcopy(grade))
            self.grade = copy.deepcopy(grade)
        def select(start, end, seek=True):
            self.ws.selected_program_range = (start, end)
            self.selections.append((start, end, seek))
            self.panel.refresh()
        self.ws.apply_selected_grade = apply
        self.ws.select_program_range = select
        self.panel = ColorWorkspace(self.root, self.ws)
        self.panel.pack(fill='both', expand=True)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.root.deiconify()
        self.root.update()
        self.root.update_idletasks()

    def tearDown(self):
        if hasattr(self, 'root'):
            self.root.destroy()

    def event(self, time, y=40, shift=False):
        return SimpleNamespace(x=self.panel._x_from_time(time), y=y, state=1 if shift else 0)

    def test_wheel_drag_uses_single_apply_and_preserves_other_wheels(self):
        self.grade = ColorGrade(offset_y=.1, gain_r=.05)
        self.panel.refresh()
        wheel = self.panel.wheels['lift']
        cx, cy = wheel._center
        wheel._press(SimpleNamespace(x=cx, y=cy))
        for fraction in (.2, .4, .6):
            wheel._motion(SimpleNamespace(x=cx + wheel._radius * fraction, y=cy))
        self.assertEqual(self.applied, [])
        wheel._release()
        self.assertEqual(len(self.applied), 1)
        self.assertGreater(self.grade.lift_r, 0)
        self.assertLess(self.grade.lift_g, 0)
        self.assertEqual(self.grade.offset_y, .1)
        self.assertEqual(self.grade.gain_r, .05)

    def test_scope_choice_changes_target_without_changing_project(self):
        original = copy.deepcopy(self.project)
        self.ws.grade_target = 'selection'
        self.ws.clear_grade_preview = Mock()
        def set_target(target):
            self.ws.grade_target = target
        self.ws.set_grade_target = Mock(side_effect=set_target)
        self.panel.refresh()
        self.assertEqual(self.panel.scope_var.get(), '所选片段 · 机位 2')
        self.panel.scope_var.set('整机位 1')
        self.panel._scope_changed()
        self.ws.set_grade_target.assert_called_once_with(1)
        self.assertEqual(self.panel.scope_var.get(), '整机位 1')
        self.assertEqual(self.project, original)
        self.assertEqual(self.applied, [])

    def test_many_motion_previews_never_commit_to_a_new_target(self):
        self.ws.grade_target = 'selection'
        self.ws.get_active_grade = lambda: copy.deepcopy(self.grade)
        self.ws.apply_active_grade = Mock()
        self.ws.preview_active_grade = Mock()
        self.ws.clear_grade_preview = Mock()
        self.panel.refresh()
        wheel = self.panel.wheels['offset']
        cx, cy = wheel._center
        wheel._press(SimpleNamespace(x=cx, y=cy))
        for fraction in (.1, .2, .3, .4):
            wheel._motion(SimpleNamespace(x=cx + wheel._radius*fraction, y=cy))
        self.assertGreaterEqual(self.ws.preview_active_grade.call_count, 4)
        self.ws.apply_active_grade.assert_not_called()
        self.ws.grade_target = 1
        self.panel.refresh()
        wheel._release()
        self.ws.apply_active_grade.assert_not_called()
        self.assertIsNone(wheel.draft)

    def test_y_slider_previews_repeatedly_then_applies_once(self):
        self.ws.grade_target = 'selection'
        self.ws.get_active_grade = lambda: copy.deepcopy(self.grade)
        self.ws.preview_active_grade = Mock()
        def apply(grade):
            self.grade = copy.deepcopy(grade)
        self.ws.apply_active_grade = Mock(side_effect=apply)
        self.panel.refresh()
        wheel = self.panel.wheels['gain']
        wheel._begin_y()
        for value in (.1, .2, .3):
            wheel.y_value.set(value)
            wheel._y_changed(value)
        self.assertGreaterEqual(self.ws.preview_active_grade.call_count, 3)
        self.ws.apply_active_grade.assert_not_called()
        wheel.commit_y()
        self.ws.apply_active_grade.assert_called_once()
        self.assertAlmostEqual(self.grade.gain_y, .3)

    def test_refresh_and_unmodified_focus_out_do_not_change_project(self):
        self.grade.lift_r = .123456789
        self.panel.refresh()
        self.panel.wheels['lift'].commit_entries(SimpleNamespace(keysym=''))
        self.assertEqual(self.applied, [])
        self.assertEqual(self.grade.lift_r, .123456789)

    def test_entries_apply_y_and_rgb_and_reset_one_wheel(self):
        wheel = self.panel.wheels['gain']
        wheel.values['r'].set('.1')
        wheel.values['g'].set('-.05')
        wheel.y_value.set(.2)
        wheel._y_changed(.2)
        wheel.commit_y()
        self.assertEqual(len(self.applied), 1)
        self.assertAlmostEqual(self.grade.gain_r, .1)
        self.assertAlmostEqual(self.grade.gain_g, -.05)
        self.assertAlmostEqual(self.grade.gain_y, .2)
        wheel.reset()
        self.assertEqual(len(self.applied), 2)
        self.assertEqual(self.grade, ColorGrade())

    def test_invalid_entry_does_not_apply(self):
        wheel = self.panel.wheels['gamma']
        wheel.values['r'].set('nan')
        with patch('color_workspace.messagebox.showerror') as dialog:
            wheel.commit_entries()
        dialog.assert_called_once()
        self.assertEqual(self.applied, [])

    def test_click_selects_program_span(self):
        event = self.event(5)
        self.panel._strip_press(event)
        self.panel._strip_release(event)
        self.assertEqual(self.selections, [(3, 7, False)])
        self.ws.seek.assert_called_once_with(5)

    def test_drag_selects_frame_snapped_range_only_on_release(self):
        self.panel._strip_press(self.event(1.234, shift=True))
        self.panel._strip_motion(self.event(8.123, shift=True))
        self.assertEqual(self.selections, [])
        self.panel._strip_release(self.event(8.123, shift=True))
        start, end, seek = self.selections[0]
        self.assertAlmostEqual(start, 37 / 30)
        self.assertAlmostEqual(end, 244 / 30)
        self.assertTrue(seek)
        self.assertEqual(self.applied, [])

    def test_ruler_seeks_without_selecting(self):
        self.panel._strip_press(self.event(2, y=10))
        self.panel._strip_motion(self.event(5, y=10))
        self.panel._strip_release(self.event(5, y=10))
        self.assertEqual([call.args[0] for call in self.ws.seek.call_args_list], [2, 5])
        self.assertEqual(self.selections, [])

    def test_boundary_drag_preserves_other_boundary(self):
        self.panel._strip_press(self.event(3))
        self.panel._strip_motion(self.event(2))
        self.panel._strip_release(self.event(2))
        self.assertEqual(self.selections, [(2, 7, True)])

    def test_range_entries_and_error(self):
        self.panel.range_start_var.set('00:00:01.050')
        self.panel.range_end_var.set('00:00:03.000')
        self.panel.apply_range_entries()
        self.assertEqual(self.selections, [(32 / 30, 3, True)])
        self.panel.range_end_var.set('0')
        with patch('color_workspace.messagebox.showerror') as dialog:
            self.panel.apply_range_entries()
        dialog.assert_called_once()
        self.assertEqual(len(self.selections), 1)

    def test_responsive_layout_and_scroll_at_high_dpi(self):
        self.assertEqual(self.panel._columns, 4)
        self.panel._layout_controls(700)
        self.assertEqual(self.panel._columns, 2)
        self.ws._px = lambda n: round(n * 2.5)
        self.panel._layout_controls(2400)
        self.assertEqual(self.panel._columns, 4)
        self.panel._layout_controls(1280)
        self.assertEqual(self.panel._columns, 2)
        self.root.update_idletasks()
        self.assertTrue(self.panel.controls_canvas.cget('yscrollcommand'))
        self.assertEqual(self.errors, [])

    def test_actual_240dpi_widgets_stay_within_four_column_layout(self):
        self.panel.destroy()
        self.root.withdraw()
        self.root.tk.call('tk', 'scaling', 240 / 72)
        self.root.geometry('3840x1500+10000+10000')
        self.ws._px = lambda n: round(n * 2.5)
        self.panel = ColorWorkspace(self.root, self.ws)
        self.panel.pack(fill='both', expand=True)
        self.root.deiconify()
        self.root.update()
        self.root.update_idletasks()
        self.assertEqual(self.panel._columns, 4)
        canvas_right = self.panel.controls_canvas.winfo_rootx() + self.panel.controls_canvas.winfo_width()
        for wheel in self.panel.wheels.values():
            self.assertLessEqual(wheel.winfo_rootx() + wheel.winfo_width(), canvas_right)
            self.assertGreater(wheel.canvas.winfo_height(), 300)
            self.assertGreater(wheel._radius, 100)
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
