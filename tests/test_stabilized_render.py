"""Real CPU renders prove range caches reach both ordinary and mixed jobs.

The blue cache deliberately differs from the original red source. Gyroflow's
own motion/rendering tests are separate; here the cache is already prepared.
"""
from __future__ import annotations

import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from creative_core import Transition
from exporter_core import CREATE_NO_WINDOW,Capabilities,ExportSettings
from gyroflow_core import StabilizationSettings,StabilizedRange,cache_signature
from multicam_core import CameraClip,Cut,MulticamProject,render_multicam


class StabilizedRenderMediaTests(unittest.TestCase):
    width,height,fps=128,72,10

    @classmethod
    def run_command(cls,command):
        result=subprocess.run(command,capture_output=True,timeout=45,creationflags=CREATE_NO_WINDOW)
        if result.returncode:raise AssertionError(result.stderr.decode('utf-8','replace')[-3000:])
        return result.stdout

    @classmethod
    def setUpClass(cls):
        app=Path(__file__).resolve().parents[1]
        cls.ffmpeg,cls.ffprobe=(str(app/'bin'/name) for name in ('ffmpeg.exe','ffprobe.exe'))
        if not Path(cls.ffmpeg).is_file():raise unittest.SkipTest('bundled FFmpeg unavailable')
        cls.temporary=tempfile.TemporaryDirectory(prefix='stabilized_render_')
        cls.folder=Path(cls.temporary.name)
        for color in ('red','blue'):
            cls.run_command([cls.ffmpeg,'-v','error','-y','-f','lavfi','-i',
                f'color=c={color}:s=128x72:r=10:d=3','-an','-c:v','libx264','-crf','0',
                '-preset','ultrafast','-threads','1',str(cls.folder/f'{color}.mp4')])
        cls.audio=cls.folder/'master.wav'
        cls.run_command([cls.ffmpeg,'-v','error','-y','-f','lavfi','-i',
            'sine=frequency=613:sample_rate=48000:duration=3','-c:a','pcm_s16le',str(cls.audio)])

    @classmethod
    def tearDownClass(cls):cls.temporary.cleanup()

    def project(self,stabilized=True):
        original=str(self.folder/'red.mp4')
        project=MulticamProject(clips=[CameraClip('red_source',1,original,0.,3.)],
            cuts=[Cut(0.,1)],master_audio=str(self.audio),master_duration=3.,fps=10.)
        if stabilized:
            settings=StabilizationSettings()
            project.stabilizations=[StabilizedRange(start=1.,end=2.,camera=1,source_id='red_source',
                settings=settings,cache_path=str(self.folder/'blue.mp4'),
                signature=cache_signature(original,0.,3.,settings),source_start=0.,source_end=3.)]
        project.validate()
        return project

    def render(self,project,name):
        output=self.folder/f'{name}.mov'
        calls=[]
        def runner(command,report):
            calls.append(command)
            result=subprocess.run(command,capture_output=True,timeout=45,creationflags=CREATE_NO_WINDOW)
            return result.returncode,result.stderr.decode('utf-8','replace')
        render_multicam(project,output,self.ffmpeg,self.ffprobe,
            ExportSettings(codec='libx264',preset='ultrafast',quality=0,audio='pcm_s16le',
                           audio_sample_rate=48000,decode_mode='cpu',parallel_workers=1,
                           bframes=0,extra_args=['-threads','1']),Capabilities(),runner)
        picture=self.run_command([self.ffmpeg,'-v','error','-i',str(output),'-map','0:v:0',
            '-an','-f','rawvideo','-pix_fmt','rgb24','pipe:1'])
        audio=self.run_command([self.ffmpeg,'-v','error','-i',str(output),'-map','0:a:0',
            '-vn','-c:a','pcm_s16le','-f','s16le','pipe:1'])
        size=self.width*self.height*3
        middle=3*((self.height//2)*self.width+self.width//2)
        self.assertEqual(len(picture)//size,30)
        self.assertEqual(len(picture)%size,0)
        pixels=[tuple(picture[index+middle:index+middle+3]) for index in range(0,len(picture),size)]
        return pixels,audio,calls

    def test_selected_range_renders_red_blue_red_and_preserves_master_pcm(self):
        baseline=self.render(self.project(False),'unchanged')
        project=self.project(True)
        before=copy.deepcopy(project)
        pixels,audio,calls=self.render(project,'selected_stabilization')
        self.assertEqual(project,before,'rendering must not mutate saved ranges')
        self.assertEqual(audio,baseline[1])
        self.assertEqual(len(audio),3*48000*2)
        for index,pixel in enumerate(pixels):
            if 10<=index<20:
                self.assertLess(pixel[0],15,(index,pixel));self.assertGreater(pixel[2],235,(index,pixel))
            else:
                self.assertGreater(pixel[0],235,(index,pixel));self.assertLess(pixel[2],15,(index,pixel))
        self.assertTrue(any(str(self.folder/'blue.mp4') in command for command in calls),
                        'the cache must actually be decoded by the final renderer')

    def test_same_camera_cut_crossfades_stabilized_cache_to_original_using_its_handles(self):
        project=self.project(True)
        project.edit_points=[2.]
        project.transitions=[Transition(2.,'fade',1.)]
        pixels,audio,calls=self.render(project,'stabilized_transition')
        baseline=self.render(self.project(False),'transition_audio_reference')
        self.assertEqual(audio,baseline[1])
        self.assertGreater(pixels[5][0],235);self.assertLess(pixels[5][2],15)
        self.assertGreater(pixels[12][2],235);self.assertLess(pixels[12][0],15)
        self.assertTrue(85<pixels[20][0]<175 and 85<pixels[20][2]<175,pixels[20])
        self.assertGreater(pixels[27][0],235);self.assertLess(pixels[27][2],15)
        mixed=next(command for command in calls if '-filter_complex' in command
                   and 'xfade=' in command[command.index('-filter_complex')+1])
        self.assertIn(str(self.folder/'blue.mp4'),mixed)
        self.assertIn(str(self.folder/'red.mp4'),mixed)


if __name__=='__main__':unittest.main()
