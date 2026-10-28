"""Real vid.stab motion reduction, range alignment, metadata and cancel safety."""
from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
import image_stabilization as backend
from exporter_core import CREATE_NO_WINDOW,ExportCancelled,probe_media
from gyroflow_core import StabilizationSettings,cache_matches,StabilizedRange


class PictureStabilizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg=APP/'bin'/'ffmpeg.exe';cls.ffprobe=APP/'bin'/'ffprobe.exe'
        if not cls.ffmpeg.is_file():raise unittest.SkipTest('Bundled FFmpeg unavailable')
        cls.temp=tempfile.TemporaryDirectory(prefix='image_stabilize_')
        cls.folder=Path(cls.temp.name)/"中文'运动素材";cls.folder.mkdir()
        cls.source=cls.folder/"摄像机'片段.mkv"
        random=np.random.default_rng(721)
        canvas=np.repeat(np.repeat(random.integers(30,210,(60,100),dtype=np.uint8),4,0),4,1)
        cls.frames=[]
        for index in range(72):
            x=40+round(7*math.sin(index*2*math.pi/5));y=30+round(5*math.sin(index*2*math.pi/7))
            frame=np.repeat(canvas[y:y+180,x:x+320,None],3,axis=2)
            frame[68:112,138:182]=[30+3*index,40,100]
            cls.frames.append(frame)
        cls.raw=np.asarray(cls.frames,dtype=np.uint8)
        cls.command(['-y','-f','rawvideo','-pix_fmt','rgb24','-video_size','320x180','-framerate','24',
            '-i','pipe:0','-c:v','ffv1','-pix_fmt','bgr0',str(cls.source)],cls.raw.tobytes())

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls,'temp'):cls.temp.cleanup()

    @classmethod
    def command(cls,arguments,data=None):
        result=subprocess.run([str(cls.ffmpeg),'-hide_banner','-loglevel','error',*arguments],input=data,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=CREATE_NO_WINDOW,timeout=60)
        if result.returncode:raise AssertionError(result.stderr.decode('utf-8','replace'))
        return result.stdout

    def settings(self,**kwargs):
        return StabilizationSettings(engine='image',image_smoothing=.5,image_zoom=False,**kwargs)

    def render(self,settings=None,name="成片'防抖.mp4",**kwargs):
        return backend.run_image_stabilization(self.source,.52,2.47,settings or self.settings(),self.folder/name,
            ffmpeg_path=str(self.ffmpeg),ffprobe_path=str(self.ffprobe),**kwargs)

    def decode(self,path):
        raw=self.command(['-i',str(path),'-an','-pix_fmt','rgb24','-f','rawvideo','pipe:1'])
        return np.frombuffer(raw,dtype=np.uint8).reshape(-1,180,320,3)

    @staticmethod
    def motion_error(frames):
        images=frames[:,24:-24,24:-24].astype(float).mean(axis=3)
        window=np.outer(np.hanning(images.shape[1]),np.hanning(images.shape[2]))
        shifts=[]
        previous=None
        for image in images:
            image=(image-image.mean())*window
            spectrum=np.fft.fft2(image)
            if previous is not None:
                cross=spectrum*previous.conj();cross/=np.maximum(abs(cross),1e-8)
                y,x=np.unravel_index(np.argmax(abs(np.fft.ifft2(cross))),cross.shape)
                if y>cross.shape[0]//2:y-=cross.shape[0]
                if x>cross.shape[1]//2:x-=cross.shape[1]
                shifts.append(x*x+y*y)
            previous=spectrum
        return float(np.mean(shifts))

    def test_real_motion_reduction_frame_alignment_cfr_and_chinese_apostrophe_paths(self):
        log=[];progress=[]
        result=self.render(log=log.append,progress=lambda *args:progress.append(args))
        frames=self.decode(result.cache_path)
        self.assertEqual(frames.shape[0],48)
        self.assertEqual((result.width,result.height,result.fps),(320,180,24.))
        self.assertAlmostEqual(result.source_start,.5);self.assertAlmostEqual(result.source_end,2.5)
        self.assertAlmostEqual(result.duration,2.)
        self.assertAlmostEqual(float(np.median(frames[0,84:96,154:166,0])),66,delta=3)
        self.assertAlmostEqual(float(np.median(frames[-1,84:96,154:166,0])),207,delta=3)
        before=self.motion_error(self.raw[12:60]);after=self.motion_error(frames)
        self.assertGreater(before,5)
        self.assertLess(after,before*.55)
        self.assertFalse(result.probe.has_gyro);self.assertFalse(result.probe.has_lens)
        self.assertIn('vid.stab',result.probe.source_name)
        self.assertEqual(progress[-1][0],100.)
        self.assertEqual(sorted(value for value,_ in progress),[value for value,_ in progress])
        record=StabilizedRange(.5,2.5,1,'source',self.settings(),result.cache_path,result.signature,.5,2.5)
        self.assertTrue(cache_matches(record,self.source))
        report={'success':True,'range_frames':48,'source_start':result.source_start,'source_end':result.source_end,
                'source_motion_mse':before,'stabilized_motion_mse':after,'encoder_log':log,
                'cache_frame_format':probe_media(str(self.ffprobe),result.cache_path).pixel_format}
        (APP.parent/'image_stabilization_validation.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')

    def test_signature_uses_image_controls_and_source_but_ignores_gyro_sidecars(self):
        first=self.settings()
        signature=backend.image_cache_signature(self.source,.5,2.5,first)
        unrelated=dataclasses.replace(first,gyro_path="unrelated'gyro.gcsv",lens_path='missing.json')
        self.assertEqual(signature,backend.image_cache_signature(self.source,.5,2.5,unrelated))
        for name,value in (('image_smoothing',1.),('image_shakiness',8),('image_zoom',True),
                           ('image_tripod',True),('image_model','translation')):
            self.assertNotEqual(signature,backend.image_cache_signature(self.source,.5,2.5,dataclasses.replace(first,**{name:value})))
        self.assertNotEqual(signature,backend.image_cache_signature(self.source,0,2.5,first))
        self.assertEqual(len(signature),64)

    def test_cancel_during_analysis_preserves_previous_output_and_removes_temporary_files(self):
        target=self.folder/'previous.mp4';target.write_bytes(b'previous-cache')
        event=threading.Event()
        def progress(value,*args):
            if value>1:event.set()
        with self.assertRaises(ExportCancelled):
            self.render(name=target.name,cancel_event=event,progress=progress)
        self.assertEqual(target.read_bytes(),b'previous-cache')
        self.assertEqual(list(self.folder.glob('okk_image_stab_*')),[])

    def test_failed_analysis_and_precancel_do_not_replace_existing_output(self):
        target=self.folder/'failed.mp4';target.write_bytes(b'old-cache')
        with patch.object(backend,'_run',side_effect=RuntimeError('Analysis failed')):
            with self.assertRaisesRegex(RuntimeError,'Analysis failed'):self.render(name=target.name)
        self.assertEqual(target.read_bytes(),b'old-cache')
        self.assertEqual(list(self.folder.glob('okk_image_stab_*')),[])
        event=threading.Event();event.set()
        with self.assertRaises(ExportCancelled):self.render(name=target.name,cancel_event=event)
        with self.assertRaisesRegex(ValueError,'覆盖原片'):
            backend.run_image_stabilization(self.source,0,1,self.settings(),self.source,
                ffmpeg_path=str(self.ffmpeg),ffprobe_path=str(self.ffprobe))

    def test_nvenc_failure_falls_back_to_real_cpu_encoding(self):
        actual_run=backend._run;logs=[]
        def force_cpu(command,*args,**kwargs):
            if 'hevc_nvenc' in command:raise RuntimeError('NVENC forced unavailable for fallback verification')
            return actual_run(command,*args,**kwargs)
        with patch.object(backend,'_run',side_effect=force_cpu):
            result=self.render(name='cpu_fallback.mp4',log=logs.append)
        self.assertEqual(probe_media(str(self.ffprobe),result.cache_path).video_codec,'h264')
        self.assertTrue(any('CPU x264' in line for line in logs))
        self.assertEqual(len(self.decode(result.cache_path)),48)

    def test_ten_bit_cache_and_source_colour_tags_are_retained_with_honest_warning(self):
        source=self.folder/'ten_bit.mp4'
        self.command(['-y','-i',str(self.source),'-vf',
            'format=yuv420p10le,setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:range=limited','-c:v','libx265',
            '-preset','ultrafast','-crf','12','-x265-params','pools=4:frame-threads=2:log-level=error',
            '-color_primaries','bt2020','-color_trc','smpte2084','-colorspace','bt2020nc','-color_range','tv',str(source)])
        logs=[]
        result=backend.run_image_stabilization(source,.5,1.,self.settings(),self.folder/'ten_bit_cache.mp4',
            ffmpeg_path=str(self.ffmpeg),ffprobe_path=str(self.ffprobe),log=logs.append)
        media=probe_media(str(self.ffprobe),result.cache_path)
        self.assertGreaterEqual(media.bit_depth,10)
        self.assertEqual((media.color_primaries,media.color_transfer,media.color_space,media.color_range),
                         ('bt2020','smpte2084','bt2020nc','tv'))
        self.assertFalse(media.has_audio)
        self.assertTrue(any('8bit' in value and 'HDR' in value for value in logs))

    def test_fractional_cfr_full_range_levels_and_tags(self):
        source=self.folder/'full_range.mkv'
        frame=self.raw[0].copy()
        frame[20:52,30:94]=8;frame[20:52,220:284]=247
        self.command(['-y','-f','rawvideo','-pix_fmt','rgb24','-video_size','320x180',
            '-framerate','30000/1001','-i','pipe:0','-c:v','mjpeg','-q:v','2','-pix_fmt','yuvj420p',
            '-color_range','pc',str(source)],frame.tobytes()*18)
        result=backend.run_image_stabilization(source,0,.59,self.settings(),self.folder/'full_range.mp4',
            ffmpeg_path=str(self.ffmpeg),ffprobe_path=str(self.ffprobe))
        self.assertAlmostEqual(result.fps,30000/1001,places=8)
        self.assertAlmostEqual(result.duration,18*1001/30000,delta=.001)
        before=self.decode(source)[0];after=self.decode(result.cache_path)[0]
        for roi in ((slice(27,44),slice(40,84)),(slice(27,44),slice(230,274))):
            self.assertAlmostEqual(float(np.median(after[roi])),float(np.median(before[roi])),delta=4)
        self.assertEqual(probe_media(str(self.ffprobe),result.cache_path).color_range,'pc')

    def test_rotation_mode_and_tripod_lock_have_actual_picture_effects(self):
        random=np.random.default_rng(41)
        gray=np.repeat(np.repeat(random.integers(25,180,(60,130),dtype=np.uint8),4,0),4,1)
        base=np.repeat(gray[30:210,70:390,None],3,axis=2)
        base[82:92,210:280]=255
        yy,xx=np.mgrid[:180,:320];rotated=[]
        for index in range(48):
            angle=.065*math.sin(index*2*math.pi/7)
            sx=np.rint((xx-159.5)*math.cos(angle)+(yy-89.5)*math.sin(angle)+159.5).astype(int)
            sy=np.rint(-(xx-159.5)*math.sin(angle)+(yy-89.5)*math.cos(angle)+89.5).astype(int)
            frame=np.zeros_like(base);valid=(sx>=0)&(sx<320)&(sy>=0)&(sy<180)
            frame[valid]=base[sy[valid],sx[valid]];rotated.append(frame)
        rotation=self.folder/'rotation.mkv'
        self.command(['-y','-f','rawvideo','-pix_fmt','rgb24','-video_size','320x180','-framerate','24',
            '-i','pipe:0','-c:v','ffv1','-pix_fmt','bgr0',str(rotation)],np.asarray(rotated).tobytes())
        def stabilize(source,settings,name):
            result=backend.run_image_stabilization(source,0,2,settings,self.folder/name,
                ffmpeg_path=str(self.ffmpeg),ffprobe_path=str(self.ffprobe))
            return self.decode(result.cache_path)
        def angular_deviation(frames):
            angles=[]
            for frame in frames:
                y,x=np.where(frame.min(axis=2)>235)
                centered=np.stack((x-x.mean(),y-y.mean()))
                values,vectors=np.linalg.eigh(centered@centered.T)
                vx,vy=vectors[:,-1]
                angle=math.atan2(vy,vx)
                if angle>math.pi/2:angle-=math.pi
                if angle<-math.pi/2:angle+=math.pi
                angles.append(angle)
            return float(np.std(angles))
        translation=stabilize(rotation,self.settings(image_model='translation'),'translation.mp4')
        similarity=stabilize(rotation,self.settings(image_model='similarity'),'similarity.mp4')
        before=angular_deviation(rotated);plain=angular_deviation(translation);corrected=angular_deviation(similarity)
        self.assertGreater(plain,before*.65)
        self.assertLess(corrected,plain*.5)
        panning=[]
        for index in range(48):
            x=40+round(index*.4)
            panning.append(np.repeat(gray[30:210,x:x+320,None],3,axis=2))
        pan=self.folder/'pan.mkv'
        self.command(['-y','-f','rawvideo','-pix_fmt','rgb24','-video_size','320x180','-framerate','24',
            '-i','pipe:0','-c:v','ffv1','-pix_fmt','bgr0',str(pan)],np.asarray(panning).tobytes())
        normal=stabilize(pan,self.settings(),'normal_pan.mp4')
        locked=stabilize(pan,self.settings(image_tripod=True),'tripod_pan.mp4')
        def end_displacement(frames):
            return self.motion_error(np.stack((frames[0],frames[-1])))
        self.assertGreater(end_displacement(normal),50)
        self.assertLess(end_displacement(locked),end_displacement(normal)*.15)

    def test_textureless_video_does_not_report_successful_motion_tracking(self):
        source=self.folder/'black.mkv'
        self.command(['-y','-f','lavfi','-i','color=black:size=320x180:rate=24:duration=1',
            '-c:v','ffv1',str(source)])
        with self.assertRaisesRegex(ValueError,'纹理'):
            backend.run_image_stabilization(source,0,.5,self.settings(),self.folder/'black_cache.mp4',
                ffmpeg_path=str(self.ffmpeg),ffprobe_path=str(self.ffprobe))
        self.assertFalse((self.folder/'black_cache.mp4').exists())


if __name__=='__main__':unittest.main()
