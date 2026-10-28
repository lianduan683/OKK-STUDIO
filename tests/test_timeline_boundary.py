"""Programme boundary operations and actual Tk press/move/release wiring."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creative_core import ColorGrade, Transition, validate_transitions
from multicam_core import CameraClip, Cut, MulticamProject
from program_edits import ClipGrade, program_spans, validate_program_edits
from timeline_boundary import boundary_limits, move_program_boundary
from editor_core import AlignmentResult
try:
    from . import test_timeline_drag_merge as drop_tests
except ImportError:
    import test_timeline_drag_merge as drop_tests


def project(fps=30.):
    return MulticamProject(clips=[CameraClip('a', 1, 'a.mp4', 0., 12.),
                                   CameraClip('b', 2, 'b.mp4', 0., 12.)],
        cuts=[Cut(0., 1), Cut(4., 2), Cut(8., 1)], fps=fps,
        edit_points=[4., 8.],
        clip_grades=[ClipGrade(0., 4., ColorGrade(exposure=.1)),
                     ClipGrade(4., 8., ColorGrade(exposure=.2))])


class ProgramBoundaryCoreTests(unittest.TestCase):
    def test_moves_cut_explicit_edit_and_both_grade_edges_together(self):
        value = project()
        sources = copy.deepcopy(value.clips)
        self.assertEqual(move_program_boundary(value, 4., 5.), 5.)
        self.assertEqual([cut.time for cut in value.cuts], [0., 5., 8.])
        self.assertEqual(value.edit_points, [5., 8.])
        self.assertEqual([(grade.start, grade.end) for grade in value.clip_grades], [(0., 5.), (5., 8.)])
        self.assertEqual(value.clips, sources)
        self.assertEqual(value.camera_at(4.5), 1)
        self.assertEqual(value.camera_at(5.), 2)
        validate_program_edits(value)

    def test_adjacent_cuts_keep_at_least_one_frame(self):
        value = project()
        self.assertEqual(boundary_limits(value, 4.), (1, 239))
        self.assertEqual(move_program_boundary(value, 4., 20.), 239/30)
        self.assertEqual(move_program_boundary(value, 239/30, -20.), 1/30)
        validate_program_edits(value)

    def test_fractional_fps_boundary_uses_integer_frames(self):
        value = project(30000/1001)
        value.cuts = [Cut(0., 1), Cut(100/value.fps, 2), Cut(200/value.fps, 1)]
        value.edit_points = []
        value.clip_grades = []
        self.assertEqual(move_program_boundary(value, 100/value.fps, 123.2/value.fps), 123/value.fps)
        self.assertEqual(value.cuts[1].time, 123/value.fps)

    def test_transition_moves_with_cut_and_neighbour_windows_remain_intact(self):
        value = project()
        value.transitions = [Transition(4., 'fade', 1.), Transition(8., 'fade', 1.)]
        actual = move_program_boundary(value, 4., 20.)
        self.assertEqual(actual, 7.)
        self.assertEqual([(item.time, item.duration) for item in value.transitions], [(7., 1.), (8., 1.)])
        validate_transitions(value.transitions, value)
        validate_program_edits(value)

    def test_same_camera_manual_cut_can_move_without_inventing_camera_switch(self):
        value = project()
        value.cuts = [Cut(0., 1)]
        value.clip_grades = []
        move_program_boundary(value, 4., 6.)
        self.assertEqual(value.cuts, [Cut(0., 1)])
        self.assertEqual(value.edit_points, [6., 8.])
        self.assertEqual([(span.start, span.end) for span in program_spans(value)], [(0., 6.), (6., 8.), (8., 12.)])

    def test_programme_edges_and_non_boundaries_are_rejected(self):
        value = project()
        for point in (0., 12., 3.):
            with self.assertRaises(ValueError):
                move_program_boundary(value, point, 5.)
        with self.assertRaises(ValueError):
            boundary_limits(MulticamProject(), 0.)

    def test_baked_overlay_blocks_only_its_adjacent_programme_boundaries(self):
        value = project()
        value.library_overlays = [SimpleNamespace(start=0., end=4., camera=1, source_id='a')]
        with self.assertRaisesRegex(ValueError, '先移除特效'):
            move_program_boundary(value, 4., 5.)
        self.assertEqual(move_program_boundary(value, 8., 9.), 9.)


class ProgramBoundaryTkTests(unittest.TestCase):
    setUp = drop_tests.TimelineDropTests.setUp
    tearDown = drop_tests.TimelineDropTests.tearDown

    def prepare(self):
        self.workspace.project = project()
        self.workspace._refresh()
        self.root.update_idletasks()

    def event_xy(self, time):
        workspace = self.workspace
        canvas = workspace.timeline
        top, bottom = workspace._timeline_program_bounds
        return dict(x=round(workspace._timeline_x(time)-canvas.canvasx(0)),
                    y=round((top+bottom)/2-canvas.canvasy(0)))

    def test_real_mouse_drag_moves_cut_not_source_and_is_one_undo(self):
        self.prepare()
        workspace = self.workspace
        original = copy.deepcopy(workspace.project)
        workspace.selected_program_range = (4., 8.)
        canvas = workspace.timeline
        canvas.event_generate('<Motion>', **self.event_xy(4.))
        self.assertEqual(canvas.cget('cursor'), 'sb_h_double_arrow')
        with patch.object(workspace, '_configure_player') as player:
            canvas.event_generate('<ButtonPress-1>', **self.event_xy(4.))
            self.assertIsNotNone(workspace._program_boundary_drag)
            canvas.event_generate('<B1-Motion>', **self.event_xy(5.))
            canvas.event_generate('<B1-Motion>', **self.event_xy(6.))
            player.assert_not_called()
            self.assertEqual(workspace.project.cuts[1].time, 6.)
            canvas.event_generate('<ButtonRelease-1>', **self.event_xy(6.))
            player.assert_called_once()
        self.assertEqual(workspace.selected_program_range, (6., 8.))
        self.assertEqual(workspace.project.clips, original.clips)
        self.assertEqual(len(workspace.undo_stack), 1)
        workspace.undo()
        self.assertEqual(workspace.project, original)
        workspace.redo()
        self.assertEqual(workspace.project.cuts[1].time, 6.)
        self.assertEqual(self.errors, [])

    def test_zoomed_scrolled_cut_drag_clamps_before_next_cut(self):
        self.prepare()
        workspace = self.workspace
        workspace.zoom = 4
        workspace.draw_timeline()
        workspace.timeline.xview_moveto(.2)
        workspace.draw_timeline()
        workspace.timeline.event_generate('<ButtonPress-1>', **self.event_xy(4.))
        # Coordinates deliberately run past the visible viewport; Tk's held
        # mouse grab keeps the drag active, and the model must stay bounded.
        workspace.timeline.event_generate('<B1-Motion>', **self.event_xy(10.))
        workspace.timeline.event_generate('<ButtonRelease-1>', **self.event_xy(10.))
        self.assertEqual(workspace.project.cuts[1].time, 8.-1/30)
        self.assertEqual(workspace.project.edit_points, [8.-1/30, 8.])
        self.assertEqual(self.errors, [])

    def test_escape_restores_grade_boundaries_and_undo_redo_history(self):
        self.prepare()
        workspace = self.workspace
        original = copy.deepcopy(workspace.project)
        workspace.dirty = False
        workspace.redo_stack = [copy.deepcopy(original)]
        workspace.timeline.event_generate('<ButtonPress-1>', **self.event_xy(4.))
        workspace.timeline.event_generate('<B1-Motion>', **self.event_xy(5.))
        workspace._key(SimpleNamespace(widget=workspace.timeline, state=0, keysym='Escape', char=''))
        self.assertEqual(workspace.project, original)
        self.assertFalse(workspace.dirty)
        self.assertEqual(len(workspace.redo_stack), 1)
        self.assertEqual(workspace.undo_stack, [])
        self.assertIsNone(workspace._program_boundary_drag)
        self.assertEqual(self.errors, [])

    def test_boundary_click_without_motion_does_not_dirty_project(self):
        self.prepare()
        workspace = self.workspace
        workspace.dirty = False
        canvas = workspace.timeline
        canvas.event_generate('<ButtonPress-1>', **self.event_xy(4.))
        canvas.event_generate('<ButtonRelease-1>', **self.event_xy(4.))
        self.assertEqual(workspace.undo_stack, [])
        self.assertFalse(workspace.dirty)
        self.assertEqual(self.errors, [])

    def test_transition_boundary_drag_keeps_transition_and_grade_in_sync(self):
        self.prepare()
        workspace = self.workspace
        workspace.project.transitions = [Transition(4., 'fade', 1.)]
        workspace._refresh()
        canvas = workspace.timeline
        canvas.event_generate('<ButtonPress-1>', **self.event_xy(4.))
        self.root.update_idletasks()
        canvas.event_generate('<B1-Motion>', **self.event_xy(5.))
        canvas.event_generate('<ButtonRelease-1>', **self.event_xy(5.))
        self.assertEqual(workspace.project.cuts[1].time, 5.)
        self.assertEqual(workspace.project.transitions, [Transition(5., 'fade', 1.)])
        self.assertEqual(workspace.project.clip_grades[0].end, 5.)
        validate_transitions(workspace.project.transitions, workspace.project)
        self.assertEqual(self.errors, [])

    def test_overlay_blocks_cut_source_drag_and_manual_offset_without_undo(self):
        self.prepare()
        workspace = self.workspace
        workspace.project.library_overlays = [SimpleNamespace(start=0., end=4., camera=1, source_id='a')]
        original = copy.deepcopy(workspace.project)
        canvas = workspace.timeline
        canvas.event_generate('<ButtonPress-1>', **self.event_xy(4.))
        canvas.event_generate('<B1-Motion>', **self.event_xy(5.))
        canvas.event_generate('<ButtonRelease-1>', **self.event_xy(5.))
        self.assertIsNone(workspace._program_boundary_drag)
        self.assertIn('先移除特效', workspace.status.get())
        press = drop_tests.TimelineDropTests.timeline_event(self, 'a')
        workspace.timeline_press(press)
        workspace.timeline_motion(SimpleNamespace(x=press.x+workspace._timeline_scale(), y=press.y))
        workspace.timeline_release(press)
        workspace.media_tree.selection_set('a')
        workspace.offset_var.set('1')
        workspace.apply_offset()
        self.assertEqual(workspace.project, original)
        self.assertEqual(workspace.undo_stack, [])
        self.assertIn('移除叠加效果', workspace.status.get())
        self.assertEqual(self.errors, [])

    def test_overlay_rejects_entire_alignment_before_any_clip_is_changed(self):
        self.prepare()
        workspace = self.workspace
        workspace.project.library_overlays = [SimpleNamespace(start=0., end=4., camera=1, source_id='a')]
        original = copy.deepcopy(workspace.project)
        workspace._finish_alignment([('b', AlignmentResult(-2., .9, 1., 'test')),
                                     ('a', AlignmentResult(-3., .9, 1., 'test'))])
        self.assertEqual(workspace.project, original)
        self.assertEqual(workspace.undo_stack, [])
        self.assertIn('移除叠加效果', workspace.status.get())

    def test_effect_library_opens_beside_timeline_after_mixin_integration(self):
        from effects_browser import PRESETS
        with patch('effects_browser.library_presets', return_value=PRESETS):
            browser = self.workspace.show_effects_library()
        self.root.update_idletasks()
        self.assertEqual(self.workspace.inspector_tabs.select(), str(self.workspace.library_tab))
        self.assertEqual(browser.master, self.workspace.library_tab)
        self.assertIn(str(self.workspace.timeline_section), self.workspace.workspace_split.panes())
        self.assertTrue(callable(self.workspace.apply_library_overlay))
        self.assertFalse(self.workspace._handle_library_job('unrelated', None))
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
