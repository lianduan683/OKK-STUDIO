"""Real-frame regression for imported luma masks, including partial exports."""
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys
from dataclasses import replace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from creative_core import Transition, transition_from_dict, transition_to_dict
from creative_render import plan_creative_segments, TransitionSegment, build_transition_command
from multicam_core import CameraClip, Cut, MulticamProject
from exporter_core import ExportSettings
from gpu_live_player import missing_gpu_features

class LibraryMaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = str(Path(__file__).resolve().parents[1]/'bin/ffmpeg.exe')
        cls.folder = tempfile.TemporaryDirectory(prefix='okk-mask-')
        cls.root = Path(cls.folder.name)
        for name,color in [('a','red'),('b','blue')]:
            cls.run_ffmpeg(['-f','lavfi','-i',f'color={color}:s=160x90:r=12:d=4','-c:v','ffv1',str(cls.root/f'{name}.mkv')])
        cls.mask = cls.root/'horizontal.png'
        cls.run_ffmpeg(['-f','lavfi','-i',"nullsrc=s=160x90,geq=lum='255*X/W':cb=128:cr=128",'-frames:v','1',str(cls.mask)])

    @classmethod
    def run_ffmpeg(cls,args):
        result = subprocess.run([cls.ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-y','-filter_threads','1',*args],capture_output=True,timeout=40)
        if result.returncode:
            raise AssertionError(result.stderr.decode('utf-8','replace'))
        return result.stdout

    @classmethod
    def tearDownClass(cls): cls.folder.cleanup()

    def project(self):
        return MulticamProject(clips=[CameraClip('a',1,str(self.root/'a.mkv'),0,4),CameraClip('b',2,str(self.root/'b.mkv'),0,4)],
            cuts=[Cut(0,1),Cut(2,2)],fps=12,transitions=[Transition(2,'fade',1,str(self.mask))])

    def render(self, job, name, fmt='yuv420p'):
        target=self.root/name
        command=build_transition_command(job,self.project(),[self.ffmpeg,'-hide_banner','-loglevel','error','-y'],
            ExportSettings(),160,90,fmt,'12',['-c:v','ffv1'],target)
        p=subprocess.run(command,capture_output=True,timeout=40)
        self.assertEqual(p.returncode,0,p.stderr.decode('utf-8','replace'))
        raw=self.run_ffmpeg(['-i',str(target),'-f','rawvideo','-pix_fmt','rgb24','-'])
        return [raw[i:i+160*90*3] for i in range(0,len(raw),160*90*3)]

    def test_mask_spatial_wipe_and_endpoints_in_8_and_10_bit(self):
        project=self.project()
        job=next(s for s in plan_creative_segments(project,project.segments()) if isinstance(s,TransitionSegment))
        for fmt in ['yuv420p','yuv420p10le']:
            frames=self.render(job,fmt+'.mkv',fmt)
            self.assertEqual(len(frames),12)
            self.assertGreater(frames[0][0],220); self.assertLess(frames[0][2],30)
            self.assertGreater(frames[-1][2],220); self.assertLess(frames[-1][0],30)
            left=(45*160+20)*3; right=(45*160+140)*3
            self.assertGreater(frames[6][left+2],200)
            self.assertGreater(frames[6][right],200)

    def test_partial_export_keeps_same_mask_phase(self):
        project=self.project()
        job=next(s for s in plan_creative_segments(project,project.segments()) if isinstance(s,TransitionSegment))
        full=self.render(job,'full.mkv')
        partial=self.render(replace(job,start=job.start+3/12,duration=4/12,frames=4,trim_start_frames=3),'partial.mkv')
        self.assertEqual(partial,full[3:7])

    def test_mask_roundtrip_and_explicit_cached_preview_fallback(self):
        effect=self.project().transitions[0]
        self.assertEqual(transition_from_dict(transition_to_dict(effect)),effect)
        self.assertIn('资源遮罩转场',missing_gpu_features(self.project(),{'transitions':['fade'],'grade_uniforms':True,'subtitles':True}))
        self.assertEqual(transition_from_dict({'time':2,'kind':'fade','duration':1}).mask_path,'')

    def test_missing_mask_refuses_to_silently_render_plain_fade(self):
        project=self.project()
        job=next(s for s in plan_creative_segments(project,project.segments()) if isinstance(s,TransitionSegment))
        with self.assertRaisesRegex(ValueError,'遮罩文件缺失'):
            self.render(replace(job,mask_path=str(self.root/'missing.png')),'invalid.mkv')

if __name__=='__main__': unittest.main()
