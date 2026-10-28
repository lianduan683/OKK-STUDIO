"""Warm-camera neutral/skin matching, independent of differently framed stages."""
from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from auto_color import analyze_rgb_samples,build_auto_grade,sample_analyze,reference_match_summary
from reference_color import white_balance_plan
from creative_core import ColorGrade,grade_filter
from exporter_core import CREATE_NO_WINDOW
from multicam_core import CameraClip,MulticamProject

WARM_SKIN=(196,160,118)
WARM_WHITE=(245,232,213)
COOL_SKIN=(201,175,181)
COOL_WHITE=(236,237,249)
PURPLE=(65,30,100)

def scene(skin,white,skin_count=250,white_count=200,purple_count=500):
    return bytes(skin)*skin_count+bytes(white)*white_count+bytes(PURPLE)*purple_count+bytes((8,8,10))*50

def chroma(pixel):
    mean=sum(pixel)/3
    return tuple(value/mean for value in pixel)

def distance(first,second):
    return math.sqrt(sum((a-b)**2 for a,b in zip(chroma(first),chroma(second))))


class CameraReferenceDecisionTests(unittest.TestCase):
    def setUp(self):
        self.target=analyze_rgb_samples([scene(WARM_SKIN,WARM_WHITE)])
        self.reference=analyze_rgb_samples([scene(COOL_SKIN,COOL_WHITE,skin_count=550,white_count=130,purple_count=270)])

    def test_shared_bright_neutrals_drive_gains_despite_different_subject_coverage(self):
        plan=white_balance_plan(self.target,self.reference)
        self.assertTrue(plan.used_reference)
        self.assertEqual(plan.source,'亮部中性色')
        self.assertAlmostEqual(max(plan.gains),1.)
        self.assertLess(plan.gains[0],plan.gains[2])
        self.assertLess(plan.gains[1],plan.gains[2])
        self.assertGreaterEqual(min(plan.gains),1/1.42)
        mapped=tuple(value*gain for value,gain in zip(WARM_WHITE,plan.gains))
        self.assertLess(distance(mapped,COOL_WHITE),distance(WARM_WHITE,COOL_WHITE)*.1)
        self.assertIn('参考',reference_match_summary(self.target,self.reference))

    def test_skin_cohort_includes_cool_pink_but_excludes_purple_and_yellow_hair(self):
        frame=bytes(COOL_SKIN)*300+bytes((205,180,115))*500+bytes(PURPLE)*500
        result=analyze_rgb_samples([frame])
        self.assertEqual(result.skin_count,300)
        for actual,expected in zip(result.skin_rgb,COOL_SKIN):self.assertAlmostEqual(actual,expected/255)
        self.assertGreater(result.skin_rgb[2],result.skin_rgb[1])

    def test_missing_or_sparse_reference_falls_back_without_forced_blue(self):
        black=analyze_rgb_samples([bytes((0,0,0))*1000])
        plan=white_balance_plan(self.target,black)
        self.assertFalse(plan.used_reference)
        self.assertTrue(plan.warnings)
        sparse=analyze_rgb_samples([scene(COOL_SKIN,COOL_WHITE)],samples_requested=7)
        self.assertFalse(white_balance_plan(self.target,sparse).used_reference)
        green=analyze_rgb_samples([bytes((10,180,20))*1000])
        self.assertEqual(white_balance_plan(green).gains,(1.,1.,1.))

    def test_unreferenced_portrait_also_removes_yellow_and_is_repeatable(self):
        grade=build_auto_grade(self.target,'portrait_clear')
        self.assertGreater(grade.skin_deyellow,.7)
        self.assertLess(grade.wb_red,grade.wb_blue)
        repeated=build_auto_grade(self.target,'portrait_clear',base_grade=grade)
        self.assertEqual(grade,repeated)
        referenced=build_auto_grade(self.target,'portrait_clear',reference_analysis=self.reference)
        self.assertLess(referenced.skin_deyellow,grade.skin_deyellow)
        self.assertGreater(referenced.skin_lighten,.45)
        self.assertEqual(build_auto_grade(self.target,'portrait_clear',base_grade=referenced,
                                         reference_analysis=self.reference),referenced)

    def test_manual_wheels_curves_and_lut_stay_while_automatic_wb_is_replaced(self):
        base=ColorGrade(lift_r=.06,curve_master=((0.,0.),(.5,.56),(1.,1.)),
                        lut_path='saved.cube',wb_red=.5,skin_deyellow=.8)
        grade=build_auto_grade(self.target,'portrait_clear',base_grade=base,reference_analysis=self.reference)
        self.assertEqual((grade.lift_r,grade.curve_master,grade.lut_path),(base.lift_r,base.curve_master,base.lut_path))
        self.assertNotEqual(grade.wb_red,.5)
        self.assertEqual(build_auto_grade(analyze_rgb_samples([bytes((0,0,0))*100]),base_grade=base),base)

    def test_small_bright_clothing_area_is_protected_when_most_of_stage_is_dark(self):
        result=analyze_rgb_samples([bytes((50,50,50))*990+bytes((248,248,248))*10])
        grade=build_auto_grade(result,'portrait_clear')
        self.assertLess(result.rgb_peak_p98,.3)
        self.assertGreater(result.rgb_peak_p995,.97)
        self.assertLess((248/255)*2**grade.exposure,1.)
        self.assertGreater(grade.gamma,1.)


