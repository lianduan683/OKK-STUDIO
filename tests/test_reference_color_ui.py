"""Real Tk coverage for optional camera-reference matching on a selected Cut."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))

from auto_color import analyze_rgb_samples,build_auto_grade,PRESET_LABELS
from creative_core import ColorGrade
from multicam_core import CameraClip,Cut,MulticamProject
from program_edits import ClipGrade,grade_at

try:
    from . import test_auto_color_workspace as _fixture
except ImportError:
    import test_auto_color_workspace as _fixture


class ReferenceColorUiTests(unittest.TestCase):
    # Reuse only the real Tk fixture, avoiding inheritance of its old test cases.
    wait=_fixture.AutoColorWorkspaceTests.wait
    tearDown=_fixture.AutoColorWorkspaceTests.tearDown

    def setUp(self):
        _fixture.AutoColorWorkspaceTests.setUp(self)
        for name in ('c.mp4','d.mp4'):(self.folder/name).write_bytes(b'fixture only')
        clips=[CameraClip(chr(96+camera),camera,str(self.folder/f'{chr(96+camera)}.mp4'),0,12,
                          camera_make='Sony',camera_model=f'ILCE-7M{camera}')
               for camera in range(1,5)]
        self.reference_grade=ColorGrade(exposure=.1,wb_red=.97,wb_green=.99)
        self.outside_grade=ColorGrade(exposure=-.15,saturation=.92)
        self.workspace.project=MulticamProject(clips=clips,
            cuts=[Cut(0,1),Cut(4,4),Cut(8,3)],fps=30,edit_points=[2.,10.],
            camera_grades={2:copy.deepcopy(self.reference_grade)},
            clip_grades=[ClipGrade(0,2,copy.deepcopy(self.outside_grade))])
        self.workspace._refresh()
        self.workspace.select_program_range(4,8)
        self.workspace.enter_color_workspace()
        self.workspace.undo_stack.clear();self.workspace.redo_stack.clear();self.workspace.dirty=False
        self.panel=self.workspace.color_workspace.auto_panel
        warm=bytes((194,158,110))*800+bytes((245,232,213))*300+bytes((102,46,140))*900
        cool=bytes((196,176,182))*500+bytes((231,235,242))*500+bytes((70,10,110))*1000
        self.analysis=analyze_rgb_samples([warm]*3,sample_times=(4.5,6.,7.5))
        self.reference_analysis=analyze_rgb_samples([cool]*3,sample_times=(4.5,6.,7.5))
        self.root.update()
        self.assertEqual(self.errors,[])

    def choose_reference(self,camera=2):
        self.panel.refresh_reference_choices()
        label=next(label for label,value in self.panel._reference_choices.items() if value==camera)
        self.panel.reference_var.set(label)
        self.panel.reference_combo.event_generate('<<ComboboxSelected>>')
        self.root.update()
        self.assertEqual(self.workspace.project.color_reference_camera,camera)

    def run_reference(self,preset='portrait_clear',reference=None):
        reference=reference or self.reference_analysis
        def analyze(project,*args,**kwargs):
            if project.camera_at(5.)==2:
                if isinstance(reference,Exception):raise reference
                return reference
            return self.analysis
        with patch('auto_color_workspace.sample_analyze',side_effect=analyze) as mock:
            self.assertTrue(self.workspace.start_auto_color(preset=preset,reference_camera=2))
            self.wait(lambda:not self.workspace._auto_color_running)
        self.assertEqual(self.errors,[])
        self.assertFalse(any(dialog.called for dialog in self.dialogs))
        self.root._run_editor_process.assert_not_called()
        return mock

    def test_camera_four_cut_matches_camera_two_without_touching_reference_or_other_cuts(self):
        self.choose_reference(2)
        self.workspace.undo_stack.clear()
        original=copy.deepcopy(self.workspace.project)
        sampled=self.run_reference()
        self.assertEqual(sampled.call_count,2)
        target_call=next(call for call in sampled.call_args_list if call.args[0].camera_at(5.)==4)
        reference_call=next(call for call in sampled.call_args_list if call.args[0].camera_at(5.)==2)
        self.assertEqual(target_call.args[1:3],(4.,8.))
        self.assertEqual(reference_call.args[1:3],(4.,8.))
        self.assertIsNone(target_call.kwargs.get('sample_grade'))
        self.assertEqual(reference_call.kwargs['sample_grade'],self.reference_grade)
        expected=build_auto_grade(self.analysis,preset='portrait_clear',base_grade=ColorGrade(),
                                  bias_ev=.25,strength=1.,reference_analysis=self.reference_analysis)
        self.assertEqual(grade_at(self.workspace.project,5),expected)
        self.assertEqual(grade_at(self.workspace.project,1),self.outside_grade)
        self.assertIsNone(grade_at(self.workspace.project,3))
        self.assertIsNone(grade_at(self.workspace.project,9))
        self.assertEqual(self.workspace.project.cuts,original.cuts)
        self.assertEqual(self.workspace.project.camera_grades,original.camera_grades)
        self.assertEqual(self.workspace.project.global_grade,original.global_grade)
        self.assertEqual(len(self.workspace.undo_stack),1)
        self.assertIn('参考',self.workspace.auto_color_status.get())
        self.workspace.undo()
        self.assertEqual(self.workspace.project,original)

    def test_dropdown_names_real_cameras_and_excludes_selected_cut_camera(self):
        self.panel.refresh_reference_choices()
        values=list(self.panel.reference_combo.cget('values'))
        self.assertEqual(set(self.panel._reference_choices.values()),{None,1,2,3})
        self.assertIn(self.workspace._label_camera(2),values)
        self.assertNotIn(self.workspace._label_camera(4),values)
        self.choose_reference(2)
        self.assertEqual(self.panel.reference_var.get(),self.workspace._label_camera(2))
        self.panel.preset_var.set(PRESET_LABELS['portrait_natural'])
        with patch.object(self.workspace,'start_auto_color',return_value=True) as start:
            self.panel.apply()
        self.assertEqual(start.call_args.kwargs['reference_camera'],2)
        self.assertEqual(start.call_args.kwargs['preset'],'portrait_natural')
        self.assertEqual(start.call_args.kwargs['bias_ev'],.25)

    def test_changing_reference_while_analysis_runs_discards_old_result(self):
        self.choose_reference(2);self.workspace.undo_stack.clear()
        original_grades=copy.deepcopy(self.workspace.project.clip_grades)
        entered,release=threading.Event(),threading.Event()
        def analyze(project,*args,**kwargs):
            entered.set();release.wait(timeout=3)
            return self.reference_analysis if project.camera_at(5.)==2 else self.analysis
        with patch('auto_color_workspace.sample_analyze',side_effect=analyze):
            self.assertTrue(self.workspace.start_auto_color(reference_camera=2))
            self.wait(entered.is_set)
            self.workspace.set_color_reference_camera(3)
            release.set();self.wait(lambda:not self.workspace._auto_color_running)
        self.assertEqual(self.workspace.project.clip_grades,original_grades)
        self.assertEqual(self.workspace.project.color_reference_camera,3)
        self.assertEqual(self.workspace.undo_stack,[])
        self.assertIn('未应用',self.workspace.auto_color_status.get())

    def test_reference_grade_edit_during_analysis_discards_old_result(self):
        self.choose_reference(2);self.workspace.undo_stack.clear()
        original_grades=copy.deepcopy(self.workspace.project.clip_grades)
        entered,release=threading.Event(),threading.Event()
        def analyze(project,*args,**kwargs):
            entered.set();release.wait(timeout=3)
            return self.reference_analysis if project.camera_at(5.)==2 else self.analysis
        with patch('auto_color_workspace.sample_analyze',side_effect=analyze):
            self.assertTrue(self.workspace.start_auto_color(reference_camera=2))
            self.wait(entered.is_set)
            self.workspace.project.camera_grades[2]=ColorGrade(exposure=.3)
            release.set();self.wait(lambda:not self.workspace._auto_color_running)
        self.assertEqual(self.workspace.project.clip_grades,original_grades)
        self.assertEqual(self.workspace.project.camera_grades[2].exposure,.3)
        self.assertEqual(self.workspace.undo_stack,[])
        self.assertIn('未应用',self.workspace.auto_color_status.get())

    def test_unusable_reference_falls_back_to_portrait_preset_for_target_cut(self):
        self.choose_reference(2);self.workspace.undo_stack.clear()
        unusable=analyze_rgb_samples([bytes((0,0,0))*1024])
        self.assertFalse(unusable.usable)
        expected=build_auto_grade(self.analysis,preset='portrait_clear',base_grade=ColorGrade(),
                                  bias_ev=.25,strength=1.)
        original=copy.deepcopy(self.workspace.project)
        for reference in (unusable,RuntimeError('reference frame unavailable')):
            with self.subTest(reference=type(reference).__name__):
                self.run_reference(reference=reference)
                self.assertEqual(grade_at(self.workspace.project,5),expected)
                self.assertEqual(self.workspace.project.camera_grades[2],self.reference_grade)
                self.assertEqual(grade_at(self.workspace.project,1),self.outside_grade)
                self.assertIsNone(grade_at(self.workspace.project,9))
                self.assertEqual(len(self.workspace.undo_stack),1)
                self.assertIn('参考',self.workspace.auto_color_status.get())
                self.workspace.undo()
                self.assertEqual(self.workspace.project,original)
                self.workspace.redo_stack.clear()

    def test_reference_lut_hdr_and_highlight_warnings_are_prefixed_and_not_dropped(self):
        self.choose_reference(2);self.workspace.undo_stack.clear()
        notes=('参考机位 LUT 不在当前实时显示中，按实时色轮与曲线校色匹配。',
               '素材带 HDR 传递函数；此功能不自动转换 Log/HDR，请核对色彩空间。',
               '高光保护限制了正曝光增益，并温和提亮中间调。')
        reference=replace(self.reference_analysis,warnings=(*notes,notes[0]))
        # Target gamma does not govern which reference warnings are shown.
        with patch('auto_color_workspace.build_auto_grade',return_value=ColorGrade(exposure=-.1,gamma=1.)):
            self.run_reference(reference=reference)
        status=self.workspace.auto_color_status.get()
        for note in notes:self.assertEqual(status.count('参考机位：'+note),1,status)
        self.assertEqual(self.workspace.project.camera_grades[2],self.reference_grade)
        self.assertEqual(len(self.workspace.undo_stack),1)

    def test_repeat_reference_analysis_is_stable_and_not_cumulative(self):
        self.choose_reference(2);self.workspace.undo_stack.clear()
        self.run_reference();first=copy.deepcopy(grade_at(self.workspace.project,5))
        self.run_reference();second=grade_at(self.workspace.project,5)
        self.assertEqual(first,second)
        self.assertEqual(len([item for item in self.workspace.project.clip_grades if item.start==4]),1)

    def test_v6_reference_roundtrip_and_v5_without_new_fields_loads_neutral_defaults(self):
        self.choose_reference(2)
        path=self.folder/'reference.multicam.json'
        self.workspace.project.save(path)
        payload=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(payload['version'],8)
        self.assertEqual(MulticamProject.load(path),self.workspace.project)
        payload['version']=5;payload.pop('color_reference_camera')
        for grade in [payload['global_grade'],*payload['camera_grades'].values(),
                      *(item['grade'] for item in payload['clip_grades'])]:
            for name in ('wb_red','wb_green','wb_blue','skin_deyellow'):grade.pop(name,None)
        path.write_text(json.dumps(payload),encoding='utf-8')
        legacy=MulticamProject.load(path)
        self.assertIsNone(legacy.color_reference_camera)
        self.assertEqual(legacy.camera_grades[2].exposure,.1)
        self.assertEqual((legacy.camera_grades[2].wb_red,legacy.camera_grades[2].wb_green,
                          legacy.camera_grades[2].wb_blue,legacy.camera_grades[2].skin_deyellow),(1.,1.,1.,0.))
        self.assertEqual(grade_at(legacy,1).exposure,self.outside_grade.exposure)


if __name__=='__main__':unittest.main()
