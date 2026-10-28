"""Real Tk integration for asynchronous, selected-cut beginner grading."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock,patch

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))

from auto_color import analyze_rgb_samples,build_auto_grade,PRESET_LABELS
from creative_core import ColorGrade
from exporter_core import Capabilities
from multicam_core import CameraClip,Cut,MulticamProject
from multicam_ui import MulticamWorkspace
from program_edits import grade_at


class AutoColorWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw()
        self.root.tk.call('tk','scaling',96/72)
        self.root.attributes('-alpha',0);self.root.overrideredirect(True)
        self.root.geometry('1280x820+10000+10000')
        self.temp=tempfile.TemporaryDirectory(prefix='auto_colour_ui_');self.folder=Path(self.temp.name)
        for name in ('a.mp4','b.mp4','ffmpeg.exe','ffprobe.exe'):(self.folder/name).write_bytes(b'fixture only')
        self.root.working=False;self.root.mpv_path='';self.root.mpv=None
        self.root.cancel_event=threading.Event()
        self.root.caps=Capabilities(ffmpeg_path=str(self.folder/'ffmpeg.exe'),ffprobe_path=str(self.folder/'ffprobe.exe'))
        self.root.progress_var=tk.DoubleVar(self.root);self.root.progress_text_var=tk.StringVar(self.root)
        self.root._log=Mock();self.root._run_editor_process=Mock();self.root._begin_shared_progress=Mock()
        self.root._set_working=lambda value:setattr(self.root,'working',bool(value))
        self.errors=[];self.root.report_callback_exception=lambda *error:self.errors.append(error)
        self.patches=[patch('multicam_ui.messagebox.showerror'),patch('auto_color_workspace.messagebox.showerror')]
        self.dialogs=[item.start() for item in self.patches]
        self.workspace=MulticamWorkspace(self.root,self.root);self.workspace.pack(fill='both',expand=True)
        self.workspace.project=MulticamProject(clips=[
            CameraClip('a',1,str(self.folder/'a.mp4'),0,12,camera_make='Sony',camera_model='ILCE-7M4'),
            CameraClip('b',2,str(self.folder/'b.mp4'),0,12,camera_make='Nikon',camera_model='NIKON Z 9')],
            cuts=[Cut(0,1),Cut(4,2),Cut(8,1)],fps=30)
        self.workspace._refresh()
        self.root.deiconify();self.root.update()
        self.workspace.select_program_range(4,8)
        self.workspace.enter_color_workspace()
        self.workspace.player=Mock()
        self.workspace.undo_stack.clear();self.workspace.redo_stack.clear();self.workspace.dirty=False
        self.analysis=analyze_rgb_samples([bytes((115,115,115))*1024 for _ in range(3)],
            sample_times=(4.5,6.,7.5))
        self.assertEqual(self.errors,[])

    def tearDown(self):
        self.workspace.close()
        if self.workspace._auto_color_worker:self.workspace._auto_color_worker.join(timeout=3)
        for token in self.root.tk.call('after','info'):self.root.tk.call('after','cancel',token)
        self.root.destroy()
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()

    def wait(self,predicate,timeout=3):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.root.update()
            if predicate():return
            time.sleep(.01)
        self.fail('asynchronous auto-colour task did not complete')

    def apply(self,preset='natural',**kwargs):
        with patch('auto_color_workspace.sample_analyze',return_value=self.analysis) as analyze:
            self.assertTrue(self.workspace.start_auto_color(preset=preset,**kwargs))
            self.wait(lambda:not self.workspace._auto_color_running)
        self.assertEqual(self.errors,[])
        self.assertFalse(any(dialog.called for dialog in self.dialogs))
        self.root._run_editor_process.assert_not_called()
        return analyze

    def test_auto_portrait_changes_only_selected_cut_and_saves_one_undo_step(self):
        original=copy.deepcopy(self.workspace.project)
        analyze=self.apply('portrait_clear')
        self.assertEqual(analyze.call_args.args[1:3],(4.,8.))
        grade=grade_at(self.workspace.project,5)
        self.assertEqual((grade.skin_lighten,grade.skin_saturation,grade.skin_deyellow),(.55,.24,.75))
        self.assertIsNone(grade_at(self.workspace.project,2));self.assertIsNone(grade_at(self.workspace.project,10))
        self.assertEqual(self.workspace.project.cuts,original.cuts)
        self.assertEqual(self.workspace.project.camera_grades,{})
        self.assertEqual(self.workspace.project.global_grade,ColorGrade())
        self.assertEqual(len(self.workspace.undo_stack),1)
        path=self.folder/'preset.multicam.json';self.workspace.project.save(path)
        self.assertEqual(grade_at(MulticamProject.load(path),5),grade)
        self.workspace.undo();self.assertEqual(self.workspace.project,original)

    def test_repeat_analysis_does_not_add_another_quarter_stop(self):
        self.apply();first=copy.deepcopy(grade_at(self.workspace.project,5))
        self.apply();second=grade_at(self.workspace.project,5)
        self.assertEqual(second,first)
        self.assertAlmostEqual(second.exposure,build_auto_grade(self.analysis,bias_ev=0).exposure+.25)
        self.assertEqual(len(self.workspace.project.clip_grades),1)

    def test_all_three_ui_presets_use_default_quarter_ev_bias(self):
        panel=self.workspace.color_workspace.auto_panel
        self.assertIn('+0.25',panel.bias_var.get())
        for preset,skin in (('natural',0),('portrait_natural',.35),('portrait_clear',.55)):
            panel.preset_var.set(PRESET_LABELS[preset])
            with patch('auto_color_workspace.sample_analyze',return_value=self.analysis):
                panel.apply();self.wait(lambda:not self.workspace._auto_color_running)
            self.assertEqual(grade_at(self.workspace.project,5).skin_lighten,skin)
            self.assertIn(PRESET_LABELS[preset],self.workspace.auto_color_status.get())
            self.assertIn('+0.25',self.workspace.auto_color_status.get())
            self.assertEqual(str(panel.cancel_button.cget('state')),'disabled')

    def test_selecting_another_cut_discards_finished_result(self):
        entered,release=threading.Event(),threading.Event()
        original=copy.deepcopy(self.workspace.project)
        def analyze(*args,**kwargs):entered.set();release.wait(timeout=2);return self.analysis
        with patch('auto_color_workspace.sample_analyze',side_effect=analyze):
            self.assertTrue(self.workspace.start_auto_color())
            self.wait(entered.is_set)
            self.workspace.select_program_range(8,12)
            release.set();self.wait(lambda:not self.workspace._auto_color_running)
        self.assertEqual(self.workspace.project,original)
        self.assertEqual(self.workspace.undo_stack,[])
        self.assertIn('未应用',self.workspace.auto_color_status.get())

    def test_manual_grade_change_discards_stale_analysis(self):
        entered,release=threading.Event(),threading.Event()
        def analyze(*args,**kwargs):entered.set();release.wait(timeout=2);return self.analysis
        with patch('auto_color_workspace.sample_analyze',side_effect=analyze):
            self.workspace.start_auto_color();self.wait(entered.is_set)
            self.workspace.apply_active_grade(ColorGrade(exposure=-.2))
            release.set();self.wait(lambda:not self.workspace._auto_color_running)
        self.assertEqual(grade_at(self.workspace.project,5).exposure,-.2)
        self.assertEqual(len(self.workspace.undo_stack),1)
        self.assertIn('未应用',self.workspace.auto_color_status.get())

    def test_cancel_discards_even_a_successful_late_worker_result(self):
        entered,release=threading.Event(),threading.Event()
        original=copy.deepcopy(self.workspace.project)
        def analyze(*args,**kwargs):entered.set();release.wait(timeout=2);return self.analysis
        with patch('auto_color_workspace.sample_analyze',side_effect=analyze):
            self.workspace.start_auto_color();self.wait(entered.is_set)
            self.workspace.cancel_auto_color();release.set()
            self.wait(lambda:not self.workspace._auto_color_running)
        self.assertEqual(self.workspace.project,original)
        self.assertEqual(self.workspace.undo_stack,[])
        self.assertIn('已取消',self.workspace.auto_color_status.get())

    def test_unusable_frame_analysis_leaves_the_saved_grade_unchanged(self):
        self.analysis=analyze_rgb_samples([bytes((0,0,0))*1024])
        original=copy.deepcopy(self.workspace.project)
        self.apply('portrait_clear')
        self.assertEqual(self.workspace.project,original)
        self.assertEqual(self.workspace.undo_stack,[])
        self.assertIn('未找到可靠',self.workspace.auto_color_status.get())

    def test_global_or_camera_range_changes_discard_old_result_and_unlock_options(self):
        workspace=self.workspace;panel=workspace.color_workspace.auto_panel
        for target,change in ((None,'project_range'),(2,'range_controls'),(None,'fps_control')):
            with self.subTest(target=target,change=change):
                workspace.project.range_start=0.;workspace.project.range_end=None
                workspace.start_var.set('00:00:00.000');workspace.end_var.set('');workspace.fps_var.set('30')
                workspace.set_grade_target(target)
                entered,release=threading.Event(),threading.Event()
                def analyze(*args,**kwargs):
                    entered.set();release.wait(timeout=2);return self.analysis
                with patch('auto_color_workspace.sample_analyze',side_effect=analyze):
                    self.assertTrue(workspace.start_auto_color())
                    self.wait(entered.is_set)
                    self.assertTrue(all(widget.instate(['disabled']) for widget in panel.option_widgets))
                    self.assertTrue(panel.apply_button.instate(['disabled']))
                    self.assertFalse(panel.cancel_button.instate(['disabled']))
                    if change=='project_range':
                        workspace.project.range_start=1.;workspace.project.range_end=11.
                    elif change=='range_controls':
                        workspace.start_var.set('00:00:01.000');workspace.end_var.set('00:00:11.000')
                    else:workspace.fps_var.set('29.97')
                    release.set();self.wait(lambda:not workspace._auto_color_running)
                self.assertEqual(workspace.project.global_grade,ColorGrade())
                self.assertEqual(workspace.project.camera_grades,{})
                self.assertEqual(workspace.project.clip_grades,[])
                self.assertEqual(workspace.undo_stack,[])
                self.assertIn('未应用',workspace.auto_color_status.get())
                self.assertTrue(all(not widget.instate(['disabled']) for widget in panel.option_widgets
                                    if widget is not panel.reference_combo))
                # Global grading has no single camera to match; the new
                # reference control intentionally stays disabled there.
                self.assertEqual(panel.reference_combo.instate(['disabled']),target is None)
                self.assertFalse(panel.apply_button.instate(['disabled']))
                self.assertTrue(panel.cancel_button.instate(['disabled']))

    def test_nondefault_half_ev_bias_reports_new_highlight_protection(self):
        # +.25 fits under the highlights, whereas +.50 requires protection.
        # Thus analysis.warnings has no pre-existing highlight warning.
        sample=bytes((115,115,115))*900+bytes((191,191,191))*100
        self.analysis=analyze_rgb_samples([sample]*3,sample_times=(4.5,6.,7.5))
        self.assertFalse(any('高光' in text for text in self.analysis.warnings))
        self.apply(bias_ev=.5)
        grade=grade_at(self.workspace.project,5)
        self.assertGreater(grade.gamma,1.001)
        self.assertIn('+0.50',self.workspace.auto_color_status.get())
        self.assertIn('高光',self.workspace.auto_color_status.get())


if __name__=='__main__':unittest.main()
