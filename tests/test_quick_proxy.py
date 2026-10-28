"""1080p source-clock proxies: cache, fallback, cancellation and real encodes."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import quick_proxy as module
from exporter_core import Capabilities, ExportCancelled, MediaInfo, detect_capabilities, find_binary, probe_media

ROOT=Path(__file__).resolve().parents[1]


class ProxyCommandTests(unittest.TestCase):
    def test_gop_is_at_most_one_second_and_cpu_is_bounded(self):
        media=MediaInfo('camera.mov',duration=5,width=3840,height=2160,fps=30000/1001,bit_depth=10)
        cpu=module._build_command('ffmpeg',media.path,'out.mp4',media,'cpu')
        self.assertEqual(cpu[cpu.index('-g')+1],'29')
        self.assertEqual(cpu[cpu.index('-bf')+1],'0')
        self.assertEqual([cpu[index+1] for index,value in enumerate(cpu) if value=='-threads'],['2','2'])
        self.assertEqual(cpu[cpu.index('-filter_threads')+1],'2')
        self.assertIn('reset_sar=1',cpu[cpu.index('-vf')+1])

    def test_fps_cap_does_not_speed_up_the_source(self):
        media=MediaInfo('camera.mov',duration=12,width=3840,height=2160,fps=120,bit_depth=10)
        command=module._build_command('ffmpeg',media.path,'out.mp4',media,'cuda_nvenc')
        graph=command[command.index('-vf')+1]
        self.assertIn('setpts=PTS-STARTPTS,fps=fps=30/1:start_time=0',graph)
        self.assertIn('scale_cuda=',graph)
        self.assertEqual(command[command.index('-t')+1],'12.000000000')
        self.assertEqual(command[command.index('-preset')+1],'p1')
        self.assertNotIn('hwdownload',graph)

    def test_source_path_stat_and_parameter_version_are_all_part_of_key(self):
        stamp={'path':'C:/one/camera.mov','size':100,'mtime_ns':123}
        first=module._cache_key(stamp)
        for changed in ({**stamp,'path':'C:/two/camera.mov'},{**stamp,'size':101},{**stamp,'mtime_ns':124}):
            self.assertNotEqual(first,module._cache_key(changed))
        with patch.dict(module.PARAMETERS,version=999):
            self.assertNotEqual(first,module._cache_key(stamp))

    def test_old_720p_cache_has_a_distinct_key_and_folder(self):
        stamp={'path':'C:/one/camera.mov','size':100,'mtime_ns':123}
        current=module._cache_key(stamp)
        self.assertEqual(module.PARAMETERS['bounds'],[1920,1080])
        with patch.dict(module.PARAMETERS,version=1,bounds=[1280,720]):
            self.assertNotEqual(current,module._cache_key(stamp))

    def test_precancellation_does_not_probe_or_create_cache(self):
        cancel=threading.Event();cancel.set()
        with patch.object(module,'_probe_media_cancel') as probe,patch.object(module,'cache_dir') as cache:
            with self.assertRaises(ExportCancelled):
                module.prepare_1080p_proxy('unused','ffmpeg','ffprobe',Capabilities(),cancel_event=cancel)
        probe.assert_not_called();cache.assert_not_called()

    def test_cancellation_terminates_and_reaps_a_blocked_probe(self):
        cancel=threading.Event();started=threading.Event();children=[]
        original=subprocess.Popen
        def spawn(*args,**kwargs):
            child=original(*args,**kwargs);children.append(child);started.set();return child
        def stop_when_started():
            if started.wait(3):cancel.set()
        interrupter=threading.Thread(target=stop_when_started);interrupter.start()
        before=time.monotonic()
        try:
            with patch.object(module,'_media_probe_command',return_value=[sys.executable,'-c','import time; time.sleep(30)']), \
                    patch.object(module.subprocess,'Popen',side_effect=spawn):
                with self.assertRaises(ExportCancelled):
                    module._probe_media_cancel('blocked-ffprobe','camera.mov',cancel_event=cancel)
        finally:
            interrupter.join(3)
            for child in children:
                if child.poll() is None:child.kill();child.wait(3)
        self.assertEqual(len(children),1)
        self.assertIsNotNone(children[0].returncode)
        self.assertLess(time.monotonic()-before,3)

    def test_validation_does_not_wrap_cancellation_or_retry_encoding(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'work') as temporary:
            source=Path(temporary)/'source.mov';source.write_bytes(b'source')
            proxy=Path(temporary)/'proxy.mp4';proxy.write_bytes(b'proxy')
            media=MediaInfo(str(source),duration=2,width=1280,height=720,fps=30)
            with patch.object(module,'_probe_media_cancel',side_effect=ExportCancelled('cancel')):
                with self.assertRaises(ExportCancelled):module._validate('ffprobe',str(source),proxy,media)

    def test_cancelled_cache_probe_does_not_start_a_replacement_encode(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'work') as temporary:
            folder=Path(temporary);source=folder/'source.mov';source.write_bytes(b'source')
            output=folder/(module._cache_key(module._source_stamp(source))+'.mp4');output.write_bytes(b'old cache')
            media=MediaInfo(str(source),duration=2,width=1280,height=720,fps=30)
            with patch.object(module,'cache_dir',return_value=folder), \
                    patch.object(module,'_probe_media_cancel',side_effect=[media,ExportCancelled('cancel')]), \
                    patch.object(module,'_run_encode') as encode:
                with self.assertRaises(ExportCancelled):
                    module.prepare_1080p_proxy(source,'ffmpeg','ffprobe',Capabilities())
            encode.assert_not_called();self.assertEqual(output.read_bytes(),b'old cache')


class RealQuickProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg=find_binary('ffmpeg',ROOT);cls.ffprobe=find_binary('ffprobe',ROOT)
        if not cls.ffmpeg or not cls.ffprobe:raise unittest.SkipTest('FFmpeg unavailable')
        cls.folder=tempfile.TemporaryDirectory(prefix='quick_proxy_tests_',dir=ROOT/'work')
        cls.root=Path(cls.folder.name);cls.caps=detect_capabilities(ROOT)
        cls.source=cls.root/'landscape.mov';cls.portrait=cls.root/'portrait.mov'
        for path,size,rate in ((cls.source,'3840x2160',60),(cls.portrait,'1080x1920',60)):
            result=subprocess.run([cls.ffmpeg,'-hide_banner','-v','error','-y',
                '-f','lavfi','-i',f'testsrc2=size={size}:rate={rate}:duration=2',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=2',
                '-c:v','libx264','-preset','ultrafast','-crf','25','-threads','2',
                '-pix_fmt','yuv420p10le','-c:a','aac','-shortest',str(path)],capture_output=True,timeout=90)
            if result.returncode:raise RuntimeError(result.stderr.decode('utf-8','replace')[-2000:])

    @classmethod
    def tearDownClass(cls):cls.folder.cleanup()

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(dir=self.root)
        self.cache=Path(self.temporary.name)
        self.cache_patch=patch.object(module,'cache_dir',return_value=self.cache);self.cache_patch.start()
        self.register_patch=patch.object(module,'register_proxy',return_value=True);self.register=self.register_patch.start()

    def tearDown(self):
        self.register_patch.stop();self.cache_patch.stop();self.temporary.cleanup()

    def verify(self,path,source):
        original=probe_media(self.ffprobe,str(source));proxy=probe_media(self.ffprobe,str(path))
        self.assertEqual(proxy.video_codec,'h264');self.assertEqual(proxy.bit_depth,8)
        self.assertEqual(proxy.fps,30);self.assertTrue(proxy.has_audio)
        self.assertLessEqual(proxy.width,1920);self.assertLessEqual(proxy.height,1080)
        self.assertAlmostEqual(proxy.duration,original.duration,delta=.04)
        self.assertAlmostEqual(proxy.width/proxy.height,original.width/original.height,delta=.006)
        raw=subprocess.run([self.ffprobe,'-v','error','-count_frames','-select_streams','v:0',
            '-show_entries','stream=nb_read_frames,has_b_frames','-of','json',str(path)],capture_output=True,check=True,timeout=30)
        info=json.loads(raw.stdout)['streams'][0]
        self.assertEqual(int(info['nb_read_frames']),60);self.assertEqual(info['has_b_frames'],0)
        return proxy

    def test_real_4k_ten_bit_to_gpu_1080p_and_exact_cache_reuse(self):
        if 'h264_nvenc' not in self.caps.nvenc_codecs:self.skipTest('NVENC unavailable')
        progress=[]
        result=module.prepare_1080p_proxy(self.source,self.ffmpeg,self.ffprobe,self.caps,
            progress=lambda percent,phase:progress.append((percent,phase)))
        proxy=self.verify(result,self.source)
        self.assertEqual((proxy.width,proxy.height),(1920,1080))
        self.assertEqual(progress[-1][0],100)
        self.assertTrue(any(0<value<99 for value,_ in progress))
        self.assertTrue(any('NVENC' in phase for _,phase in progress))
        self.register.assert_called_with(os.path.normcase(str(self.source.resolve())),result)
        # Index registration is best-effort; the UI can still attach the
        # returned, fully validated path explicitly when its index is unwritable.
        self.register.return_value=False
        with patch.object(module,'_run_encode',side_effect=AssertionError('cache must not re-encode')):
            again=module.prepare_1080p_proxy(self.source,self.ffmpeg,self.ffprobe,self.caps)
        self.assertEqual(again,result)

    def test_real_portrait_cpu_proxy_preserves_aspect_and_seconds(self):
        result=module.prepare_1080p_proxy(self.portrait,self.ffmpeg,self.ffprobe,Capabilities())
        proxy=self.verify(result,self.portrait)
        self.assertLess(proxy.width,proxy.height)

    def test_nvenc_failure_falls_back_to_real_two_thread_cpu_encode(self):
        original=module._run_encode;commands=[]
        def run(command,*args,**kwargs):
            commands.append(command)
            if command[command.index('-c:v')+1]=='h264_nvenc':raise module.ProxyEncodeError('simulated driver failure')
            return original(command,*args,**kwargs)
        with patch.object(module,'_run_encode',side_effect=run):
            result=module.prepare_1080p_proxy(self.source,self.ffmpeg,self.ffprobe,
                Capabilities(nvenc_codecs={'h264_nvenc'},cuda_decode=False))
        self.verify(result,self.source)
        self.assertEqual([cmd[cmd.index('-c:v')+1] for cmd in commands],['h264_nvenc','libx264'])
        self.assertEqual(commands[-1][commands[-1].index('-filter_threads')+1],'2')

    def test_cancel_running_ffmpeg_keeps_previous_cache_and_never_registers_partial(self):
        destination=self.cache/(module._cache_key(module._source_stamp(self.source))+'.mp4')
        destination.write_bytes(b'previous cache remains untouched')
        cancelled=threading.Event();percentages=[];builder=module._build_command
        def throttled(*args,**kwargs):
            command=builder(*args,**kwargs);command.insert(command.index('-i'),'-re');return command
        def progress(percent,phase):
            percentages.append(percent)
            if 0<percent<99:cancelled.set()
        with patch.object(module,'_build_command',side_effect=throttled):
            with self.assertRaises(ExportCancelled):
                module.prepare_1080p_proxy(self.source,self.ffmpeg,self.ffprobe,Capabilities(),cancelled,progress)
        self.assertTrue(any(0<value<99 for value in percentages))
        self.assertEqual(destination.read_bytes(),b'previous cache remains untouched')
        self.assertEqual(list(self.cache.glob('*.pending.mp4')),[])
        self.register.assert_not_called()

    def test_failed_encoder_keeps_previous_cache_and_cleans_temporary(self):
        destination=self.cache/(module._cache_key(module._source_stamp(self.source))+'.mp4')
        destination.write_bytes(b'old')
        with patch.object(module,'_run_encode',side_effect=module.ProxyEncodeError('all encoders unavailable')):
            with self.assertRaisesRegex(RuntimeError,'all encoders'):
                module.prepare_1080p_proxy(self.source,self.ffmpeg,self.ffprobe,self.caps)
        self.assertEqual(destination.read_bytes(),b'old')
        self.assertEqual(list(self.cache.glob('*.pending.mp4')),[])
        self.register.assert_not_called()

    def test_source_changed_during_render_is_not_published(self):
        source=self.cache/'source.mov';shutil.copyfile(self.source,source)
        initial=module._source_stamp(source);changed=False
        def progress(percent,phase):
            nonlocal changed
            if percent==99 and not changed:
                os.utime(source,ns=(initial['mtime_ns']+1000000,initial['mtime_ns']+1000000));changed=True
        with self.assertRaisesRegex(ValueError,'原片在生成期间发生变化'):
            module.prepare_1080p_proxy(source,self.ffmpeg,self.ffprobe,self.caps,progress=progress)
        self.assertFalse((self.cache/(module._cache_key(initial)+'.mp4')).exists())
        self.assertEqual(list(self.cache.glob('*.pending.mp4')),[])
        self.register.assert_not_called()


if __name__=='__main__':unittest.main()
