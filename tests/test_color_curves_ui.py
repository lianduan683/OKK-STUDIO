"""Native Tk curve gestures preview drafts and commit the selected Cut once."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from color_workspace import ColorWorkspace
from color_curve_editor import IDENTITY
from creative_core import ColorGrade, curve_points
from multicam_core import CameraClip, Cut, MulticamProject


class ColorCurvesTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()
        self.root.attributes('-alpha', 0)
        self.root.overrideredirect(True)
        self.root.geometry('1280x700+10000+10000')
        self.root.tk.call('tk', 'scaling', 96/72)
        self.grade = ColorGrade(gain_r=.1)
        self.applied, self.previews, self.errors = [], [], []
        self.project = MulticamProject(
            clips=[CameraClip('a', 1, 'a.mp4', 0, 12), CameraClip('b', 2, 'b.mp4', 0, 12)],
            cuts=[Cut(0, 1), Cut(4, 2), Cut(8, 1)], fps=30)
        self.ws = SimpleNamespace(project=self.project, current=5., selected_program_range=(4., 8.),
            grade_target='selection', _available=lambda: True, _px=lambda n: round(n),
            split_program_at_playhead=Mock(), preview_effects=Mock(), exit_color_workspace=Mock(),
            seek=Mock(), clear_grade_preview=Mock(), get_active_grade=lambda: copy.deepcopy(self.grade))
        def apply(grade):
            self.grade = copy.deepcopy(grade)
            self.applied.append(copy.deepcopy(grade))
        self.ws.apply_active_grade = apply
        self.ws.preview_active_grade = lambda grade: self.previews.append(copy.deepcopy(grade))
        self.page = ColorWorkspace(self.root, self.ws)
        self.page.pack(fill='both', expand=True)
        self.page.grading_tabs.select(self.page.curves_tab)
        self.curve = self.page.curve_editor
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.root.deiconify()
        self.root.update()
        self.root.update_idletasks()
        self.ws.clear_grade_preview.reset_mock()

    def tearDown(self):
        if hasattr(self, 'root'):
            self.root.destroy()

    def event(self, x, y):
        sx, sy = self.curve._to_screen(x, y)
        return SimpleNamespace(x=sx, y=sy)

    def add_point(self, x, y):
        event = self.event(x, y)
        self.curve._press(event)
        self.curve._release(event)

    def test_click_and_many_drags_preview_then_commit_one_grade(self):
        before = copy.deepcopy(self.grade)
        self.curve._press(self.event(.3, .6))
        self.curve._motion(self.event(.35, .7))
        self.curve._motion(self.event(.4, .75))
        self.assertEqual(self.grade, before)
        self.assertEqual(self.applied, [])
        self.assertGreaterEqual(len(self.previews), 3)
        self.curve._release()
        self.assertEqual(len(self.applied), 1)
        points = curve_points(self.grade, 'master')
        self.assertEqual(len(points), 3)
        self.assertAlmostEqual(points[1][0], .4)
        self.assertAlmostEqual(points[1][1], .75)
        self.assertEqual(self.grade.gain_r, .1)
        self.curve._release()
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.errors, [])

    def test_endpoint_x_remains_fixed_and_y_is_editable(self):
        self.curve._press(self.event(0, 0))
        self.curve._motion(self.event(.4, .3))
        self.curve._release()
        x, y = curve_points(self.grade, 'master')[0]
        self.assertEqual(x, 0.)
        self.assertAlmostEqual(y, .3)
        self.curve._press(self.event(1, 1))
        self.curve._motion(self.event(.6, .8))
        self.curve._release()
        x, y = curve_points(self.grade, 'master')[-1]
        self.assertEqual(x, 1.)
        self.assertAlmostEqual(y, .8)

    def test_channels_and_reset_current_or_all_preserve_other_controls(self):
        self.curve.channel_var.set('红色 R')
        self.curve._channel_changed()
        self.add_point(.4, .7)
        self.curve.channel_var.set('绿色 G')
        self.curve._channel_changed()
        self.add_point(.3, .2)
        self.assertNotEqual(self.grade.curve_r, IDENTITY)
        self.assertNotEqual(self.grade.curve_g, IDENTITY)
        self.curve.reset_channel()
        self.assertEqual(self.grade.curve_g, IDENTITY)
        self.assertNotEqual(self.grade.curve_r, IDENTITY)
        self.curve.reset_all()
        self.assertTrue(all(curve_points(self.grade, channel) == IDENTITY for channel in ('master', 'r', 'g', 'b')))
        self.assertEqual(self.grade.gain_r, .1)
        self.assertEqual(len(self.applied), 4)

    def test_right_click_deletes_only_internal_points(self):
        self.add_point(.5, .7)
        self.curve._delete_point(self.event(0, 0))
        self.curve._delete_point(self.event(1, 1))
        self.assertEqual(len(self.applied), 1)
        self.curve._delete_point(self.event(.5, .7))
        self.assertEqual(len(self.applied), 2)
        self.assertEqual(self.grade.curve_master, IDENTITY)

    def test_eight_point_limit_does_not_create_hidden_or_invalid_point(self):
        self.grade.curve_master = tuple((index/7, index/7) for index in range(8))
        self.page.refresh()
        self.add_point(.11, .85)
        self.assertEqual(self.applied, [])
        self.assertEqual(len(self.grade.curve_master), 8)
        self.assertIn('最多 8', self.curve.info_var.get())

    def test_switch_target_or_selected_cut_discards_pending_curve(self):
        before = copy.deepcopy(self.grade)
        self.curve._press(self.event(.4, .6))
        self.ws.grade_target = 2
        self.page.refresh()
        self.curve._release()
        self.assertEqual(self.applied, [])
        self.assertEqual(self.grade, before)
        self.ws.grade_target = 'selection'
        self.page.refresh()
        self.curve._press(self.event(.3, .7))
        self.ws.selected_program_range = (8., 12.)
        self.page.refresh()
        self.curve._release()
        self.assertEqual(self.applied, [])
        self.assertEqual(self.grade, before)
        self.assertIsNone(self.curve.draft)

    def test_focus_out_or_tab_change_cancels_without_undo(self):
        self.curve._press(self.event(.4, .7))
        self.curve._focus_out()
        self.curve._release()
        self.assertEqual(self.applied, [])
        self.ws.clear_grade_preview.assert_called()
        self.curve._press(self.event(.3, .6))
        self.page.grading_tabs.select(self.page.wheels_tab)
        self.root.update()
        self.assertIsNone(self.curve.draft)
        self.assertEqual(self.applied, [])

    def test_plot_and_controls_are_visible_and_target_is_selected_cut(self):
        self.assertTrue(self.curve.canvas.winfo_ismapped())
        self.assertGreater(self.curve.canvas.winfo_height(), 100)
        self.assertEqual(self.page.scope_var.get(), '所选片段 · 机位 2')
        self.assertEqual(self.ws.grade_target, 'selection')
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
