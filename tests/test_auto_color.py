"""Automatic selected-cut grading decisions and real FFmpeg frame sampling."""
from __future__ import annotations

from dataclasses import replace
import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from auto_color import (analyze_rgb_samples, build_auto_grade, sample_analyze,
                        _parse_ppm, PRESET_LABELS)
from creative_core import ColorGrade, validate_grade
from exporter_core import CREATE_NO_WINDOW, ExportCancelled
from multicam_core import CameraClip, Cut, MulticamProject


def pixels(rgb,count=1024):
    return bytes(rgb)*count


class AutomaticGradeDecisionTests(unittest.TestCase):
    def test_normal_target_bias_is_exact_quarter_stop_and_strength_is_non_cumulative(self):
        analysis=analyze_rgb_samples([pixels((115,115,115))])
        normal=build_auto_grade(analysis,bias_ev=0)
        preferred=build_auto_grade(analysis,bias_ev=.25)
        self.assertAlmostEqual(preferred.exposure-normal.exposure,.25,places=10)
        self.assertAlmostEqual(preferred.exposure,.25,delta=.01)
        self.assertEqual(build_auto_grade(analysis,base_grade=preferred),preferred)
        half=build_auto_grade(analysis,base_grade=preferred,strength=.5)
        self.assertAlmostEqual(half.exposure,preferred.exposure*.5)
        self.assertAlmostEqual(build_auto_grade(analysis,strength=0).exposure,0)

    def test_dark_normal_and_bright_samples_receive_different_exposure_decisions(self):
        dark=analyze_rgb_samples([pixels((55,55,55))])
        normal=analyze_rgb_samples([pixels((115,115,115))])
        bright=analyze_rgb_samples([pixels((190,190,190))])
        grades=[build_auto_grade(item) for item in (dark,normal,bright)]
        self.assertGreater(grades[0].exposure,grades[1].exposure)
        self.assertGreater(grades[1].exposure,grades[2].exposure)
        self.assertLess(grades[2].exposure,0)

    def test_large_white_highlights_limit_gain_instead_of_clipping_then_use_gentle_gamma(self):
        analysis=analyze_rgb_samples([pixels((40,40,40),900)+pixels((240,240,240),100)])
        grade=build_auto_grade(analysis)
        self.assertLess(grade.exposure,.1)
        self.assertGreater(grade.gamma,1.)
        self.assertLessEqual(grade.gamma,1.25)
        self.assertLess((240/255)*2**grade.exposure,1.)
        self.assertTrue(any('高光' in warning for warning in analysis.warnings))

    def test_black_borders_do_not_replace_the_content_reference_for_backlit_midtones(self):
        analysis=analyze_rgb_samples([pixels((0,0,0),600)+pixels((50,50,50),300)+pixels((240,240,240),100)])
        self.assertEqual(analysis.luma_p50,0.)
        self.assertAlmostEqual(analysis.reference_luma,50/255)
        grade=build_auto_grade(analysis)
        self.assertGreater(grade.gamma,1.)
        self.assertLess((240/255)*2**grade.exposure,1.)

    def test_black_white_and_missing_pixels_do_not_generate_extreme_grades(self):
        base=ColorGrade(exposure=.3,saturation=.8)
        for value in (0,255):
            analysis=analyze_rgb_samples([pixels((value,value,value))])
            self.assertFalse(analysis.usable)
            self.assertEqual(build_auto_grade(analysis,base_grade=base),base)
        with self.assertRaisesRegex(ValueError,'没有可供分析'):
            analyze_rgb_samples([b''])

    def test_white_balance_is_conservative_and_does_not_neutralize_saturated_red(self):
        cold=analyze_rgb_samples([pixels((116,128,140))])
        self.assertGreater(cold.temperature,0)
        self.assertLessEqual(abs(cold.temperature),.2)
        colored=analyze_rgb_samples([pixels((220,20,20))])
        self.assertEqual((colored.temperature,colored.tint),(0.,0.))
        self.assertTrue(any('中性' in warning for warning in colored.warnings))

    def test_manual_wheels_curves_and_lut_remain_owned_by_user(self):
        base=ColorGrade(exposure=1.5,temperature=.4,lift_r=.12,
            curve_master=((0.,0.),(.5,.58),(1.,1.)),lut_path='user.cube')
        analysis=analyze_rgb_samples([pixels((115,115,115))])
        grade=build_auto_grade(analysis,base_grade=base)
        self.assertEqual(grade.lift_r,base.lift_r)
        self.assertEqual(grade.curve_master,base.curve_master)
        self.assertEqual(grade.lut_path,base.lut_path)
        self.assertNotEqual(grade.exposure,base.exposure)
        self.assertEqual(base.exposure,1.5)

    def test_portrait_presets_control_selective_skin_strength(self):
        if not hasattr(ColorGrade(),'skin_lighten'): self.skipTest('Portrait engine not integrated yet')
        analysis=analyze_rgb_samples([pixels((115,115,115))])
        natural=build_auto_grade(analysis,'natural')
        portrait=build_auto_grade(analysis,'portrait_natural')
        clear=build_auto_grade(analysis,'portrait_clear')
        self.assertEqual(natural.skin_lighten,0)
        self.assertGreater(portrait.skin_lighten,0)
        self.assertGreater(clear.skin_lighten,portrait.skin_lighten)
        self.assertGreater(clear.skin_saturation,portrait.skin_saturation)
        self.assertAlmostEqual(clear.exposure,natural.exposure)
        validate_grade(clear,check_lut=False)

    def test_invalid_options_and_ppm_whitespace_pixels(self):
        analysis=analyze_rgb_samples([pixels((115,115,115))])
        for kwargs in ({'preset':'missing'},{'bias_ev':float('nan')},{'strength':2}):
            with self.assertRaises(ValueError): build_auto_grade(analysis,**kwargs)
        raw=bytes((10,13,32,0,255,17))
        self.assertEqual(_parse_ppm(b'P6\n2 1\n255\n'+raw),raw)
        with self.assertRaises(ValueError): _parse_ppm(b'P6\n2 1\n255\n'+raw[:-1])


