"""Real alpha pixels, frame boundaries, grading and master-audio delivery."""
from __future__ import annotations

from dataclasses import replace
from array import array
from pathlib import Path
import os
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from creative_core import ColorGrade
from exporter_core import (CREATE_NO_WINDOW, Capabilities, ExportCancelled, ExportSettings,
                           MediaInfo, detect_capabilities, probe_media)
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam
from program_edits import ClipGrade
from subtitle_core import SubtitleCue, SubtitleStyle, SubtitleTrack
from video_stack_render import (build_stack_command, plan_stack_segments, validate_stack_media)


class StackPlannerTests(unittest.TestCase):
    def make(self):
        return MulticamProject(clips=[CameraClip('top', 1, 'top.mov', -.2, .8, 'wrong.mp4', has_alpha=True),
                                     CameraClip('next', 1, 'next.mov', 1, .4, has_alpha=True),
                                     CameraClip('bottom', 36, 'bottom.mp4', .2, 1.4)],
                               fps=10, master_duration=2, video_stack_enabled=True,
                               video_stack_order=[1, 36], cuts=[Cut(0, 36)])

    def test_half_open_edges_gaps_and_negative_offsets_ignore_programme_cuts(self):
        jobs=plan_stack_segments(self.make())
        self.assertEqual([(job.start,job.frames,[layer.camera for layer in job.layers]) for job in jobs],
                         [(0,2,[1]),(.2,4,[1,36]),(.6,4,[36]),(1,4,[1,36]),(1.4,2,[36]),(1.6,4,[])])
        self.assertAlmostEqual(jobs[0].layers[0].source_start,.2)
        self.assertEqual(jobs[0].layers[0].path,'top.mov')

    def test_hidden_layers_black_and_frame_total_with_partial_range(self):
        project=self.make();project.range_start=.29;project.range_end=1.71
        project.video_stack_hidden=[1,36]
        jobs=plan_stack_segments(project)
        self.assertEqual(sum(job.frames for job in jobs),14)
        self.assertTrue(all(not job.layers for job in jobs))

    def test_opaque_treatment_cannot_remove_original_alpha(self):
        job=plan_stack_segments(self.make())[0]
        layer=replace(job.layers[0],path='baked.mp4')
        info={'top.mov':MediaInfo('top.mov',pixel_format='argb',has_alpha=True),
              'baked.mp4':MediaInfo('baked.mp4',pixel_format='yuv420p')}
        with self.assertRaisesRegex(ValueError,'没有透明通道'):
            validate_stack_media([replace(job,layers=(layer,))],info)

    def test_alpha_webm_requests_libvpx_decoder_and_never_proxy(self):
        project=self.make();job=plan_stack_segments(project)[0]
        command=build_stack_command(job,project,['ffmpeg'],128,72,'yuv420p','10/1',[], 'out.mp4',
                                    media_by_path={'top.mov':MediaInfo('top.mov',video_codec='vp9',has_alpha=True)})
        self.assertIn('libvpx-vp9',command);self.assertNotIn('wrong.mp4',command)
        self.assertNotIn('-hwaccel',command)

    def test_cancelled_stack_keeps_existing_destination_and_cleans_graph_files(self):
        with tempfile.TemporaryDirectory(prefix='stack_cancel_') as folder:
            source=Path(folder)/'source.mov';source.write_bytes(b'original')
            target=Path(folder)/'output.mov';target.write_bytes(b'previous completed movie')
            project=MulticamProject(clips=[CameraClip('a',1,str(source),0,1)],fps=10,
                                    video_stack_enabled=True)
            event=threading.Event();graphs=[]
            def runner(command,report):
                graphs.append(Path(command[command.index('-/filter_complex')+1]))
                self.assertTrue(graphs[-1].is_file())
                Path(command[-1]).write_bytes(b'not a complete movie')
                event.set()
                return 0,''
            info=MediaInfo(str(source),duration=1,width=128,height=72,fps=10)
            with (patch('multicam_core.probe_media',return_value=info),
                  patch('multicam_core.probe_partition_rates',return_value={})):
                with self.assertRaises(ExportCancelled):
                    render_multicam(project,target,'ffmpeg','ffprobe',
                                    ExportSettings(codec='libx264',audio='none'),Capabilities(),runner,
                                    cancel_event=event)
            self.assertEqual(target.read_bytes(),b'previous completed movie')
            self.assertTrue(graphs);self.assertTrue(all(not path.exists() for path in graphs))


