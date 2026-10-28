"""Off-screen native UI integration for camera targets and live grade drafts.

Actual Tk controls and workspace transactions are used.  A mock player records
commands, so no GPU, FFmpeg, system input or user video is touched.
"""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import threading
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creative_core import ColorGrade
from exporter_core import Capabilities
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_ui import MulticamWorkspace
from program_edits import grade_at


class RealtimeGradeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk display unavailable: {error}')
        self.root.withdraw()
        self.root.tk.call('tk', 'scaling', 96/72)
        self.root.attributes('-alpha', 0)
        self.root.overrideredirect(True)
        self.root.geometry('1280x820+10000+10000')
        self.temp = tempfile.TemporaryDirectory(prefix='realtime_grade_ui_')
        self.folder = Path(self.temp.name)
        for name in ('a.mp4', 'b.mp4'):
            (self.folder/name).write_bytes(b'fixture path only')
        self.root.working = False
        self.root.mpv_path = ''
        self.root.mpv = None
        self.root.cancel_event = threading.Event()
        self.root.caps = Capabilities(ffmpeg_path='test-ffmpeg.exe', ffprobe_path='test-ffprobe.exe')
        self.root.progress_var = tk.DoubleVar(self.root)
        self.root.progress_text_var = tk.StringVar(self.root)
        self.root._log = Mock()
        self.root._run_editor_process = Mock()
        self.root._begin_shared_progress = Mock()
        self.root._set_working = lambda active: setattr(self.root, 'working', bool(active))
        self.callback_errors = []
        self.root.report_callback_exception = lambda *details: self.callback_errors.append(details)
        self.patches = [patch('multicam_ui.messagebox.showerror'),
                        patch('multicam_ui.messagebox.showwarning'),
                        patch('multicam_ui.messagebox.showinfo'),
                        patch('multicam_ui.messagebox.askyesnocancel', return_value=False)]
        self.dialogs = [item.start() for item in self.patches]
        self.workspace = MulticamWorkspace(self.root, self.root)
        self.workspace.pack(fill='both', expand=True)
        self.workspace.project = MulticamProject(
            clips=[CameraClip('a', 1, str(self.folder/'a.mp4'), 0., 12.),
                   CameraClip('b', 2, str(self.folder/'b.mp4'), 0., 12.)],
            cuts=[Cut(0., 1), Cut(3., 2), Cut(7., 1)], fps=30.)
        self.workspace.fps_var.set('30')
        self.workspace.current = 4.
        self.workspace.selected_program_range = (3., 7.)
        self.workspace._refresh()
        self.workspace.dirty = False  # Simulate the clean, loaded fixture.
        self.root.deiconify()
        self.root.update()
        self.player = Mock()
        self.workspace.player = self.player
        self.workspace.show_creative('color')
        self.root.update()
        self.assertEqual(self.callback_errors, [])
        self.player.reset_mock()

    def tearDown(self):
        if hasattr(self, 'workspace'): self.workspace.close()
        if hasattr(self, 'root'):
            for pending in self.root.tk.call('after', 'info'):
                self.root.tk.call('after', 'cancel', pending)
            self.root.destroy()
        for item in getattr(self, 'patches', []): item.stop()
        if hasattr(self, 'temp'): self.temp.cleanup()

    def key(self, number):
        return self.workspace._key(SimpleNamespace(widget=self.workspace.color_workspace.strip,
                                                  keysym=str(number), char=str(number), state=0))

    def previews(self):
        return [call for call in self.player.submit.call_args_list if call.args[0] == 'grade_preview']

    def flush(self):
        pending = self.workspace._grade_preview_after
        if pending is not None:
            self.workspace.after_cancel(pending)
            self.workspace._grade_preview_after = None
        self.workspace._flush_grade_preview()

    def drag(self, name='lift'):
        wheel = self.workspace.color_workspace.wheels[name]
        cx, cy = wheel._center
        wheel._press(SimpleNamespace(x=cx, y=cy))
        for fraction in (.2, .4, .6):
            wheel._motion(SimpleNamespace(x=cx+wheel._radius*fraction, y=cy))
        return wheel

    def assert_no_jobs_or_errors(self):
        self.assertEqual(self.callback_errors, [])
        self.dialogs[0].assert_not_called()
        self.root._run_editor_process.assert_not_called()

    def test_camera_targets_have_independent_values_and_never_edit_programme_cuts(self):
        workspace = self.workspace
        cuts = copy.deepcopy(workspace.project.cuts)
        self.assertEqual(workspace.grade_target, 'selection', 'Colour mode should grade only the selected cut by default')
        grade_one = ColorGrade(lift_r=.12, gamma_y=.07)
        grade_two = ColorGrade(gain_b=.2, offset_y=.03)
        workspace.set_grade_target(1)
        workspace.apply_active_grade(grade_one)
        workspace.set_grade_target(2)
        self.assertEqual(workspace.get_active_grade(), ColorGrade())
        workspace.apply_active_grade(grade_two)
        workspace.set_grade_target(1)
        self.assertEqual(workspace.get_active_grade(), grade_one)
        returned = workspace.get_active_grade()
        returned.lift_r = .4
        self.assertEqual(workspace.project.camera_grades[1], grade_one)
        self.assertEqual(workspace.project.camera_grades[2], grade_two)
        self.assertEqual(workspace.project.cuts, cuts)
        self.assertEqual(workspace.project.clip_grades, [])
        self.assertEqual(len(workspace.undo_stack), 2)
        self.player.submit.assert_any_call('audition', camera=1)
        self.assert_no_jobs_or_errors()

    def test_colour_and_timeline_numbers_switch_only_the_selected_span(self):
        workspace = self.workspace
        original = copy.deepcopy(workspace.project)
        self.assertIn('机位 2', workspace.color_workspace.scope_var.get())
        self.assertEqual(self.key(1), 'break')
        self.assertEqual(workspace.grade_target, 'selection')
        self.assertIn('机位 1', workspace.color_workspace.scope_var.get())
        self.assertEqual(workspace.selected_program_range, (3., 7.))
        self.assertEqual(workspace.project.camera_at(4.), 1)
        self.assertEqual(workspace.project.camera_at(1.), original.camera_at(1.))
        self.assertEqual(workspace.project.camera_at(8.), original.camera_at(8.))
        self.assertEqual(len(workspace.undo_stack), 1)
        workspace.undo()
        workspace.exit_color_workspace()
        workspace.select_program_range(3., 7.)
        self.key(1)
        self.assertEqual(workspace.project.camera_at(4.), 1)
        self.assertEqual(workspace.project.camera_at(1.), original.camera_at(1.))
        self.assertEqual(workspace.project.camera_at(8.), original.camera_at(8.))
        self.assertEqual(len(workspace.undo_stack), 1)
        self.assert_no_jobs_or_errors()

    def test_selected_cut_realtime_grade_does_not_change_other_cuts_of_same_camera(self):
        workspace = self.workspace
        workspace.project.cuts = [Cut(0., 1)]
        workspace.project.edit_points = [4., 8.]
        workspace._refresh()
        workspace.select_program_range(0., 4.)
        self.assertEqual(workspace.grade_target, 'selection')
        first_grade = ColorGrade(gamma_y=.12)
        workspace.apply_active_grade(first_grade)
        workspace.select_program_range(4., 8.)
        self.assertEqual(workspace.get_active_grade(), ColorGrade())
        self.player.reset_mock()
        before = copy.deepcopy(workspace.project)
        history = len(workspace.undo_stack)
        wheel = self.drag('gain')
        self.flush()
        self.assertEqual(workspace.project, before)
        self.assertEqual(len(workspace.undo_stack), history)
        self.assertEqual(self.previews()[-1].kwargs['target'], ('selection', 4., 8.))
        wheel._release()
        self.assertGreater(grade_at(workspace.project, 5.).gain_r, 0.)
        self.assertEqual(grade_at(workspace.project, 2.), first_grade)
        self.assertIsNone(grade_at(workspace.project, 10.))
        self.assertEqual(workspace.project.camera_grades, {})
        self.assertEqual(workspace.project.global_grade, ColorGrade())
        self.assertEqual(len(workspace.undo_stack), history+1)
        self.assert_no_jobs_or_errors()

    def test_selecting_another_cut_discards_old_draft_without_leaking_colour(self):
        workspace = self.workspace
        workspace.project.edit_points = [5.]
        workspace._refresh()
        workspace.select_program_range(3., 5.)
        original = copy.deepcopy(workspace.project)
        wheel = self.drag('lift')
        self.flush()
        workspace.select_program_range(5., 7.)
        self.flush()
        wheel._release()
        self.assertEqual(workspace.grade_target, 'selection')
        self.assertEqual(workspace.selected_program_range, (5., 7.))
        self.assertEqual(workspace.get_active_grade(), ColorGrade())
        self.assertEqual(workspace.project, original)
        self.assertEqual(workspace.undo_stack, [])
        self.assertTrue(any(call.kwargs.get('grade', object()) is None for call in self.previews()))
        self.assert_no_jobs_or_errors()

    def test_number_entry_preview_and_late_focusout_do_not_cross_cut_boundary(self):
        workspace = self.workspace
        workspace.project.edit_points = [5.]
        workspace._refresh()
        workspace.select_program_range(3., 5.)
        original = copy.deepcopy(workspace.project)
        wheel = workspace.color_workspace.wheels['gain']
        wheel._entry_focus()
        wheel.values['r'].set('.22')
        wheel._preview_entries()
        self.flush()
        self.assertEqual(workspace.project, original)
        self.assertEqual(self.previews()[-1].kwargs['target'], ('selection', 3., 5.))
        self.assertAlmostEqual(self.previews()[-1].kwargs['grade'].gain_r, .22)
        workspace.select_program_range(5., 7.)
        wheel._focus_out()
        wheel.commit_entries(SimpleNamespace(keysym='Return'))
        self.assertEqual(workspace.project, original)
        self.assertEqual(workspace.undo_stack, [])
        self.assertEqual(workspace.get_active_grade(), ColorGrade())
        self.assert_no_jobs_or_errors()

    def test_basic_colour_entries_preview_live_and_commit_only_selected_cut(self):
        workspace = self.workspace
        panel = workspace.creative_panel
        original = copy.deepcopy(workspace.project)
        panel.grade_vars['exposure'].set('.35')
        panel.preview_grade()
        self.flush()
        self.assertEqual(workspace.project, original)
        self.assertEqual(workspace.undo_stack, [])
        self.assertEqual(self.previews()[-1].kwargs['target'], ('selection', 3., 7.))
        self.assertAlmostEqual(self.previews()[-1].kwargs['grade'].exposure, .35)
        panel.apply_grade()
        self.assertAlmostEqual(grade_at(workspace.project, 4.).exposure, .35)
        self.assertIsNone(grade_at(workspace.project, 1.))
        self.assertIsNone(grade_at(workspace.project, 8.))
        self.assertEqual(workspace.project.camera_grades, {})
        self.assertEqual(workspace.project.global_grade, ColorGrade())
        self.assertEqual(len(workspace.undo_stack), 1)
        self.assert_no_jobs_or_errors()

    def test_paused_wheel_motion_previews_without_save_or_undo_release_commits_once(self):
        workspace = self.workspace
        workspace.set_grade_target(1)
        workspace.playing = False
        original = copy.deepcopy(workspace.project)
        self.player.reset_mock()
        with patch.object(workspace, 'preview_active_grade', wraps=workspace.preview_active_grade) as preview:
            wheel = self.drag()
            self.assertGreaterEqual(preview.call_count, 3)
            self.flush()
        self.assertEqual(workspace.project, original)
        self.assertEqual(workspace.undo_stack, [])
        self.assertFalse(workspace.dirty)
        self.assertTrue(self.previews())
        command = self.previews()[-1]
        self.assertEqual(command.kwargs['target'], ('camera', 1))
        self.assertGreater(command.kwargs['grade'].lift_r, 0.)
        self.assertFalse(workspace.playing)
        self.assertNotIn('configure', [call.args[0] for call in self.player.submit.call_args_list])
        wheel._release()
        self.assertEqual(len(workspace.undo_stack), 1)
        self.assertGreater(workspace.project.camera_grades[1].lift_r, 0.)
        self.assertNotIn(2, workspace.project.camera_grades)
        self.assertTrue(workspace.dirty)
        wheel._release()
        self.assertEqual(len(workspace.undo_stack), 1)
        self.assert_no_jobs_or_errors()

    def test_switch_target_discards_pending_gesture_without_committing_to_new_camera(self):
        workspace = self.workspace
        workspace.set_grade_target(1)
        original = copy.deepcopy(workspace.project)
        wheel = self.drag('gain')
        self.flush()
        workspace.set_grade_target(2)
        self.flush()
        wheel._release()
        self.assertEqual(workspace.grade_target, 2)
        self.assertEqual(workspace.get_active_grade(), ColorGrade())
        self.assertEqual(workspace.project, original)
        self.assertEqual(workspace.undo_stack, [])
        self.assertTrue(any(call.kwargs.get('grade', object()) is None for call in self.previews()))
        self.assert_no_jobs_or_errors()

    def test_leaving_colour_clears_draft_and_audition_without_saving_draft(self):
        workspace = self.workspace
        workspace.set_grade_target(1)
        original = copy.deepcopy(workspace.project)
        wheel = self.drag('offset')
        self.flush()
        workspace.exit_color_workspace()
        self.flush()
        self.assertFalse(workspace._color_mode)
        self.player.submit.assert_any_call('audition', camera=None)
        self.assertTrue(any(call.kwargs.get('grade', object()) is None for call in self.previews()))
        self.assertEqual(workspace.project, original)
        self.assertEqual(workspace.undo_stack, [])
        wheel._release()
        self.assertEqual(workspace.project, original, 'A delayed release after leaving must discard its draft')
        self.assert_no_jobs_or_errors()

    def test_camera_grades_save_restore_and_undo_does_not_serialize_draft(self):
        workspace = self.workspace
        one, two = ColorGrade(lift_r=.15), ColorGrade(gain_b=.2)
        workspace.set_grade_target(1)
        workspace.apply_active_grade(one)
        workspace.set_grade_target(2)
        workspace.apply_active_grade(two)
        workspace.preview_active_grade(ColorGrade(offset_r=.3))
        self.flush()
        path = self.folder/'独立机位调色.multicam.json'
        workspace.project_path = str(path)
        self.assertTrue(workspace.save())
        saved = MulticamProject.load(path)
        self.assertEqual(saved.camera_grades, {1:one, 2:two})
        self.assertEqual(saved.cuts, [Cut(0., 1), Cut(3., 2), Cut(7., 1)])
        workspace.undo()
        self.assertEqual(workspace.project.camera_grades, {1:one})
        self.assertTrue(any(call.kwargs.get('grade', object()) is None for call in self.previews()))
        workspace.redo()
        self.assertEqual(workspace.project.camera_grades, {1:one, 2:two})
        workspace.load_project(str(path))
        workspace.show_creative('color')
        workspace.set_grade_target(2)
        self.assertEqual(workspace.get_active_grade(), two)
        self.assert_no_jobs_or_errors()

    def test_global_and_selection_target_keep_their_independent_persisted_scope(self):
        workspace = self.workspace
        workspace.set_grade_target(1)
        camera_grade = ColorGrade(lift_g=.1)
        workspace.apply_active_grade(camera_grade)
        global_grade = ColorGrade(exposure=.2)
        workspace.set_grade_target(None)
        workspace.apply_active_grade(global_grade)
        workspace.preview_active_grade(ColorGrade(saturation=.8))
        self.flush()
        self.assertEqual(self.previews()[-1].kwargs['target'], ('global',))
        workspace.set_grade_target('selection')
        selected_grade = ColorGrade(gamma_y=.15)
        workspace.apply_active_grade(selected_grade)
        workspace.preview_active_grade(ColorGrade(offset_y=.02))
        self.flush()
        self.assertEqual(self.previews()[-1].kwargs['target'], ('selection', 3., 7.))
        self.assertEqual(grade_at(workspace.project, 4.), selected_grade)
        self.assertIsNone(grade_at(workspace.project, 1.))
        self.assertEqual(workspace.project.global_grade, global_grade)
        self.assertEqual(workspace.project.camera_grades[1], camera_grade)
        self.assertEqual(workspace.get_active_grade(), selected_grade)
        self.assert_no_jobs_or_errors()

    def test_busy_export_blocks_preview_and_commit(self):
        workspace = self.workspace
        workspace.set_grade_target(1)
        original = copy.deepcopy(workspace.project)
        self.player.reset_mock()
        workspace.busy = True
        self.root.working = True
        workspace.preview_active_grade(ColorGrade(lift_r=.2))
        workspace.apply_active_grade(ColorGrade(lift_r=.2))
        self.flush()
        self.assertEqual(workspace.project, original)
        self.assertEqual(workspace.undo_stack, [])
        self.assertEqual(self.previews(), [])
        workspace.busy = False
        self.root.working = False
        self.assert_no_jobs_or_errors()

    def test_program_swap_event_lifts_existing_buffer_then_acknowledges_without_project_edits(self):
        workspace = self.workspace
        self.assertEqual(len(workspace.program_views), 2)
        surfaces = dict(workspace.camera_surfaces)
        surface_ids = {camera:surface.winfo_id() for camera,surface in surfaces.items()}
        views = list(workspace.program_views)
        window = views[1].winfo_id()
        project = copy.deepcopy(workspace.project)
        current, playing = workspace.current, workspace.playing
        self.player.reset_mock()
        with patch.object(views[1], 'lift', wraps=views[1].lift) as lift:
            workspace.player_queue.put(('program_swap', window))
            workspace._poll()
        lift.assert_called_once()
        self.assertEqual(workspace._program_front_window, window)
        self.player.submit.assert_called_once_with('program_swap_ack', window=window)
        self.assertEqual(workspace.program_views, views)
        self.assertTrue(all(view.winfo_exists() for view in views))
        self.assertEqual(workspace.camera_surfaces, surfaces)
        self.assertEqual({camera:surface.winfo_id() for camera,surface in surfaces.items()}, surface_ids)
        self.assertEqual(workspace.project, project)
        self.assertEqual((workspace.current, workspace.playing), (current, playing))
        self.player.reset_mock()
        workspace.player_queue.put(('program_swap', -1))
        workspace._poll()
        self.assertEqual(workspace._program_front_window, window)
        self.player.submit.assert_not_called()
        workspace.player = None
        workspace.player_queue.put(('program_swap', views[0].winfo_id()))
        workspace._poll()
        self.assertEqual(workspace._program_front_window, window, 'A stopped player must not process stale swap events')
        workspace.player = self.player
        self.assert_no_jobs_or_errors()


if __name__ == '__main__':
    unittest.main()