class RealFrameAnalysisTests(unittest.TestCase):
    def setUp(self):
        app=Path(__file__).resolve().parents[1]
        self.ffmpeg=app/'bin'/'ffmpeg.exe'
        self.ffprobe=app/'bin'/'ffprobe.exe'
        if not self.ffmpeg.is_file(): self.skipTest('Bundled FFmpeg unavailable')
        self.temporary=tempfile.TemporaryDirectory(prefix='auto_color_samples_')
        self.folder=Path(self.temporary.name)

    def tearDown(self):
        if hasattr(self,'temporary'): self.temporary.cleanup()

    def video(self,name,rgb):
        path=self.folder/(name+'.mkv')
        frame=bytes(rgb)*96*54
        command=[str(self.ffmpeg),'-hide_banner','-loglevel','error','-y','-f','rawvideo',
            '-pixel_format','rgb24','-video_size','96x54','-framerate','10','-i','pipe:0',
            '-c:v','ffv1','-pix_fmt','bgr0',str(path)]
        result=subprocess.run(command,input=frame*20,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              creationflags=CREATE_NO_WINDOW,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
        return str(path)

    def test_actual_cut_sources_are_sampled_and_existing_grade_is_not_baked_into_analysis(self):
        dark=self.video('dark',(55,55,55));bright=self.video('bright',(190,190,190))
        project=MulticamProject(clips=[CameraClip('a',1,dark,0,2),CameraClip('b',2,bright,0,2)],
                                cuts=[Cut(0,1),Cut(1,2)],fps=10.)
        original=copy.deepcopy(project);progress=[]
        first=sample_analyze(project,0,1,str(self.ffmpeg),str(self.ffprobe),progress=lambda *item: progress.append(item))
        second=sample_analyze(project,1,2,str(self.ffmpeg),str(self.ffprobe))
        self.assertEqual(project,original)
        self.assertLess(first.luma_p50,second.luma_p50)
        self.assertGreater(build_auto_grade(first).exposure,build_auto_grade(second).exposure)
        self.assertEqual(first.samples_used,7)
        self.assertTrue(all(0<=time<1 for time in first.sample_times))
        self.assertTrue(all(1<=time<2 for time in second.sample_times))
        self.assertEqual(progress[-1][0],100.)
        project.global_grade=ColorGrade(exposure=2,saturation=.5)
        repeat=sample_analyze(project,0,1,str(self.ffmpeg),str(self.ffprobe))
        self.assertEqual(first.histogram,repeat.histogram)

    def test_one_frame_and_black_gaps_are_bounded_and_cancellation_is_immediate(self):
        path=self.video('normal',(115,115,115))
        project=MulticamProject(clips=[CameraClip('a',1,path,0,1)],master_duration=3,fps=10)
        one=sample_analyze(project,.1,.2,str(self.ffmpeg),str(self.ffprobe))
        self.assertEqual(one.samples_used,1)
        mixed=sample_analyze(project,0,3,str(self.ffmpeg),str(self.ffprobe))
        self.assertLess(mixed.samples_used,mixed.samples_requested)
        self.assertTrue(any('跳过' in note for note in mixed.warnings))
        event=threading.Event();event.set()
        with self.assertRaises(ExportCancelled):
            sample_analyze(project,0,1,str(self.ffmpeg),str(self.ffprobe),cancel_event=event)
        with self.assertRaisesRegex(ValueError,'没有可读取'):
            sample_analyze(project,1.1,2,str(self.ffmpeg),str(self.ffprobe))
        with self.assertRaisesRegex(ValueError,'不足一帧'):
            sample_analyze(project,.01,.02,str(self.ffmpeg),str(self.ffprobe))


if __name__=='__main__': unittest.main()
