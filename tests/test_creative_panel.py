"""Real Tk checks for creative controls without opening a visible window."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creative_core import ColorGrade, Transition
from creative_panel import ALIGNMENTS, TRANSITIONS, build_creative_panel
from multicam_core import CameraClip, Cut, MulticamProject
from subtitle_core import SubtitleCue


class CreativePanelTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk unavailable: {error}')
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.temp = tempfile.TemporaryDirectory(prefix='creative_panel_')
        self.addCleanup(self.temp.cleanup)
        self.rootpath = Path(self.temp.name)
        self.history = []
        self.refreshes = 0
        self.seeks = []
        self.preview_calls = []
        self.ws = SimpleNamespace(
            project=MulticamProject(
                clips=[CameraClip('a', 1, 'a.mp4', 0, 6), CameraClip('b', 2, 'b.mp4', 0, 6)],
                cuts=[Cut(0, 1), Cut(2, 2), Cut(4, 1)], fps=30),
            current=1.25, selected_cut=None, status=tk.StringVar(self.root),
            _available=lambda: True, _px=lambda value: round(value),
            preview_effects=lambda: self.preview_calls.append(True), seek=self.seeks.append)
        self.ws._remember = lambda: self.history.append(copy.deepcopy(self.ws.project))
        def refresh():
            self.refreshes += 1
            self.ws.creative_panel_refresh()
        self.ws._refresh = refresh
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill='both', expand=True)
        self.outer = build_creative_panel(self.ws, self.notebook)
        self.panel = self.ws.creative_panel
        self.error_patch = patch('creative_panel.messagebox.showerror')
        self.errors = self.error_patch.start()
        self.addCleanup(self.error_patch.stop)

    def test_build_select_and_refresh_never_mutate_project(self):
        original = copy.deepcopy(self.ws.project)
        self.assertEqual(self.ws.creative_tab, self.outer)
        for tab in ('color', 'transition', 'subtitle'):
            self.panel.select(tab)
            self.assertEqual(self.panel.tabs.select(), str(self.panel.pages[tab]))
            self.panel.refresh()
        self.assertEqual(self.ws.project, original)
        self.assertEqual(self.history, [])
        self.assertEqual(self.refreshes, 0)
        buttons = self._descendants(self.panel, ttk.Button)
        preview = next(button for button in buttons if button.cget('text') == '预览当前效果片段')
        preview.invoke()
        self.assertEqual(self.preview_calls, [True])

    def test_apply_grade_scopes_identity_and_invalid_input_do_not_pollute_undo(self):
        self.panel.grade_vars['exposure'].set('1.25')
        self.panel.apply_grade()
        self.assertEqual(self.ws.project.global_grade.exposure, 1.25)
        self.assertEqual(len(self.history), 1)
        self.assertEqual(self.history[0].global_grade, ColorGrade())
        self.panel.grade_scope_var.set('机位 2')
        self.panel._load_grade()
        self.panel.grade_vars['saturation'].set('0.25')
        self.panel.apply_grade()
        self.assertEqual(self.ws.project.camera_grades[2].saturation, 0.25)
        original = copy.deepcopy(self.ws.project)
        before = len(self.history)
        for value in ('nope', 'NaN', '999'):
            self.panel.grade_vars['gamma'].set(value)
            self.panel.apply_grade()
            self.assertEqual(self.ws.project, original)
            self.assertEqual(len(self.history), before)
        self.assertEqual(self.errors.call_count, 3)
        self.panel.reset_grade()
        self.assertNotIn(2, self.ws.project.camera_grades)
        self.assertEqual(self.ws.project.global_grade.exposure, 1.25)

    def test_missing_lut_cannot_be_applied_and_disabled_target_bypasses_file(self):
        self.panel.lut_var.set(str(self.rootpath / 'missing.cube'))
        self.panel.apply_grade()
        self.assertEqual(self.history, [])
        self.errors.assert_called_once()
        self.panel.grade_enabled_var.set(False)
        self.panel.apply_grade()
        self.assertFalse(self.ws.project.global_grade.enabled)
        self.assertEqual(len(self.history), 1)

    def test_transition_create_replace_remove_and_overlap_validation(self):
        choices = list(self.panel._cut_choices)
        self.panel.transition_cut_var.set(choices[0])
        self.panel.transition_kind_var.set('向左擦除')
        self.panel.transition_duration_var.set('1')
        self.panel.apply_transition()
        self.assertEqual(self.ws.project.transitions, [Transition(2, 'wipeleft', 1)])
        self.panel.transition_kind_var.set('圆形展开')
        self.panel.apply_transition()
        self.assertEqual(self.ws.project.transitions, [Transition(2, 'circleopen', 1)])
        self.panel.transition_cut_var.set(choices[1])
        self.panel.transition_duration_var.set('4')
        self.panel.apply_transition()
        self.assertEqual(len(self.history), 2)
        self.assertEqual(self.ws.project.transitions, [Transition(2, 'circleopen', 1)])
        self.errors.assert_called_once()
        self.panel.transition_cut_var.set(choices[0])
        self.panel.remove_transition()
        self.assertEqual(self.ws.project.transitions, [])
        self.assertEqual(len(TRANSITIONS), 9)

    def test_root_cut_selection_refreshes_transition_target_without_losing_local_choice(self):
        choices = list(self.panel._cut_choices)
        self.ws.selected_cut = 2
        self.panel.refresh()
        self.assertEqual(self.panel.transition_cut_var.get(), choices[1])
        self.panel.transition_cut_var.set(choices[0])
        self.panel.refresh()
        self.assertEqual(self.panel.transition_cut_var.get(), choices[0])
        self.assertEqual(self.history, [])

    def test_import_srt_without_video_can_be_undone_and_invalid_replacement_is_atomic(self):
        self.ws.project = MulticamProject()
        self.panel.refresh()
        path = self.rootpath / '字幕.srt'
        path.write_text('1\n00:00:01,000 --> 00:00:02,500\n第一行\n第二行\n', encoding='utf-8')
        with patch('creative_panel.filedialog.askopenfilename', return_value=str(path)):
            self.panel.import_srt()
        self.assertEqual(len(self.ws.project.subtitles.cues), 1)
        self.assertEqual(self.ws.project.subtitles.cues[0].text, '第一行\n第二行')
        self.assertEqual(len(self.history), 1)
        self.assertEqual(self.history[0].subtitles.cues, [])
        original = copy.deepcopy(self.ws.project)
        path.write_text('broken SRT', encoding='utf-8')
        with patch('creative_panel.filedialog.askopenfilename', return_value=str(path)):
            self.panel.import_srt()
        self.assertEqual(self.ws.project, original)
        self.assertEqual(len(self.history), 1)
        self.errors.assert_called_once()

    def test_subtitle_add_multiline_edit_seek_delete_and_validation(self):
        self.panel.add_cue()
        cue = self.ws.project.subtitles.cues[0]
        self.assertEqual((cue.start, cue.end), (1.25, 3.25))
        self.panel.subtitle_start_var.set('00:00:00.500')
        self.panel.subtitle_end_var.set('00:00:02.000')
        self.panel.subtitle_text.delete('1.0', 'end')
        self.panel.subtitle_text.insert('1.0', '测试\n多行字幕')
        self.panel.save_cue()
        cue = self.ws.project.subtitles.cues[0]
        self.assertEqual((cue.start, cue.end, cue.text), (0.5, 2.0, '测试\n多行字幕'))
        before = len(self.history)
        self.panel.subtitle_end_var.set('00:00:00.100')
        self.panel.save_cue()
        self.assertEqual(len(self.history), before)
        self.assertEqual(self.ws.project.subtitles.cues[0], cue)
        self.ws.project.subtitles.offset = 0.25
        self.panel._seek_cue()
        self.assertEqual(self.seeks, [0.75])
        self.panel.delete_cue()
        self.assertEqual(self.ws.project.subtitles.cues, [])

    def test_style_controls_cover_every_field_and_offset_burn_mode(self):
        values = {'font_family': 'Microsoft YaHei', 'font_size': '64', 'color': '#AABBCC',
                  'outline_color': '#112233', 'outline_width': '3.5', 'shadow': '2.5',
                  'bold': True, 'italic': True, 'alignment': '上方居右', 'margin_x': '72',
                  'margin_y': '44', 'box': True, 'background_color': '#224466',
                  'background_opacity': '35'}
        for name, value in values.items():
            self.panel.style_vars[name].set(value)
        self.panel.subtitle_offset_var.set('-00:00:00.250')
        self.panel.subtitle_enabled_var.set(False)
        self.panel.subtitle_burn_var.set(False)
        self.panel.apply_subtitle_style()
        track = self.ws.project.subtitles
        self.assertFalse(track.enabled)
        self.assertFalse(track.burn_in)
        self.assertEqual(track.offset, -0.25)
        for name, value in values.items():
            expected = ALIGNMENTS[value] if name == 'alignment' else (
                0.35 if name == 'background_opacity' else float(value) if name in
                {'font_size', 'outline_width', 'shadow', 'margin_x', 'margin_y'} else value)
            self.assertEqual(getattr(track.style, name), expected, name)
        self.assertEqual(len(self.history), 1)
        self.panel.style_vars['background_opacity'].set('101')
        self.panel.apply_subtitle_style()
        self.assertEqual(len(self.history), 1)
        self.assertEqual(self.ws.project.subtitles.style.background_opacity, 0.35)
        self.errors.assert_called_once()

    def test_explicit_export_uses_user_path_offset_and_does_not_modify_project(self):
        self.ws.project.subtitles.cues = [SubtitleCue('cue1', 1, 2, '字幕')]
        self.ws.project.subtitles.offset = 0.5
        self.ws.project.subtitles.enabled = False
        self.panel.refresh()
        path = self.rootpath / '另存字幕.srt'
        with patch('creative_panel.filedialog.asksaveasfilename', return_value=str(path)):
            self.panel.export_srt()
        self.assertIn('00:00:01,500 --> 00:00:02,500', path.read_text(encoding='utf-8-sig'))
        self.assertIn('字幕', path.read_text(encoding='utf-8-sig'))
        self.assertFalse(self.ws.project.subtitles.enabled)
        self.assertEqual(self.history, [])

    def test_busy_guard_prevents_every_project_edit(self):
        original = copy.deepcopy(self.ws.project)
        self.ws._available = lambda: False
        for method in (self.panel.apply_grade, self.panel.reset_grade, self.panel.apply_transition,
                       self.panel.remove_transition, self.panel.import_srt, self.panel.export_srt,
                       self.panel.add_cue, self.panel.save_cue, self.panel.delete_cue,
                       self.panel.apply_subtitle_style):
            method()
        self.assertEqual(self.history, [])
        self.assertEqual(self.ws.project, original)

    @staticmethod
    def _descendants(widget, kind):
        result = [widget] if isinstance(widget, kind) else []
        for child in widget.winfo_children():
            result.extend(CreativePanelTests._descendants(child, kind))
        return result

    def test_narrow_sidebar_keeps_footer_visible_and_scrolls_all_tabs(self):
        # This root is transparent and off screen; no user app is manipulated.
        self.root.attributes('-alpha', 0)
        self.root.overrideredirect(True)
        self.root.tk.call('tk', 'scaling', 96 / 72)
        self.root.geometry('294x620+10000+10000')
        self.root.deiconify()
        self.root.update()
        preview = next(button for button in self._descendants(self.panel, ttk.Button)
                       if button.cget('text') == '预览当前效果片段')
        for name in self.panel.pages:
            self.panel.select(name)
            self.root.update()
            self.assertGreater(preview.winfo_height(), 1)
            self.assertLessEqual(preview.winfo_y() + preview.winfo_height(), preview.master.winfo_height())
        for canvas, body in self.panel._canvases:
            self.assertLessEqual(body.winfo_width(), canvas.winfo_width() + 1)
            canvas.yview_moveto(1)
        self.errors.assert_not_called()

    def test_high_dpi_sidebar_entries_remain_inside_scroll_body(self):
        self.notebook.destroy()
        self.root.tk.call('tk', 'scaling', 240 / 72)
        self.ws._px = lambda value: round(value * 2.5)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill='both', expand=True)
        build_creative_panel(self.ws, self.notebook)
        self.panel = self.ws.creative_panel
        self.root.attributes('-alpha', 0)
        self.root.overrideredirect(True)
        self.root.geometry('735x1550+10000+10000')
        self.root.deiconify()
        for page in self.panel.pages:
            self.panel.select(page)
            self.root.update()
            for field in self._descendants(self.panel.pages[page], (tk.Entry, ttk.Combobox)):
                self.assertGreaterEqual(field.winfo_x(), 0)
                self.assertLessEqual(field.winfo_x() + field.winfo_width(), field.master.winfo_width())
        self.errors.assert_not_called()


if __name__ == '__main__':
    unittest.main()
