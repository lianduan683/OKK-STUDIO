"""CPU compatibility is explicit and must not accept a stale load or empty frame."""
from __future__ import annotations
import unittest
from unittest.mock import patch
from mpv_controller import MpvController, MPV_GPU_PLAYBACK_OPTIONS

class RunningProcess:
    def poll(self): return None

class SoftwareFallbackTests(unittest.TestCase):
    def controller(self,allowed=True): return MpvController('mpv.exe',123,allow_software_decode=allowed)
    def test_default_start_stays_strict_and_opt_in_only_changes_fallback_option(self):
        launches=[]
        for allowed in (False,True):
            controller=self.controller(allowed)
            with patch('mpv_controller.subprocess.Popen',return_value=RunningProcess()) as popen:
                with patch.object(controller,'command',return_value='ready'):controller.start()
            launches.append(popen.call_args.args[0])
        self.assertIn('--hwdec-software-fallback=no',launches[0])
        self.assertIn('--hwdec-software-fallback=yes',launches[1])
        self.assertNotIn('--hwdec-software-fallback=no',launches[1])
        for args in launches:
            self.assertIn('--hwdec=d3d11va,nvdec',args)
            self.assertIn('--vo=gpu-next',args)
        self.assertIn('--hwdec-software-fallback=no',MPV_GPU_PLAYBACK_OPTIONS)
    def test_new_matching_cpu_file_with_video_output_reports_cpu_explicitly(self):
        controller=self.controller();controller._event_counts['file-loaded']=3
        def command(args,**kwargs):
            return {'path':'new.mp4','hwdec-current':'no','video-out-params':{'w':7680,'h':4320}}[args[1]]
        with patch.object(controller,'command',side_effect=command):
            status=controller._wait_for_gpu_decoder(.2,expected_path='new.mp4',after_generation=2)
        self.assertEqual(status,{'hwdec':'no','renderer':'gpu-next/d3d11','decode':'cpu'})
    def test_cpu_does_not_satisfy_load_until_new_generation_even_when_path_matches(self):
        controller=self.controller();queries=[];paths=0
        def command(args,**kwargs):
            nonlocal paths
            queries.append(args[1])
            if args[1]=='path':
                paths+=1
                if paths==2:controller._event_counts['file-loaded']=4
                return 'new.mp4'
            self.assertEqual(controller._event_counts.get('file-loaded'),4)
            return {'hwdec-current':'no','video-out-params':{'w':1920,'h':1080}}[args[1]]
        with patch.object(controller,'command',side_effect=command):
            controller._wait_for_gpu_decoder(.3,expected_path='new.mp4',after_generation=3)
        self.assertEqual(queries[:3],['path','path','hwdec-current'])
    def test_cpu_does_not_satisfy_new_generation_with_previous_path(self):
        controller=self.controller();controller._event_counts['file-loaded']=4;paths=0;queries=[]
        def command(args,**kwargs):
            nonlocal paths
            queries.append(args[1])
            if args[1]=='path':
                paths+=1;return 'old.mp4' if paths==1 else 'new.mp4'
            self.assertGreaterEqual(paths,2)
            return {'hwdec-current':'no','video-out-params':{'w':1920,'h':1080}}[args[1]]
        with patch.object(controller,'command',side_effect=command):
            controller._wait_for_gpu_decoder(.3,expected_path='new.mp4',after_generation=3)
        self.assertEqual(queries[:3],['path','path','hwdec-current'])
    def test_cpu_waits_for_finite_positive_video_output_dimensions(self):
        for invalid in (None,{}, {'w':0,'h':1080},{'w':1920,'h':None},{'w':float('inf'),'h':1080}):
            with self.subTest(output=invalid):
                controller=self.controller();controller._event_counts['file-loaded']=1;outputs=0
                def command(args,**kwargs):
                    nonlocal outputs
                    if args[1]=='path':return 'new.mp4'
                    if args[1]=='hwdec-current':return 'no'
                    outputs+=1;return invalid if outputs==1 else {'w':1920,'h':1080}
                with patch.object(controller,'command',side_effect=command):
                    result=controller._wait_for_gpu_decoder(.4,expected_path='new.mp4',after_generation=0)
                self.assertEqual(result['decode'],'cpu');self.assertEqual(outputs,2)
    def test_default_still_rejects_cpu_on_fresh_valid_file(self):
        controller=self.controller(False);controller._event_counts['file-loaded']=1
        def command(args,**kwargs):return {'path':'new.mp4','hwdec-current':'no'}[args[1]]
        with patch.object(controller,'command',side_effect=command):
            with self.assertRaisesRegex(RuntimeError,'回退到 CPU'):
                controller._wait_for_gpu_decoder(.015,expected_path='new.mp4',after_generation=0)
    def test_opt_in_without_matching_new_load_barrier_never_accepts_cpu(self):
        controller=self.controller()
        with patch.object(controller,'command',return_value='no'):
            with self.assertRaisesRegex(RuntimeError,'回退到 CPU'):controller._wait_for_gpu_decoder(.015)
    def test_opt_in_keeps_hardware_decode_when_available(self):
        controller=self.controller();controller._event_counts['file-loaded']=2
        with patch.object(controller,'command',side_effect=['new.mp4','d3d11va','d3d11va','p010']):
            result=controller._wait_for_gpu_decoder(.2,expected_path='new.mp4',after_generation=1)
        self.assertEqual(result['hwdec'],'d3d11va');self.assertNotIn('decode',result)

if __name__=='__main__':unittest.main(verbosity=2)
