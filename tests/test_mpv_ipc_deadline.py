"""Timeouts must bound native pipe reads and never accept an earlier reply."""
from __future__ import annotations
import json
import os
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from mpv_controller import MpvController
try:
    from .test_mpv_controller import _FakePipe,_RunningProcess
except ImportError:
    from test_mpv_controller import _FakePipe,_RunningProcess


class IpcDeadlineTests(unittest.TestCase):
    def controller(self):
        c=MpvController('mpv.exe',123);c.process=_RunningProcess();return c

    def test_late_response_cannot_satisfy_a_different_request(self):
        c=self.controller()
        pipe=_FakePipe([b'{"error":"success","data":"stale","request_id":999}\n',
                        b'{"event":"file-loaded"}\n',b'{"error":"success","data":"fresh"}\n'])
        c._pipe=pipe
        self.assertEqual(c.command(['get_property','path']),'fresh')
        self.assertEqual(c._event_counts['file-loaded'],1)
        self.assertEqual(json.loads(pipe.writes[0])['request_id'],1)

    def test_empty_pipe_times_out_without_a_blocking_read(self):
        c=self.controller();pipe=_FakePipe([]);c._pipe=pipe
        began=time.monotonic()
        with patch.object(c,'_available_bytes',return_value=0),patch.object(pipe,'readline') as readline:
            with self.assertRaisesRegex(RuntimeError,'超时'):c.command(['get_property','time-pos'],timeout=.035)
            readline.assert_not_called()
        self.assertLess(time.monotonic()-began,.3)
        self.assertIsNone(c._pipe)

    def test_gpu_probe_waits_for_new_file_generation(self):
        c=self.controller();paths=[];queries=[]
        def command(args,**kwargs):
            queries.append(args[1])
            if args[1]=='path':
                paths.append(1)
                if len(paths)==2:c._event_counts['file-loaded']=4
                return 'camera-new.mp4'
            self.assertEqual(c._event_counts.get('file-loaded',0),4)
            return {'hwdec-current':'d3d11va','hwdec-interop':'d3d11va','video-params/hw-pixelformat':'p010'}[args[1]]
        with patch.object(c,'command',side_effect=command):
            result=c._wait_for_gpu_decoder(.5,expected_path='camera-new.mp4',after_generation=3)
        self.assertEqual(result['hwdec'],'d3d11va')
        self.assertEqual(queries[:3],['path','path','hwdec-current'])

    @unittest.skipUnless(os.name=='nt','Windows named pipes')
    def test_real_nonresponding_windows_pipe_is_bounded(self):
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateNamedPipeW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,
                                         wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID]
        kernel.CreateNamedPipeW.restype=wintypes.HANDLE
        kernel.ConnectNamedPipe.argtypes=[wintypes.HANDLE,wintypes.LPVOID]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.DisconnectNamedPipe.argtypes=[wintypes.HANDLE]
        name=rf'\\.\pipe\okk_test_deadline_{uuid.uuid4().hex}'
        handle=kernel.CreateNamedPipeW(name,3,0,1,4096,4096,0,None)
        self.assertNotEqual(handle,wintypes.HANDLE(-1).value)
        release=threading.Event()
        def server():
            kernel.ConnectNamedPipe(handle,None)
            release.wait(2)
            kernel.DisconnectNamedPipe(handle);kernel.CloseHandle(handle)
        worker=threading.Thread(target=server);worker.start()
        c=self.controller();c.pipe_name=name
        began=time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError,'超时'):c.command(['get_property','time-pos'],timeout=.08)
            self.assertLess(time.monotonic()-began,.5)
            self.assertIsNone(c._pipe)
        finally:
            c._close_pipe();release.set();worker.join(2)
        self.assertFalse(worker.is_alive())


if __name__=='__main__':unittest.main()