class ReferenceColourCpuTests(unittest.TestCase):
    def setUp(self):
        self.ffmpeg=APP/'bin'/'ffmpeg.exe';self.ffprobe=APP/'bin'/'ffprobe.exe'
        if not self.ffmpeg.is_file():self.skipTest('Bundled FFmpeg unavailable')
        self.temp=tempfile.TemporaryDirectory(prefix='reference_colour_');self.folder=Path(self.temp.name)
    def tearDown(self):
        if hasattr(self,'temp'):self.temp.cleanup()
    def run_ffmpeg(self,command,data=None):
        result=subprocess.run([str(self.ffmpeg),'-hide_banner','-loglevel','error',*command],input=data,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=CREATE_NO_WINDOW,timeout=40)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
        return result.stdout
    def test_real_export_skin_and_white_are_closer_to_cool_reference_without_new_clipping(self):
        target=analyze_rgb_samples([scene(WARM_SKIN,WARM_WHITE)])
        reference=analyze_rgb_samples([scene(COOL_SKIN,COOL_WHITE,550,130,270)])
        grade=build_auto_grade(target,'portrait_clear',reference_analysis=reference)
        raw=bytes(WARM_SKIN+WARM_WHITE+PURPLE+(8,8,10))*2
        output=self.run_ffmpeg(['-f','rawvideo','-pixel_format','rgb24','-video_size','4x2','-i','pipe:0',
            '-frames:v','1','-vf',grade_filter(grade),'-pix_fmt','rgb24','-f','rawvideo','pipe:1'],raw)
        actual=[tuple(output[index:index+3]) for index in range(0,12,3)]
        self.assertLess(distance(actual[0],COOL_SKIN),distance(WARM_SKIN,COOL_SKIN)*.65)
        self.assertLess(distance(actual[1],COOL_WHITE),distance(WARM_WHITE,COOL_WHITE)*.25)
        self.assertLessEqual(max(value for pixel in actual for value in pixel),254)
        self.assertGreater(sum(actual[0]),sum(WARM_SKIN))
        self.actual={'input':WARM_SKIN,'output':actual[0],'reference':COOL_SKIN,
                     'white_input':WARM_WHITE,'white_output':actual[1],'white_reference':COOL_WHITE,
                     'skin_chroma_distance_before':distance(WARM_SKIN,COOL_SKIN),
                     'skin_chroma_distance_after':distance(actual[0],COOL_SKIN),
                     'maximum_channel':max(value for pixel in actual for value in pixel)}
        (APP.parent/'reference_colour_cpu_pixels.json').write_text(json.dumps(self.actual,indent=2),encoding='utf-8')
    def test_reference_camera_grade_is_sampled_but_target_raw_is_not_reanalysed(self):
        source=self.folder/'gray.mkv';frame=bytes((80,80,80))*64*36
        self.run_ffmpeg(['-y','-f','rawvideo','-pixel_format','rgb24','-video_size','64x36','-framerate','10',
                         '-i','pipe:0','-c:v','ffv1','-pix_fmt','bgr0',str(source)],frame*12)
        project=MulticamProject(clips=[CameraClip('a',1,str(source),0,1.2)],fps=10)
        raw=sample_analyze(project,0,1,str(self.ffmpeg),str(self.ffprobe))
        reference=sample_analyze(project,0,1,str(self.ffmpeg),str(self.ffprobe),
                                 sample_grade=ColorGrade(exposure=.5,lut_path='unavailable.cube'))
        self.assertGreater(reference.luma_p50,raw.luma_p50*1.35)
        self.assertTrue(any('LUT' in warning for warning in reference.warnings))
        project.global_grade=ColorGrade(exposure=2)
        repeat=sample_analyze(project,0,1,str(self.ffmpeg),str(self.ffprobe))
        self.assertEqual(raw.histogram,repeat.histogram)


if __name__=='__main__':unittest.main()
