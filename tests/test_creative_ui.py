"""Integrated native workspace tests for creative tracks and cached previews.

Each test constructs a transparent, off-screen Tk window. FFmpeg and player
launches are replaced; no existing app, user media, mouse or keyboard is used.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creative_core import ColorGrade, Transition
from exporter_core import Capabilities
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_ui import MulticamWorkspace
from subtitle_core import SubtitleCue, SubtitleStyle, SubtitleTrack


class CreativeWorkspaceTests(unittest.TestCase):
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
        self.temp = tempfile.TemporaryDirectory(prefix='creative_workspace_')
        self.folder = Path(self.temp.name)
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
        for name in ('camera-a.mp4', 'camera-b.mp4'):
            (self.folder / name).write_bytes(b'fixture path only')
        self.workspace.project = self._project()
        self.workspace._refresh()
        self.root.deiconify()
        self.root.update()
        self.root.update_idletasks()
        self.assertEqual(self.callback_errors, [])

    def _project(self):
        return MulticamProject(
            clips=[CameraClip('a', 1, str(self.folder / 'camera-a.mp4'), 0, 10),
                   CameraClip('b', 2, str(self.folder / 'camera-b.mp4'), 0, 10)],
            cuts=[Cut(0, 1), Cut(3, 2), Cut(7, 1)], fps=30,
            global_grade=ColorGrade(exposure=0.5), camera_grades={2: ColorGrade(temperature=0.2)},
            transitions=[Transition(3, 'fade', 0.6), Transition(7, 'wipeleft', 0.8)],
            subtitles=SubtitleTrack(cues=[SubtitleCue('cue-a', 1, 2.25, '第一条字幕\n第二行'),
                                          SubtitleCue('cue-b', 5, 6, '第二条字幕')],
                                   style=SubtitleStyle(font_size=56, bold=True), offset=0.2,
                                   burn_in=False))

    def tearDown(self):
        if hasattr(self, 'workspace'):
            worker = self.workspace._creative_preview_worker
            if worker is not None:
                worker.join(timeout=5)
            self.workspace.close()
        if hasattr(self, 'root'):
            self._cancel_afters()
            self.root.destroy()
        for item in getattr(self, 'patches', []):
            item.stop()
        if hasattr(self, 'temp'):
            self.temp.cleanup()

    def _cancel_afters(self):
        for pending in self.root.tk.call('after', 'info'):
            self.root.tk.call('after', 'cancel', pending)

    def _item_event(self, mapping, key):
        canvas = self.workspace.timeline
        item = next(item for item, value in mapping.items()
                    if value == key and canvas.type(item) == 'rectangle')
        left, top, right, bottom = canvas.coords(item)
        return SimpleNamespace(x=(left + right) / 2 - canvas.canvasx(0),
                               y=(top + bottom) / 2 - canvas.canvasy(0))

    def test_s1_track_cues_select_and_drag_on_timeline_with_one_undo(self):
        workspace = self.workspace
        labels = [workspace.timeline.itemcget(item, 'text') for item in workspace.timeline.find_all()
                  if workspace.timeline.type(item) == 'text']
        self.assertIn('S1 字幕', labels)
        self.assertEqual(set(workspace.timeline_subtitle_items.values()), {'cue-a', 'cue-b'})
        event = self._item_event(workspace.timeline_subtitle_items, 'cue-a')
        workspace.timeline_press(event)
        self.root.update_idletasks()
        self.assertEqual(workspace.creative_panel.subtitle_tree.selection(), ('cue-a',))
        self.assertEqual(workspace.creative_panel.subtitle_text.get('1.0', 'end-1c'), '第一条字幕\n第二行')
        self.assertEqual(workspace.inspector_tabs.select(), str(workspace.creative_tab))
        self.assertEqual(workspace.creative_panel.tabs.select(), str(workspace.creative_panel.pages['subtitle']))
        self.assertAlmostEqual(workspace.current, 1.2)
        original = copy.deepcopy(workspace.project)
        scale = workspace._timeline_scale()
        workspace.timeline_motion(SimpleNamespace(x=event.x + scale, y=event.y))
        workspace.timeline_motion(SimpleNamespace(x=event.x + 2 * scale, y=event.y))
        workspace.timeline_release(event)
        moved = next(cue for cue in workspace.project.subtitles.cues if cue.id == 'cue-a')
        self.assertEqual((moved.start, moved.end), (3, 4.25))
        self.assertEqual(workspace.project.subtitles.offset, 0.2)
        self.assertEqual(len(workspace.undo_stack), 1)
        self.assertIsNone(workspace.subtitle_drag)
        self.assertEqual(workspace.creative_panel.subtitle_start_var.get(), '00:00:03.000')
        workspace.undo()
        self.assertEqual(workspace.project, original)
        workspace.redo()
        self.assertEqual(workspace.project.subtitles.cues[0].start, 3)
        self.assertEqual(self.callback_errors, [])

    def test_timeline_subtitle_drag_clamps_raw_cue_start_preserving_duration(self):
        workspace = self.workspace
        event = self._item_event(workspace.timeline_subtitle_items, 'cue-a')
        workspace.timeline_press(event)
        workspace.timeline_motion(SimpleNamespace(x=event.x - workspace._timeline_scale() * 5, y=event.y))
        workspace.timeline_release(event)
        cue = workspace.project.subtitles.cues[0]
        self.assertEqual((cue.start, cue.end), (0, 1.25))
        self.assertEqual(len(workspace.undo_stack), 1)
        self.assertEqual(self.callback_errors, [])

    def test_click_second_transition_marker_selects_matching_inspector_cut(self):
        workspace = self.workspace
        event = self._item_event(workspace.timeline_transition_items, 7)
        workspace.timeline_press(event)
        self.assertEqual(workspace.creative_panel.tabs.select(), str(workspace.creative_panel.pages['transition']))
        self.assertAlmostEqual(workspace.current, 7)
        self.assertEqual(workspace.creative_panel._selected_cut_time(), 7)
        self.assertEqual(workspace.creative_panel.transition_kind_var.get(), '向左擦除')
        self.assertEqual(self.callback_errors, [])

    def test_save_new_and_load_refresh_all_creative_controls_and_version_one_defaults(self):
        workspace = self.workspace
        path = self.folder / 'creative.multicam.json'
        workspace.project_path = str(path)
        self.assertTrue(workspace.save())
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['version'], 8)
        self.assertIn('subtitles', saved)
        self.assertEqual(saved['global_grade']['exposure'], 0.5)
        workspace.new()
        self.assertEqual(workspace.creative_panel.grade_vars['exposure'].get(), '0')
        self.assertEqual(workspace.creative_panel.subtitle_tree.get_children(), ())
        workspace.load_project(str(path))
        self.assertEqual(workspace.creative_panel.grade_vars['exposure'].get(), '0.5')
        self.assertEqual(len(workspace.creative_panel.subtitle_tree.get_children()), 2)
        self.assertTrue(workspace.creative_panel.style_vars['bold'].get())
        self.assertEqual(workspace.creative_panel.style_vars['font_size'].get(), '56')
        self.assertFalse(workspace.creative_panel.subtitle_burn_var.get())
        for key in ('global_grade', 'camera_grades', 'transitions', 'subtitles'):
            saved.pop(key)
        saved['version'] = 1
        old_path = self.folder / 'old.multicam.json'
        old_path.write_text(json.dumps(saved), encoding='utf-8')
        workspace.load_project(str(old_path))
        self.assertEqual(workspace.project.global_grade, ColorGrade())
        self.assertEqual(workspace.project.transitions, [])
        self.assertEqual(workspace.project.subtitles.cues, [])
        self.assertEqual(workspace.creative_panel.grade_vars['exposure'].get(), '0')
        self.assertEqual(workspace.creative_panel.subtitle_tree.get_children(), ())
        self.assertEqual(self.callback_errors, [])
        self.dialogs[0].assert_not_called()
        self.dialogs[1].assert_not_called()

    def test_preview_dispatch_uses_snapshot_and_ready_opens_render_preview(self):
        workspace = self.workspace
        workspace.current = 4
        workspace.fps_var.set('25')
        self.root.mpv_path = 'test-mpv.exe'
        original = copy.deepcopy(workspace.project)
        calls = []

        def render(project, output, ffmpeg, ffprobe, settings, caps, run, **kwargs):
            calls.append((project, output, ffmpeg, ffprobe, settings, caps, run, kwargs))
            kwargs['progress'](2.5, 5, '调色与转场')
            Path(output).write_bytes(b'cached fixture')

        with patch('multicam_ui.render_multicam', side_effect=render):
            workspace.preview_effects()
            self.assertTrue(workspace.busy)
            self.assertTrue(self.root.working)
            workspace._creative_preview_worker.join(timeout=5)
        self.assertFalse(workspace._creative_preview_worker.is_alive())
        self.assertEqual(len(calls), 1)
        project, output, ffmpeg, ffprobe, settings, caps, run, kwargs = calls[0]
        self.assertIsNot(project, workspace.project)
        self.assertEqual(workspace.project, original)
        self.assertEqual((project.range_start, project.range_end, project.fps), (2, 7, 25))
        self.assertTrue(project.subtitles.burn_in)
        self.assertFalse(workspace.project.subtitles.burn_in)
        self.assertEqual(project.global_grade, original.global_grade)
        self.assertEqual(project.transitions, original.transitions)
        self.assertEqual((settings.codec, settings.preset, settings.resolution, settings.audio,
                          settings.parallel_workers), ('libx264', 'veryfast', '1080p', 'none', 1))
        self.assertEqual((ffmpeg, ffprobe), ('test-ffmpeg.exe', 'test-ffprobe.exe'))
        self.assertIs(kwargs['cancel_event'], self.root.cancel_event)
        self.assertIs(run, self.root._run_editor_process)
        player = Mock()
        with patch.object(workspace, 'start_monitors', side_effect=lambda: setattr(workspace, 'player', player)) as start:
            workspace._poll()
        start.assert_called_once()
        player.submit.assert_called_once_with('render_preview', path=str(output), start=2, end=7, play=True)
        self.assertFalse(workspace.busy)
        self.assertFalse(self.root.working)
        self.assertEqual(self.root.progress_var.get(), 100)
        self.assertEqual(self.callback_errors, [])
        self.dialogs[0].assert_not_called()

    def test_preview_preconditions_report_and_do_not_start_job(self):
        workspace = self.workspace
        workspace.preview_effects()
        self.dialogs[0].assert_called_once()
        self.assertFalse(workspace.busy)
        self.assertIsNone(workspace._creative_preview_worker)
        self.root.mpv_path = 'test-mpv.exe'
        workspace.project = MulticamProject()
        workspace._refresh()
        workspace.preview_effects()
        self.dialogs[2].assert_called_once()
        self.assertFalse(workspace.busy)
        self.assertEqual(self.callback_errors, [])

    @staticmethod
    def _buttons(widget):
        found = [widget] if isinstance(widget, ttk.Button) else []
        for child in widget.winfo_children():
            found.extend(CreativeWorkspaceTests._buttons(child))
        return found

    def _assert_layout(self):
        workspace = self.workspace
        for section in ('color', 'transition', 'subtitle'):
            workspace.show_creative(section)
            self.root.update()
            self.assertTrue(workspace.program_surface.winfo_ismapped())
            if workspace._color_mode:
                self.assertFalse(workspace.timeline_section.winfo_ismapped())
                self.assertTrue(workspace.color_workspace.winfo_ismapped())
                self.assertTrue(workspace.color_workspace.strip.winfo_ismapped())
            else:
                self.assertTrue(workspace.timeline.winfo_ismapped())
                self.assertGreater(workspace.timeline.winfo_height(), workspace._px(70))
            self.assertGreater(workspace.creative_panel.winfo_width(), workspace._px(160))
            preview = next(button for button in self._buttons(workspace.creative_panel)
                           if button.cget('text') == '预览当前效果片段')
            self.assertTrue(preview.winfo_ismapped())
            self.assertGreater(preview.winfo_width(), workspace._px(100))
        for label in ('字幕', '转场', '调色'):
            button = next(button for button in self._buttons(workspace) if button.cget('text') == label)
            self.assertTrue(button.winfo_ismapped(), label)
            self.assertGreater(button.winfo_width(), workspace._px(30), label)
            self.assertLessEqual(button.winfo_x() + button.winfo_width(), button.master.winfo_width(), label)
        self.assertEqual(self.callback_errors, [])

    def test_1280_layout_keeps_creative_buttons_preview_and_s1_timeline_available(self):
        self._assert_layout()

    def test_240ppi_layout_keeps_creative_buttons_preview_and_s1_timeline_available(self):
        self.workspace.close()
        self._cancel_afters()
        self.workspace.destroy()
        self.root.tk.call('tk', 'scaling', 240 / 72)
        self.root.geometry('3840x2400+10000+10000')
        self.workspace = MulticamWorkspace(self.root, self.root)
        self.workspace.pack(fill='both', expand=True)
        self.workspace.project = self._project()
        self.workspace._refresh()
        self.root.update()
        self._assert_layout()


if __name__ == '__main__':
    unittest.main()
