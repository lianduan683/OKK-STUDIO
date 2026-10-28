"""Integer motion-coordinate restoration and real 4K picture correctness."""
from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
import image_stabilization as backend
from cache_paths import cache_dir
from exporter_core import CREATE_NO_WINDOW,ExportCancelled
from gyroflow_core import StabilizationSettings


class MotionCoordinateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='motion_coordinates_',dir=cache_dir('tests/performance581'))
        self.folder=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def test_analysis_sizes_use_exact_isotropic_even_dimensions(self):
        for size,expected in (((1920,1080),1),((3840,2160),2),((7680,4320),4),((8192,4320),4)):
            with self.subTest(size=size):self.assertEqual(backend._analysis_factor(*size),expected)
        for size in ((8192,5464),(3862,2160),(7680,4320)):
            factor=backend._analysis_factor(*size)
            self.assertEqual(size[0]%(2*factor),0);self.assertEqual(size[1]%(2*factor),0)

    def test_motion_centres_sizes_and_displacements_scale_but_scores_do_not(self):
        source=self.folder/'small.trf';native=self.folder/'native.trf'
        source.write_text('VID.STAB 1\nFrame 1 (List 0 [])\nFrame 2 (List 2 [(LM -3 5 120 180 32 0.82 3.125),(LM 4 -2 840 500 56 0.75 1.8)])\n',encoding='ascii')
        self.assertEqual(backend._restore_motion_coordinates(source,native,4,2),1)
        text=native.read_text(encoding='ascii')
        self.assertIn('(LM -12 20 480 720 128 0.82 3.125)',text)
        self.assertIn('(LM 16 -8 3360 2000 224 0.75 1.8)',text)
        self.assertIn('Frame 1 (List 0 [])',text)

    def test_incomplete_motion_record_and_cancellation_are_rejected(self):
        source=self.folder/'small.trf';native=self.folder/'native.trf'
        source.write_text('VID.STAB 1\nFrame 1 (List 1 [])\n',encoding='ascii')
        with self.assertRaisesRegex(RuntimeError,'不完整'):
            backend._restore_motion_coordinates(source,native,2,1)
        event=threading.Event();event.set()
        with self.assertRaises(ExportCancelled):backend._restore_motion_coordinates(source,native,2,1,cancel_event=event)

    def test_fine_signature_matches_legacy_and_fast_uses_a_distinct_binding(self):
        source=self.folder/'source.mp4';source.write_bytes(b'fixture')
        fields=dict(image_smoothing=.5,image_shakiness=5,image_zoom=True,image_tripod=False,image_model='similarity')
        legacy=SimpleNamespace(**fields)
        fine=SimpleNamespace(**fields,image_quality='fine')
        fast=SimpleNamespace(**fields,image_quality='fast')
        self.assertEqual(backend.image_cache_signature(source,0,1,legacy),backend.image_cache_signature(source,0,1,fine))
        self.assertNotEqual(backend.image_cache_signature(source,0,1,fine),backend.image_cache_signature(source,0,1,fast))
        self.assertNotIn('image_quality',backend._image_parameters(fine))
        with self.assertRaises(ValueError):backend._image_parameters(SimpleNamespace(**fields,image_quality='unknown'))


class LargePictureStabilizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg=APP/'bin'/'ffmpeg.exe';cls.ffprobe=APP/'bin'/'ffprobe.exe'
        if not cls.ffmpeg.is_file():raise unittest.SkipTest('Bundled FFmpeg unavailable')
        cls.temp=tempfile.TemporaryDirectory(prefix='four_k_motion_',dir=cache_dir('tests/performance581'))
        cls.folder=Path(cls.temp.name);cls.source=cls.folder/"4K'运动.mp4"
        random=np.random.default_rng(184)
        gray=np.repeat(np.repeat(random.integers(25,180,(45,80),dtype=np.uint8),4,0),4,1)
        base=np.repeat(gray[:,:,None],3,axis=2);base[82:92,210:280]=255
        yy,xx=np.mgrid[:180,:320];frames=[]
        for index in range(24):
            angle=.055*math.sin(index*2*math.pi/7)
            dx=4*math.sin(index*2*math.pi/5);dy=3*math.sin(index*2*math.pi/6)
            sx=np.rint((xx-159.5-dx)*math.cos(angle)+(yy-89.5-dy)*math.sin(angle)+159.5).astype(int)
            sy=np.rint(-(xx-159.5-dx)*math.sin(angle)+(yy-89.5-dy)*math.cos(angle)+89.5).astype(int)
            frame=np.zeros_like(base);valid=(sx>=0)&(sx<320)&(sy>=0)&(sy<180)
            frame[valid]=base[sy[valid],sx[valid]];frames.append(frame)
        cls.frames=np.asarray(frames)
        cls.command(['-y','-f','rawvideo','-pix_fmt','rgb24','-video_size','320x180','-framerate','24','-i','pipe:0',
            '-vf','scale=3840:2160:flags=neighbor','-c:v','hevc_nvenc','-preset','p1','-cq','16','-bf','0','-an',str(cls.source)],cls.frames.tobytes())

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls,'temp'):cls.temp.cleanup()
    @classmethod
    def command(cls,args,data=None):
        result=subprocess.run([str(cls.ffmpeg),'-hide_banner','-loglevel','error',*args],input=data,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90,creationflags=CREATE_NO_WINDOW)
        if result.returncode:raise AssertionError(result.stderr.decode('utf-8','replace'))
        return result.stdout

    @staticmethod
    def angular_deviation(frames):
        angles=[]
        for frame in frames:
            y,x=np.where(frame.min(axis=2)>235)
            centered=np.stack((x-x.mean(),y-y.mean()))
            _,vectors=np.linalg.eigh(centered@centered.T)
            vx,vy=vectors[:,-1];angle=math.atan2(vy,vx)
            if angle>math.pi/2:angle-=math.pi
            if angle<-math.pi/2:angle+=math.pi
            angles.append(angle)
        return float(np.std(angles))

    def render(self,name,force_cpu=False,quality='fine'):
        stages=[];logs=[];run=backend._run
        def timed(command,*args,**kwargs):
            if force_cpu and '-hwaccel' in command:raise RuntimeError('Forced unsupported hardware decoder')
            begin=time.perf_counter()
            try:return run(command,*args,**kwargs)
            finally:stages.append({'phase':'analysis' if any('vidstabdetect=' in value for value in command) else 'native_transform',
                                   'seconds':time.perf_counter()-begin,'hardware_decode':'-hwaccel' in command,
                                   'interpolation':'bilinear' if any('interpol=bilinear' in value for value in command) else 'bicubic'})
        with patch.object(backend,'_run',side_effect=timed):
            result=backend.run_image_stabilization(self.source,0,1,
                StabilizationSettings(engine='image',image_zoom=False,image_model='similarity',image_quality=quality),self.folder/name,
                ffmpeg_path=str(self.ffmpeg),ffprobe_path=str(self.ffprobe),log=logs.append)
        raw=self.command(['-i',result.cache_path,'-vf','scale=320:180:flags=area','-an','-pix_fmt','rgb24','-f','rawvideo','pipe:1'])
        frames=np.frombuffer(raw,dtype=np.uint8).reshape(-1,180,320,3)
        self.assertEqual((result.width,result.height,result.fps),(3840,2160,24.))
        self.assertEqual(len(frames),24)
        before=self.angular_deviation(self.frames);after=self.angular_deviation(frames)
        self.assertLess(after,before*.55)
        return {'successful':True,'force_cpu_decode':force_cpu,'image_quality':quality,'source_angle_std':before,'stabilized_angle_std':after,
                'resolution':[result.width,result.height],'frames':len(frames),'stages':stages,'logs':logs}

    def test_reduced_analysis_restores_real_rotation_in_native_four_k_picture(self):
        report=self.render('native_4k.mp4')
        (APP.parent/'image_stabilization_four_k_581.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')

    def test_unavailable_gpu_decoder_uses_reduced_cpu_analysis_without_changing_output_size(self):
        report=self.render('cpu_fallback_4k.mp4',force_cpu=True,quality='fast')
        self.assertTrue(any('改用 CPU' in value for value in report['logs']))
        self.assertFalse(any(stage['hardware_decode'] for stage in report['stages']))
        self.assertTrue(all(stage['interpolation']=='bilinear' for stage in report['stages'] if stage['phase']=='native_transform'))
        self.assertTrue(any('双线性' in value for value in report['logs']))
        (APP.parent/'image_stabilization_four_k_fast_581.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')


if __name__=='__main__':unittest.main()
