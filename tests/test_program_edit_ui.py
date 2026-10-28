"""Native workspace integration for frame edits, selection and colour mode.

The test window is transparent and off screen.  No user apps, media decoders,
GPU jobs or system input are used; actual Tk widgets and callbacks are tested.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creative_core import ColorGrade, validate_transitions
from exporter_core import Capabilities
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_ui import ACCENT, MulticamWorkspace
from program_edits import grade_at, program_spans


class ProgramEditWorkspaceTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk display unavailable: {error}')
        self.root.withdraw()
        self.root.tk.call('tk', 'scaling', 96 / 72)
        self.root.attributes('-alpha', 0)
        self.root.overrideredirect(True)
        self.root.geometry('1280x820+10000+10000')
        self.temp = tempfile.TemporaryDirectory(prefix='program_edit_ui_')
        self.folder = Path(self.temp.name)
        for name in ('a.mp4', 'b.mp4'):
            (self.folder / name).write_bytes(b'fixture path only')
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
            clips=[CameraClip('a', 1, str(self.folder/'a.mp4'), 0, 12),
                   CameraClip('b', 2, str(self.folder/'b.mp4'), 0, 12)],
            cuts=[Cut(0, 1)], fps=30.)
        self.workspace._refresh()
        self.root.deiconify()
        self.root.update()
        self.assertEqual(self.callback_errors, [])

    def tearDown(self):
        if hasattr(self, 'workspace'):
            self.workspace.close()
        if hasattr(self, 'root'):
            for pending in self.root.tk.call('after', 'info'):
                self.root.tk.call('after', 'cancel', pending)
            self.root.destroy()
        for item in getattr(self, 'patches', []): item.stop()
        if hasattr(self, 'temp'): self.temp.cleanup()

    def key(self, keysym, char='', widget=None, state=0):
        return self.workspace._key(SimpleNamespace(keysym=keysym, char=char, state=state,
                                                  widget=widget or self.workspace.timeline))

    def program_event(self, selected):
        workspace = self.workspace
        canvas = workspace.timeline
        item = next(item for item, value in workspace.timeline_program_items.items()
                    if value == selected and canvas.type(item) == 'rectangle')
        left, top, right, bottom = canvas.coords(item)
        x, y = (left+right)/2-canvas.canvasx(0), (top+bottom)/2-canvas.canvasy(0)
        return SimpleNamespace(x=x, y=y, x_root=int(x)+10000, y_root=int(y)+10000)

    def context(self, selected):
        workspace = self.workspace
        with patch('multicam_ui.tk.Menu.tk_popup') as popup:
            workspace.timeline_context(self.program_event(selected))
        popup.assert_called_once()
        return workspace.timeline_context_menu

    @staticmethod
    def cascade(menu, index):
        return menu.nametowidget(menu.entrycget(index, 'menu'))

    def assert_clean_callbacks(self):
        self.assertEqual(self.callback_errors, [])
        self.dialogs[0].assert_not_called()
        self.root._run_editor_process.assert_not_called()

    def test_mouse_selection_c_cut_and_repeat_select_right_segment_and_persist(self):
        workspace = self.workspace
        workspace.timeline_press(self.program_event((0., 12.)))
        workspace.timeline_release(SimpleNamespace())
        self.assertEqual(workspace.selected_program_range, (0., 12.))
        self.assertEqual(workspace.current, 6.)
        self.assertEqual(self.key('c', 'c'), 'break')
        self.assertEqual(workspace.project.edit_points, [6.])
        self.assertEqual(workspace.selected_program_range, (6., 12.))
        self.assertEqual([(span.start, span.end, span.camera) for span in program_spans(workspace.project)],
                         [(0., 6., 1), (6., 12., 1)])
        before = len(workspace.undo_stack)
        self.key('c', 'c')
        self.assertEqual(len(workspace.undo_stack), before, 'C at existing edge must be a no-op')
        self.key('Right')
        self.key('c', 'c')
        self.assertEqual(workspace.project.edit_points, [6., 6.+1/30])
        self.assertEqual(workspace.selected_program_range, (6.+1/30, 12.))
        path = self.folder/'手切工程.multicam.json'
        workspace.project_path = str(path)
        self.assertTrue(workspace.save())
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['version'], 8)
        loaded = MulticamProject.load(path)
        self.assertEqual(loaded.edit_points, workspace.project.edit_points)
        self.assertEqual(program_spans(loaded), program_spans(workspace.project))
        self.assert_clean_callbacks()

    def test_number_changes_selected_span_and_preserves_surrounding_camera(self):
        workspace = self.workspace
        workspace.seek(2.)
        self.key('c', 'c')
        workspace.seek(4.)
        self.key('c', 'c')
        workspace.timeline_press(self.program_event((2., 4.)))
        workspace.timeline_release(SimpleNamespace())
        self.assertEqual(workspace.selected_program_range, (2., 4.))
        original = copy.deepcopy(workspace.project)
        self.assertEqual(self.key('2', '2'), 'break')
        self.assertEqual(workspace.selected_program_range, (2., 4.))
        for position in (0., 1., 1.9, 4., 5., 11.9):
            self.assertEqual(workspace.project.camera_at(position), original.camera_at(position))
        for position in (2., 3., 4.-1/30):
            self.assertEqual(workspace.project.camera_at(position), 2)
        self.assertEqual([(span.start, span.end) for span in program_spans(workspace.project)],
                         [(0., 2.), (2., 4.), (4., 12.)])
        workspace.undo()
        self.assertEqual(workspace.project, original)
        workspace.redo()
        self.assertEqual(workspace.project.camera_at(3.), 2)
        self.assert_clean_callbacks()

    def test_arrow_keys_move_one_frame_and_input_widgets_keep_their_keys(self):
        workspace = self.workspace
        for fps in (30., 30000/1001):
            workspace.project.fps = fps
            workspace._refresh()
            workspace.seek(60/fps)
            for _ in range(10): self.assertEqual(self.key('Right'), 'break')
            self.assertAlmostEqual(workspace.current, 70/fps, places=10)
            for _ in range(10): self.key('Left')
            self.assertAlmostEqual(workspace.current, 60/fps, places=10)
            workspace.seek(0)
            self.key('Left')
            self.assertEqual(workspace.current, 0.)
        widgets = [tk.Entry(workspace), ttk.Entry(workspace), ttk.Combobox(workspace),
                   tk.Text(workspace), tk.Spinbox(workspace), tk.Scale(workspace), ttk.Scale(workspace)]
        before = copy.deepcopy(workspace.project)
        position = workspace.current
        for widget in widgets:
            for keysym, char in (('c', 'c'), ('2', '2'), ('Right', ''), ('Left', '')):
                self.assertIsNone(self.key(keysym, char, widget=widget))
            widget.destroy()
        self.assertEqual(workspace.project, before)
        self.assertEqual(workspace.current, position)
        self.assert_clean_callbacks()

    def test_context_menu_switch_and_colour_action_hide_timeline_restore_on_exit(self):
        workspace = self.workspace
        workspace.seek(6.)
        self.key('c', 'c')
        menu = self.context((0., 6.))
        self.assertEqual([menu.entrycget(i, 'label') for i in range(menu.index('end')+1)],
                         ['切换机位', '进入调色界面', '画面防抖（无需陀螺仪）', 'Gyroflow 防抖', '在片段末尾添加转场'])
        cameras = self.cascade(menu, 0)
        self.assertEqual([cameras.entrycget(i, 'label') for i in range(cameras.index('end')+1)],
                         ['机位 1', '机位 2'])
        cameras.invoke(1)
        self.assertEqual(workspace.project.camera_at(1.), 2)
        self.assertEqual(workspace.project.camera_at(7.), 1)
        menu.invoke(1)
        self.root.update()
        self.assertTrue(workspace._color_mode)
        self.assertNotIn(str(workspace.timeline_section), workspace.workspace_split.panes())
        self.assertFalse(workspace.timeline.winfo_ismapped())
        self.assertTrue(workspace.color_workspace.winfo_ismapped())
        self.assertEqual(workspace.creative_panel.grade_scope_var.get(), '所选片段 · 机位 2')
        position = workspace.current
        self.key('Right', widget=workspace.color_workspace)
        self.assertAlmostEqual(workspace.current, position+1/30)
        self.assertEqual(self.key('c', 'c', widget=workspace.color_workspace.strip), 'break')
        self.assertAlmostEqual(workspace.selected_program_range[0], position+1/30)
        self.assertEqual(workspace.selected_program_range[1], 6.)
        self.assertTrue(workspace._color_mode)
        workspace.show_creative('transition')
        self.root.update()
        self.assertFalse(workspace._color_mode)
        self.assertIn(str(workspace.timeline_section), workspace.workspace_split.panes())
        self.assertTrue(workspace.timeline.winfo_ismapped())
        self.assertFalse(workspace.color_workspace.winfo_ismapped())
        self.assert_clean_callbacks()

    def test_context_menu_adds_transition_at_manual_same_camera_end(self):
        workspace = self.workspace
        workspace.seek(6.)
        self.key('c', 'c')
        self.assertEqual(workspace.project.cuts, [Cut(0., 1)])
        menu = self.context((0., 6.))
        transition_index = next(i for i in range(menu.index('end')+1)
                                if menu.entrycget(i, 'label') == '在片段末尾添加转场')
        self.assertEqual(menu.entrycget(transition_index, 'state'), 'normal')
        transitions = self.cascade(menu, transition_index)
        self.assertEqual(transitions.index('end'), 8)
        transitions.invoke(0)
        self.assertEqual(len(workspace.project.transitions), 1)
        self.assertEqual((workspace.project.transitions[0].time, workspace.project.transitions[0].kind),
                         (6., 'fade'))
        validate_transitions(workspace.project.transitions, workspace.project)
        workspace._refresh()
        self.assertEqual(len(workspace.project.transitions), 1, 'Refresh must retain manual-cut transitions')
        self.assertIn(6., workspace.creative_panel._cut_choices.values())
        marker = next(item for item, time in workspace.timeline_transition_items.items()
                      if time == 6. and workspace.timeline.type(item) == 'rectangle')
        left, top, right, bottom = workspace.timeline.coords(marker)
        workspace.timeline_press(SimpleNamespace(
            x=(left+right)/2-workspace.timeline.canvasx(0),
            y=(top+bottom)/2-workspace.timeline.canvasy(0)))
        self.assertEqual(workspace.creative_panel._selected_cut_time(), 6.)
        self.assertEqual(workspace.creative_panel.transition_kind_var.get(), '叠化')
        menu = self.context((6., 12.))
        transition_index = next(i for i in range(menu.index('end')+1)
                                if menu.entrycget(i, 'label') == '在片段末尾添加转场')
        self.assertEqual(menu.entrycget(transition_index, 'state'), 'disabled')
        self.assert_clean_callbacks()

    def test_range_grade_is_local_undoable_and_highlights_selected_rectangles(self):
        workspace = self.workspace
        before = copy.deepcopy(workspace.project)
        workspace.select_program_range(2., 4.)
        workspace.show_creative('color')
        grade = ColorGrade(lift_r=.12, gamma_y=.1, gain_b=-.03, offset_y=.04)
        self.assertTrue(workspace.apply_selected_grade(grade))
        self.assertEqual(grade_at(workspace.project, 3.), grade)
        self.assertIsNone(grade_at(workspace.project, 1.))
        self.assertIsNone(grade_at(workspace.project, 4.))
        self.assertEqual(workspace.project.global_grade, ColorGrade())
        copied = workspace.get_selected_grade()
        copied.lift_r = .4
        self.assertEqual(grade_at(workspace.project, 3.).lift_r, .12)
        self.assertEqual(len(workspace.undo_stack), 1)
        panel = workspace.creative_panel
        panel.grade_vars['exposure'].set('.5')
        panel.apply_grade()
        self.assertEqual(grade_at(workspace.project, 3.).exposure, .5)
        self.assertEqual(grade_at(workspace.project, 3.).lift_r, .12)
        self.assertEqual(len(workspace.undo_stack), 2, 'one edit should add exactly one undo')
        workspace.exit_color_workspace()
        self.root.update()
        workspace.draw_timeline()
        for item, selected in workspace.timeline_program_items.items():
            if workspace.timeline.type(item) != 'rectangle': continue
            if selected == (2., 4.):
                self.assertEqual(workspace.timeline.itemcget(item, 'outline'), ACCENT)
            else:
                self.assertNotEqual(workspace.timeline.itemcget(item, 'outline'), ACCENT)
        self.assertTrue(workspace.timeline.find_withtag('selection'))
        workspace.undo()
        self.assertEqual(grade_at(workspace.project, 3.), grade)
        workspace.undo()
        self.assertEqual(workspace.project, before)
        self.assert_clean_callbacks()


if __name__ == '__main__':
    unittest.main()
