"""Cross-vendor editor routing and real programme/alpha delivery checks."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from editor_core import build_concat_encode_command, build_proxy_encode_command, join_uses_cuda_frames
from exporter_core import (Capabilities, CREATE_NO_WINDOW, ExportSettings, MediaInfo,
                           detect_capabilities, probe_media)
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam
from okk_cli import build_parser, settings_from_args
from export_parallel import choose_export_workers


ROOT = Path(__file__).resolve().parents[1]


def fixture_caps(vendor):
    return Capabilities(**{vendor + '_codecs': {f'h264_{vendor}', f'hevc_{vendor}'}},
                        cuda_decode=True, scale_cuda=True)


class VendorWorkflowTests(unittest.TestCase):
    def test_4050_automatic_export_is_single_job_but_manual_choice_is_preserved(self):
        caps=Capabilities(gpu_name='NVIDIA GeForce RTX 4050 Laptop GPU',gpu_memory_mb=6144,
            nvenc_codecs={'hevc_nvenc'},cuda_decode=True)
        media=MediaInfo('a.mp4',width=1920,height=1080,bit_depth=8)
        settings=ExportSettings(codec='hevc_nvenc',decode_mode='cuda',auto_parallel=True,parallel_workers=3)
        self.assertEqual(choose_export_workers(settings,caps,[media]),1)
        settings.auto_parallel=False
        self.assertEqual(choose_export_workers(settings,caps,[media]),3)

    def test_editor_h264_proxy_uses_amf_or_qsv_without_cuda_or_nvenc_options(self):
        media=MediaInfo('source.mov',duration=2,width=3840,height=2160,fps=60,bit_depth=10)
        for backend in ('amf','qsv'):
            with self.subTest(backend=backend):
                command=build_proxy_encode_command('ffmpeg',media.path,'proxy.mp4',media,fixture_caps(backend))
                self.assertEqual(command[command.index('-c:v')+1],f'h264_{backend}')
                self.assertNotIn('-hwaccel',command)
                for flag in ('-cq','-multipass','-split_encode_mode','-gpu','-tune'):
                    self.assertNotIn(flag,command)

    def test_proxy_remains_h264_when_only_hardware_hevc_is_available(self):
        media=MediaInfo('source.mov',duration=2,width=640,height=360,fps=30)
        caps=Capabilities(amf_codecs={'hevc_amf'})
        command=build_proxy_encode_command('ffmpeg',media.path,'proxy.mp4',media,caps)
        self.assertEqual(command[command.index('-c:v')+1],'libx264')
        self.assertNotIn('-hwaccel',command)

    def test_stale_cuda_setting_cannot_force_non_nvidia_editor_pipeline(self):
        media=MediaInfo('source.mov',duration=2,width=640,height=360,fps=30)
        for backend in ('amf','qsv'):
            settings=ExportSettings(codec=f'hevc_{backend}',decode_mode='cuda')
            caps=fixture_caps(backend)
            self.assertFalse(join_uses_cuda_frames(settings,media,caps))
            command=build_concat_encode_command('ffmpeg','list.txt','final.mp4',settings,media,caps)
            self.assertNotIn('-hwaccel',command)
            self.assertEqual(command[command.index('-c:v')+1],settings.codec)
            self.assertNotIn('-cq',command)

    def test_connector_exposes_all_hardware_families_and_disables_cuda_for_other_vendors(self):
        parser=build_parser()
        for codec in ('h264_amf','hevc_amf','av1_amf','h264_qsv','hevc_qsv','av1_qsv','libx264'):
            args=parser.parse_args(['--input','a.mov','--output','b.mp4','--codec',codec,'--decode','cuda'])
            settings=settings_from_args(args,media_codec='hevc',cuda_available=True,split_value='3')
            self.assertEqual(settings.codec,codec)
            self.assertEqual(settings.decode_mode,'cpu')
            self.assertEqual(settings.split_encode,'disabled')


class RealVendorRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.caps=detect_capabilities(ROOT)
        if not cls.caps.ffmpeg_path or not cls.caps.ffprobe_path:
            raise unittest.SkipTest('FFmpeg unavailable')
        cls.temp=tempfile.TemporaryDirectory(prefix='vendor_delivery_',dir=ROOT/'work')
        cls.folder=Path(cls.temp.name)
        cls.addClassCleanup(cls.temp.cleanup)
        cls.ffmpeg,cls.ffprobe=cls.caps.ffmpeg_path,cls.caps.ffprobe_path
        cls.blue=cls.folder/'blue.mp4';cls.red=cls.folder/'red.mp4';cls.alpha=cls.folder/'alpha.mov'
        for path,graph,codec in [(cls.blue,'color=blue:s=320x180:r=30:d=2','libx264'),
                                 (cls.red,'color=red:s=320x180:r=30:d=2','libx264'),
                                 (cls.alpha,'color=red:s=320x180:r=30:d=2,format=rgba,colorchannelmixer=aa=0.5','qtrle')]:
            cls.run_ffmpeg(['-f','lavfi','-i',graph,'-c:v',codec,'-threads','2',str(path)])
        cls.master=cls.folder/'master.wav'
        cls.run_ffmpeg(['-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=2',str(cls.master)])

    @classmethod
    def run_ffmpeg(cls,args):
        result=subprocess.run([cls.ffmpeg,'-v','error','-y',*args],capture_output=True,timeout=45,
                              creationflags=CREATE_NO_WINDOW)
        if result.returncode:raise RuntimeError(result.stderr.decode('utf-8','replace'))
        return result.stdout

    def render(self,codec,*,stack=False,depth='8-bit'):
        if codec not in self.caps.hardware_codecs:self.skipTest(f'{codec} not available on this machine')
        if depth=='10-bit' and codec not in self.caps.ten_bit_codecs:
            self.skipTest(f'{codec} 10-bit not available on this machine')
        path=self.alpha if stack else self.red
        project=MulticamProject([CameraClip('b',1,str(self.blue),0,2),
            CameraClip('r',2,str(path),0,2,has_alpha=stack)],
            [Cut(0,1),Cut(1,2)],master_audio=str(self.master),master_duration=2.,fps=30.,
            video_stack_enabled=stack,video_stack_order=[2,1])
        output=self.folder/f'{codec}-{stack}-{depth}.mp4'
        def runner(command,callback=None):
            result=subprocess.run(command,capture_output=True,timeout=60,creationflags=CREATE_NO_WINDOW)
            return result.returncode,result.stderr.decode('utf-8','replace')
        render_multicam(project,str(output),self.ffmpeg,self.ffprobe,
            ExportSettings(codec=codec,decode_mode='cpu',preset='p2',
                bit_depth=depth,audio='aac',parallel_workers=1),self.caps,runner)
        media=probe_media(self.ffprobe,str(output))
        self.assertEqual(media.video_codec,'h264' if codec.startswith('h264') else 'hevc')
        self.assertAlmostEqual(media.duration,2.,delta=.08)
        self.assertTrue(media.has_audio)
        self.assertEqual(media.bit_depth,10 if depth=='10-bit' else 8)
        first=self.pixel(output,.5);second=self.pixel(output,1.5)
        if stack:
            for pixel in (first,second):
                self.assertAlmostEqual(pixel[0],128,delta=15)
                self.assertLess(pixel[1],15)
                self.assertAlmostEqual(pixel[2],127,delta=15)
        else:
            self.assertGreater(first[2],220);self.assertLess(first[0],20)
            self.assertGreater(second[0],220);self.assertLess(second[2],20)

    def pixel(self,path,time):
        raw=self.run_ffmpeg(['-ss',str(time),'-i',str(path),'-frames:v','1','-vf','scale=1:1',
                             '-pix_fmt','rgb24','-f','rawvideo','pipe:1'])
        return tuple(raw[:3])

    def test_real_amf_multicamera_cut_and_master_audio(self):self.render('h264_amf')
    def test_real_amf_ten_bit_alpha_composite(self):self.render('hevc_amf',stack=True,depth='10-bit')
    def test_real_nvenc_multicamera_regression(self):self.render('hevc_nvenc')


if __name__=='__main__':unittest.main()
