from __future__ import annotations
from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path
import subprocess,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from creative_core import ColorGrade,grade_filter
from creative_render import TransitionSegment,build_transition_command
from exporter_core import MediaInfo,ExportSettings,CREATE_NO_WINDOW
import gpu_export as gpu
from cache_paths import cache_dir

KINDS=('fade','dissolve','wipeleft','wiperight','slideleft','slideright','circleopen')


class GpuExportPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.caps=gpu.GpuExportCapabilities('ffmpeg.exe',True,True,True,KINDS,scale_cuda=True)
    def plan(self,grades=(),**kwargs):
        args=dict(ffmpeg_path='ffmpeg.exe',width=128,height=72,pixel_format='yuv420p10le',
            rate_text='30000/1001',frames=60,duration=2.002,shader_dir=self.temp.name)
        args.update(kwargs)
        with patch.object(gpu,'detect_gpu_export',return_value=self.caps):return gpu.build_gpu_video_plan(grades,**args)
    def test_static_three_layer_params_and_decode_paths(self):
        p=self.plan((ColorGrade(exposure=.25,skin_deyellow=.6),ColorGrade(gamma=1.1),ColorGrade(wb_red=.9)))
        shader=Path(p.shader_path).read_text(encoding='utf8')
        self.assertEqual(shader.count('//!TYPE CONSTANT float'),612)
        self.assertNotIn('DYNAMIC',shader);self.assertIn('//!HOOK PREOUTPUT',shader)
        self.assertIn('//!PARAM g0_exposure\n//!TYPE CONSTANT float\n0.25',shader)
        self.assertIn('g2_wb_red',shader);self.assertIn('libplacebo=',p.filter_chain)
        self.assertNotIn('lutrgb',p.filter_chain);self.assertEqual(p.input_options[1],'cuda')
        c=self.plan(decode_mode='cuda',media=MediaInfo('',bit_depth=10),prefix_frames=3)
        self.assertIn('hwdownload,format=p010le,hwupload,libplacebo=',c.filter_chain)
        self.assertIn('trim=start_frame=3:end_frame=63',c.filter_chain)
        self.assertTrue(self.plan(decode_mode='cpu',download=False).filter_chain.endswith('setpts=N/(30000/1001*TB)'))
    def test_cuda_prescales_before_cpu_bridge_and_preserves_highest_depth(self):
        source=MediaInfo('',width=7680,height=4320,bit_depth=10)
        p=self.plan(media=source,width=3840,height=2160)
        self.assertIn('scale_cuda=w=3840:h=2160:interp_algo=bicubic:format=p010le,hwdownload,format=p010le,hwupload,libplacebo=',p.filter_chain)
        self.assertEqual(p.filter_chain.count('scale_cuda='),1)
        same=self.plan(media=replace(source,width=128,height=72))
        self.assertNotIn('scale_cuda=',same.filter_chain)
        eight=replace(source,bit_depth=8)
        high=self.plan(media=eight,width=3840,height=2160)
        self.assertIn('bicubic:format=p010le,hwdownload,format=p010le',high.filter_chain)
        low=self.plan(media=eight,width=3840,height=2160,pixel_format='yuv420p')
        self.assertIn('bicubic:format=nv12,hwdownload,format=nv12',low.filter_chain)
        low_from_ten=self.plan(media=source,width=3840,height=2160,pixel_format='yuv420p')
        self.assertIn('bicubic:format=p010le,hwdownload,format=p010le',low_from_ten.filter_chain)
        self.assertTrue(low_from_ten.filter_chain.endswith('hwdownload,format=nv12'))
        self.caps=replace(self.caps,scale_cuda=False)
        with self.assertRaises(gpu.GpuExportUnsupported):self.plan(media=source,width=3840,height=2160)
    def test_cuda_respects_device_index_and_rejects_invalid_values(self):
        p=self.plan(gpu_index=2)
        self.assertEqual(p.input_options,('-hwaccel','cuda','-hwaccel_device','2','-hwaccel_output_format','cuda'))
        for value in (-1,True,1.5,'0'):
            with self.assertRaises(ValueError):self.plan(gpu_index=value)
    def test_lut_hdr_unsupported_formats_are_explicit(self):
        with self.assertRaises(gpu.GpuExportUnsupported):self.plan((ColorGrade(lut_path='missing.cube'),))
        self.plan((ColorGrade(enabled=False,lut_path='missing.cube'),))
        for transfer in ('smpte2084','arib-std-b67','linear'):
            with self.assertRaises(gpu.GpuExportUnsupported):self.plan(media=MediaInfo('',color_transfer=transfer))
        with self.assertRaises(gpu.GpuExportUnsupported):self.plan(pixel_format='yuv444p16le')
        with self.assertRaises(gpu.GpuExportUnsupported):self.plan(media=MediaInfo('',width=160,height=120))
        with self.assertRaises(gpu.GpuExportUnsupported):self.plan(media=MediaInfo('',width=128,height=72,sample_aspect_ratio='4:3'))
        for kwargs in ({'frames':0},{'prefix_frames':-1},{'width':127},{'rate_text':'bad'}):
            with self.assertRaises(ValueError):self.plan(**kwargs)
    def test_color_flags_and_fine_frame_counts_retained(self):
        media=MediaInfo('',color_space='bt709',color_primaries='bt709',color_transfer='bt709',color_range='pc')
        p=self.plan(media=media)
        self.assertIn('color_trc=bt709',p.filter_chain);self.assertIn('range=pc',p.filter_chain)
        self.assertEqual(p.color_options,('-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709','-color_range','pc'))


class ActualVulkanExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg=gpu.find_gpu_export_ffmpeg()
        if not cls.ffmpeg:raise unittest.SkipTest('Vulkan/libplacebo runtime unavailable')
        cls.temp=tempfile.TemporaryDirectory(prefix="gpu_export_中文'",dir=cache_dir('tests/gpu_export'))
        cls.folder=Path(cls.temp.name);cls.results={}
        cls.project=SimpleNamespace(fps=20.,duration=2.,camera_grades={},global_grade=ColorGrade(exposure=.1),clip_grades=[],
            subtitles=SimpleNamespace(enabled=False,burn_in=False,cues=[]))
        cls.inputs=[]
        for name,color in (('a','0xc53228'),('b','0x2850c5')):
            path=cls.folder/(name+'.mp4')
            cls.run_command([cls.ffmpeg,'-hide_banner','-loglevel','error','-y','-f','lavfi','-i',
                f'color={color}:size=320x180:rate=20:duration=2','-c:v','libx264','-crf','18','-pix_fmt','yuv420p',str(path)])
            cls.inputs.append(str(path))
    @classmethod
    def tearDownClass(cls):
        report=Path(__file__).resolve().parents[2]/'export591_gpu_validation.json'
        report.write_text(json.dumps({'successful':set(cls.results)=={'three_layer_grade','cpu_transition_fallbacks','cuda_8_to_10'},'tests':cls.results,
            'limitations':['HDR and per-layer LUT retain CPU path','All nine transitions retain CPU compositing after failed Vulkan runtime validation',
                'GPU scaler kernel/chroma sampling can differ slightly; encoding quality/resolution unchanged']},ensure_ascii=False,indent=2),encoding='utf8')
        cls.temp.cleanup()
    @staticmethod
    def run_command(command,data=None):
        r=subprocess.run(command,input=data,capture_output=True,timeout=40,creationflags=CREATE_NO_WINDOW)
        if r.returncode:raise AssertionError(str(r.returncode)+': '+r.stderr.decode('utf8','replace')[-5000:])
        return r.stdout
    def test_nine_transitions_explicitly_retain_cpu_and_exact_effect_kind(self):
        self.assertEqual(gpu.VERIFIED_GPU_TRANSITIONS,())
        checks={}
        for kind in (*KINDS,'fadeblack','fadewhite'):
            for partial in (False,True):
                job=TransitionSegment(1,self.inputs[0],0.,0.,1.,20,2,self.inputs[1],0.,kind,20,0,0.)
                if partial:job=replace(job,start=.25,duration=.5,frames=10,trim_start_frames=5)
                with self.assertRaisesRegex(gpu.GpuExportUnsupported,'CPU'):
                    gpu.build_gpu_transition_command(job,self.project,[self.ffmpeg],ExportSettings(),
                        320,180,'yuv420p10le','20',[],'unused.mp4')
                cpu=build_transition_command(job,self.project,[self.ffmpeg],ExportSettings(),
                    320,180,'yuv420p10le','20',[],'unused.mp4')
                graph=cpu[cpu.index('-filter_complex')+1]
                self.assertIn('xfade=transition='+kind+':',graph)
                self.assertNotIn('xfade_vulkan',graph)
                self.assertIn('trim=start_frame='+str(job.trim_start_frames),graph)
                self.assertEqual(cpu[cpu.index('-frames:v')+1],str(job.frames))
            checks[kind]={'gpu_route':'disabled_after_failed_runtime_validation',
                'cpu_effect_unchanged':True,'full_frames':20,'partial_frames':10}
        self.results['cpu_transition_fallbacks']=checks
    def test_actual_three_layer_primary_grade_pixels(self):
        pixels=np.array(((40,55,80),(100,128,150),(193,158,111),(245,232,214),(24,40,32),(164,125,135),(220,45,40),(250,250,250)),dtype=np.uint8)
        raw=np.repeat(np.repeat(pixels[None,:,:],16,axis=1),32,axis=0).tobytes()
        grades=(ColorGrade(wb_red=.9,wb_green=.96,exposure=.2),ColorGrade(skin_lighten=.5,skin_deyellow=.6,
            curve_b=((0.,0.),(.5,.56),(1.,1.))),ColorGrade(gamma=1.06,temperature=-.03,saturation=.94))
        p=gpu.build_gpu_video_plan(grades,ffmpeg_path=self.ffmpeg,width=128,height=32,pixel_format='yuv420p10le',
            rate_text='10',frames=1,duration=.1,decode_mode='cpu',shader_dir=self.folder)
        base=[self.ffmpeg,'-hide_banner','-loglevel','error','-filter_threads','1']
        inp=['-f','rawvideo','-pix_fmt','rgb24','-video_size','128x32','-framerate','10','-i','pipe:0']
        signal='setparams=color_primaries=bt709:color_trc=bt709:colorspace=gbr:range=pc'
        tail=['-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1']
        cpu=','.join([signal,*[grade_filter(g) for g in grades if grade_filter(g)],'format=yuv420p10le'])
        a=np.frombuffer(self.run_command(base+inp+['-vf',cpu]+tail,raw),dtype=np.uint8).reshape(32,128,3)[16,8::16].astype(int)
        b=np.frombuffer(self.run_command(base+list(p.global_options)+inp+['-vf',signal+','+p.filter_chain]+tail,raw),dtype=np.uint8).reshape(32,128,3)[16,8::16].astype(int)
        error=int(abs(a-b).max());self.assertLessEqual(error,3)
        self.results['three_layer_grade']={'maximum_channel_error_255':error}
    def test_actual_cuda_8bit_source_prescales_to_10bit_fractional_output(self):
        source=MediaInfo(self.inputs[0],width=320,height=180,bit_depth=8,color_space='bt709',
            color_transfer='bt709',color_primaries='bt709',color_range='tv')
        plan=gpu.build_gpu_video_plan((ColorGrade(exposure=.2),),ffmpeg_path=self.ffmpeg,width=160,height=90,
            pixel_format='yuv420p10le',rate_text='30000/1001',frames=3,duration=.1001,media=source,
            shader_dir=self.folder)
        self.assertIn('bicubic:format=p010le,hwdownload,format=p010le',plan.filter_chain)
        output=self.folder/'cuda_8_to_10.mp4'
        self.run_command([self.ffmpeg,'-hide_banner','-loglevel','error','-y',*plan.global_options,
            *plan.input_options,'-i',source.path,'-vf',plan.filter_chain,'-an','-c:v','hevc_nvenc','-preset','p2',
            '-rc','vbr','-cq','20','-b:v','0','-pix_fmt','yuv420p10le','-r','30000/1001','-frames:v','3',
            *plan.color_options,'-video_track_timescale','30000',str(output)])
        probe=Path(__file__).resolve().parents[1]/'bin'/'ffprobe.exe'
        data=json.loads(self.run_command([str(probe),'-v','error','-count_frames','-show_streams','-of','json',str(output)]))
        video=data['streams'][0]
        self.assertEqual(video['pix_fmt'],'yuv420p10le');self.assertEqual(video['nb_read_frames'],'3')
        self.assertEqual((video['width'],video['height']),(160,90))
        self.assertEqual(video['avg_frame_rate'],'30000/1001')
        self.assertEqual((video['color_primaries'],video['color_transfer'],video['color_space'],video['color_range']),
            ('bt709','bt709','bt709','tv'))
        self.results['cuda_8_to_10']={'frames':3,'pixel_format':video['pix_fmt'],'rate':video['avg_frame_rate'],
            'dimensions':[160,90],'color_tags_retained':True}


if __name__=='__main__':unittest.main(verbosity=2)