class StackMediaTests(unittest.TestCase):
    width,height=128,72

    @classmethod
    def command(cls,args,**kwargs):
        result=subprocess.run(args,capture_output=True,timeout=90,creationflags=CREATE_NO_WINDOW,**kwargs)
        if result.returncode:raise AssertionError(result.stderr.decode('utf8','replace')[-6000:])
        return result.stdout

    @classmethod
    def setUpClass(cls):
        app=Path(__file__).resolve().parents[1]
        cls.ffmpeg=str(app/'bin'/'ffmpeg.exe');cls.ffprobe=str(app/'bin'/'ffprobe.exe')
        if not Path(cls.ffmpeg).is_file():raise unittest.SkipTest('FFmpeg unavailable')
        cls.temp=tempfile.TemporaryDirectory(prefix='stack_alpha_');cls.folder=Path(cls.temp.name)
        cls.top=cls.folder/'transparent.mov';cls.blue=cls.folder/'blue.mov';cls.portrait=cls.folder/'portrait.mov'
        cls.dynamic=cls.folder/'dynamic.mov';cls.dynamic25=cls.folder/'dynamic25.mov'
        def make(path,width,height,pixel,rate=10):
            raw=b''.join(bytes(pixel(n,x,y)) for n in range(rate*2) for y in range(height) for x in range(width))
            cls.command([cls.ffmpeg,'-v','error','-y','-f','rawvideo','-pixel_format','rgba',
                         '-video_size',f'{width}x{height}','-framerate',str(rate),'-i','pipe:0','-an',
                         '-c:v','qtrle','-threads','1',str(path)],input=raw)
        make(cls.top,128,72,lambda n,x,y:(255,0,0,0 if x<32 else 128 if x<96 else 255))
        make(cls.blue,128,72,lambda n,x,y:(0,0,255,255))
        make(cls.portrait,36,72,lambda n,x,y:(255,0,0,255))
        make(cls.dynamic,128,72,lambda n,x,y:(20+n*10,0,0,128))
        make(cls.dynamic25,128,72,lambda n,x,y:(20+n*4,0,0,128),25)
        cls.deep=cls.folder/'deep.mkv';raw=array('H')
        for _ in range(2):
            for y in range(72):
                for x in range(128):
                    value=1000+63*(x+128*(y%8))
                    raw.extend((value,value,value,32768))
        cls.command([cls.ffmpeg,'-v','error','-y','-f','rawvideo','-pixel_format','rgba64le',
                     '-video_size','128x72','-framerate','10','-i','pipe:0','-c:v','ffv1',
                     '-pix_fmt','gbrap16le','-threads','1',str(cls.deep)],input=raw.tobytes())
        cls.audio=cls.folder/'master.wav'
        cls.command([cls.ffmpeg,'-v','error','-y','-f','lavfi','-i',
                     'sine=frequency=619:sample_rate=48000:duration=2','-c:a','pcm_s16le',str(cls.audio)])

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def project(self):
        return MulticamProject(clips=[CameraClip('a',1,str(self.top),0,2, 'missing-proxy.mp4',has_alpha=True),
                                     CameraClip('b',2,str(self.blue),0,2)],
                               cuts=[Cut(0,2)],fps=10,range_end=2,master_audio=str(self.audio),
                               master_duration=2,video_stack_enabled=True,video_stack_order=[1,2])

    def runner(self,command,report):
        self.commands.append(command)
        result=subprocess.run(command,capture_output=True,timeout=90,creationflags=CREATE_NO_WINDOW)
        packet={}
        for line in result.stdout.decode('utf8','replace').splitlines():
            key,sep,value=line.partition('=')
            if sep:
                packet[key]=value
                if key=='progress':report(packet);packet={}
        return result.returncode,result.stderr.decode('utf8','replace')

    def render(self,project,name,**overrides):
        self.commands=[];output=self.folder/(name+'.mov')
        settings=ExportSettings(codec='libx264',preset='ultrafast',quality=0,bframes=0,
                                audio='pcm_s16le',decode_mode='cpu',parallel_workers=2,
                                extra_args=['-threads','1'])
        for key,value in overrides.items():setattr(settings,key,value)
        render_multicam(project,output,self.ffmpeg,self.ffprobe,settings,Capabilities(),self.runner)
        rgb=self.command([self.ffmpeg,'-v','error','-i',str(output),'-map','0:v:0','-an',
                          '-pix_fmt','rgb24','-f','rawvideo','pipe:1'])
        return output,rgb

    def pixel(self,rgb,frame,x=64,y=36):
        offset=((frame*self.height+y)*self.width+x)*3
        return tuple(rgb[offset:offset+3])

    def close_pixel(self,value,expected,tolerance=5):
        self.assertEqual(len(value),3)
        self.assertTrue(all(abs(a-b)<=tolerance for a,b in zip(value,expected)),(value,expected))

    def test_true_alpha_order_proxy_isolation_and_continuous_master_audio(self):
        project=self.project();output,rgb=self.render(project,'alpha')
        self.assertEqual(len(rgb),20*128*72*3)
        self.close_pixel(self.pixel(rgb,3,16),(0,0,255))
        self.close_pixel(self.pixel(rgb,3),(128,0,127))
        self.close_pixel(self.pixel(rgb,3,112),(255,0,0))
        self.assertTrue(all('missing-proxy.mp4' not in command for command in self.commands))
        audio=lambda path:self.command([self.ffmpeg,'-v','error','-i',str(path),'-map','0:a:0','-vn',
                                        '-ac','1','-c:a','pcm_s16le','-f','s16le','pipe:1'])
        self.assertEqual(audio(output),audio(self.audio))
        project.video_stack_order=[2,1]
        _,reversed_rgb=self.render(project,'reverse')
        self.close_pixel(self.pixel(reversed_rgb,3),(0,0,255))

    def test_negative_offsets_clip_end_gap_and_partial_export_pixels(self):
        project=self.project();project.clips[0].path=str(self.dynamic)
        project.clips[0].start=-.2;project.clips[0].duration=.8
        project.clips[1].start=.2;project.clips[1].duration=1.4
        project.clips.append(CameraClip('a2',1,str(self.top),1,.4,has_alpha=True))
        _,full=self.render(project,'boundaries')
        self.close_pixel(self.pixel(full,0),(20,0,0)) # source frame 2, half opacity
        self.close_pixel(self.pixel(full,2),(30,0,127))
        self.close_pixel(self.pixel(full,6),(0,0,255)) # top ended exactly here
        self.close_pixel(self.pixel(full,10),(128,0,127))
        self.close_pixel(self.pixel(full,14),(0,0,255))
        self.close_pixel(self.pixel(full,16),(0,0,0))
        project.range_start=.3;project.range_end=1.7
        _,partial=self.render(project,'partial')
        stride=128*72*3
        self.assertEqual(partial,full[3*stride:17*stride])

    def test_camera_grade_preserves_alpha_and_programme_and_global_run_once(self):
        project=self.project();project.camera_grades={1:ColorGrade(exposure=-1)}
        project.clip_grades=[ClipGrade(.5,1.5,ColorGrade(exposure=-1))]
        project.global_grade=ColorGrade(exposure=-1)
        _,rgb=self.render(project,'grades')
        self.close_pixel(self.pixel(rgb,2),(32,0,64))
        self.close_pixel(self.pixel(rgb,7),(16,0,32))
        self.close_pixel(self.pixel(rgb,17),(32,0,64))
        self.close_pixel(self.pixel(rgb,7,16),(0,0,64))

    def test_nonlinear_programme_grade_runs_after_translucent_blend(self):
        project=self.project()
        project.clip_grades=[ClipGrade(.5,1.5,ColorGrade(gamma=.5))]
        project.global_grade=ColorGrade(exposure=-1)
        _,rgb=self.render(project,'nonlinear')
        self.close_pixel(self.pixel(rgb,2),(64,0,64))
        self.close_pixel(self.pixel(rgb,7),(32,0,31))
        self.close_pixel(self.pixel(rgb,17),(64,0,64))

    def test_mixed_rate_phase_is_unchanged_by_grade_boundaries_or_partial_export(self):
        project=self.project();project.fps=30;project.range_end=1.8
        project.clips[0].path=str(self.dynamic25);project.clips[0].start=-.017
        _,original=self.render(project,'mixed_rate')
        project.clip_grades=[ClipGrade(7/30,26/30,ColorGrade())]
        _,split=self.render(project,'mixed_rate_split')
        self.assertEqual(split,original)
        project.range_start=.3;project.range_end=1.6
        _,partial=self.render(project,'mixed_rate_partial')
        stride=128*72*3
        self.assertEqual(partial,original[9*stride:48*stride])
        with patch('multicam_core.probe_partition_rates',return_value={}):
            _,unknown=self.render(project,'mixed_rate_unknown')
        self.assertEqual(unknown,partial)

    def test_aspect_fit_padding_is_transparent_and_subtitle_is_on_top(self):
        project=self.project();project.clips[0].path=str(self.portrait)
        # Delivery canvas follows the first imported source, like normal mode.
        project.clips.reverse()
        _,rgb=self.render(project,'padding')
        self.close_pixel(self.pixel(rgb,2,16),(0,0,255))
        self.close_pixel(self.pixel(rgb,2),(255,0,0))
        project.subtitles=SubtitleTrack(cues=[SubtitleCue('cue',.5,1.5,'TEST')],
            style=SubtitleStyle(font_size=240,margin_y=20,outline_width=0,shadow=0))
        _,subtitled=self.render(project,'subtitle')
        stride=128*72*3
        self.assertEqual(subtitled[:5*stride],rgb[:5*stride])
        self.assertNotEqual(subtitled[8*stride:9*stride],rgb[8*stride:9*stride])
        self.assertEqual(subtitled[16*stride:],rgb[16*stride:])

    def test_36_simultaneous_layers_render_without_camera_limit(self):
        project=self.project();project.range_end=.2;project.master_audio=''
        project.clips=[CameraClip(str(camera),camera,str(self.dynamic),0,2,has_alpha=True)
                       for camera in range(1,37)]
        project.video_stack_order=list(range(1,37))
        _,rgb=self.render(project,'layers36',audio='none')
        self.assertEqual(len(rgb),2*128*72*3)
        self.close_pixel(self.pixel(rgb,0),(20,0,0))
        self.assertEqual(self.commands[0].count('-i'),36)

    def test_ten_bit_delivery_keeps_alpha_composition(self):
        project=self.project();project.range_end=.2
        project.camera_grades={1:ColorGrade(exposure=-1)}
        output,rgb=self.render(project,'deep',codec='libx265',bit_depth='10-bit',audio='none',
                               extra_args=['-threads','1','-x265-params','pools=1:frame-threads=1'])
        self.assertEqual(probe_media(self.ffprobe,output).bit_depth,10)
        self.close_pixel(self.pixel(rgb,0),(64,0,127),tolerance=7)

    def test_true_high_bit_alpha_survives_rgb_white_balance_and_mask_restore(self):
        project=MulticamProject(clips=[CameraClip('deep',1,str(self.deep),0,.2,has_alpha=True)],
            fps=10,video_stack_enabled=True,camera_grades={1:ColorGrade(wb_red=1.000001)})
        output,_=self.render(project,'precision',codec='libx265',bit_depth='10-bit',audio='none',
            extra_args=['-threads','1','-x265-params','pools=1:frame-threads=1:lossless=1'])
        self.assertEqual(probe_media(self.ffprobe,self.deep).pixel_format,'gbrap16le')
        self.assertEqual(probe_media(self.ffprobe,output).bit_depth,10)
        decoded=self.command([self.ffmpeg,'-v','error','-i',str(output),'-frames:v','1',
            '-pix_fmt','yuv420p10le','-f','rawvideo','pipe:1'])
        luma=array('H');luma.frombytes(decoded[:128*72*2])
        self.assertGreater(len(set(luma)),256,'16-bit alpha layer was reduced to 8-bit colour')

    @unittest.skipUnless(os.environ.get('OKK_TEST_NVENC')=='1','set OKK_TEST_NVENC=1 for hardware validation')
    def test_nvenc_delivery_uses_software_alpha_decode_and_hardware_encode(self):
        caps=detect_capabilities(Path(__file__).resolve().parents[1])
        if 'h264_nvenc' not in caps.nvenc_codecs or 'not detected' in caps.gpu_name:
            self.skipTest('NVIDIA GPU unavailable')
        project=self.project();project.range_end=.3
        output,rgb=self.render(project,'nvenc_alpha',codec='h264_nvenc',preset='p1',quality=16,
                               decode_mode='cuda',audio='none',extra_args=[],resolution='1080p')
        self.assertEqual(probe_media(self.ffprobe,output).video_codec,'h264')
        offset=((1080+540)*1920+960)*3
        self.close_pixel(tuple(rgb[offset:offset+3]),(128,0,127),tolerance=9)
        self.assertIn('h264_nvenc',self.commands[0]);self.assertNotIn('-hwaccel',self.commands[0])
