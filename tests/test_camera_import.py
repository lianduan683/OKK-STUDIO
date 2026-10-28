"""Batch import allocation and paged thumbnail decoder lifecycle."""
from __future__ import annotations

import hashlib
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import camera_import as module
from camera_import import assign_camera_ids,batch_import
from exporter_core import CREATE_NO_WINDOW,ExportCancelled,MediaInfo,find_binary
from multicam_core import CameraClip,Cut,MulticamProject
import multicam_player as player_module
from multicam_player import MulticamPlayer
try:
    from .test_multicam_player import _Trace,_controller_type
except ImportError:
    from test_multicam_player import _Trace,_controller_type


class BatchCameraImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='batch_cameras_')
        self.folder=Path(self.temp.name)
        self.sources=[]
        for index in range(20):
            path=self.folder/f'{index}.mp4';path.write_bytes(b'unchanged source '+bytes([index]))
            self.sources.append(str(path))

    def tearDown(self):self.temp.cleanup()

    @staticmethod
    def media(ffprobe,path):return MediaInfo(path=path,duration=2,width=160,height=90,fps=30)

    def test_twenty_files_are_twenty_new_ids_without_nine_camera_cap(self):
        used={1,3,10}
        expected=assign_camera_ids(used,20)
        original_sources=list(self.sources)
        hashes=[hashlib.sha256(Path(path).read_bytes()).digest() for path in self.sources]
        progress=[]
        with patch.object(module,'probe_media',side_effect=self.media) as probe:
            imported=batch_import('ffprobe.exe',self.sources,used,progress=lambda percent,text:progress.append(percent))
        self.assertEqual([camera for camera,_ in imported],expected)
        self.assertEqual([info.path for _,info in imported],self.sources)
        self.assertEqual(len(imported),20);self.assertGreater(imported[-1][0],9)
        self.assertEqual(probe.call_count,20)
        self.assertEqual(used,{1,3,10});self.assertEqual(self.sources,original_sources)
        self.assertEqual(hashes,[hashlib.sha256(Path(path).read_bytes()).digest() for path in self.sources])
        self.assertEqual(progress[-1],100);self.assertEqual(sorted(progress),progress)

    def test_lowest_unused_holes_and_validation(self):
        self.assertEqual(assign_camera_ids({2,4,7},6),[1,3,5,6,8,9])
        self.assertEqual(assign_camera_ids([],0),[])
        for existing,count in [([False],1),([0],1),([1.0],1),([],True),([],-1)]:
            with self.assertRaises(ValueError):assign_camera_ids(existing,count)

    def test_bad_file_returns_no_partial_batch_and_never_changes_inputs(self):
        calls=[]
        def probe(executable,path):
            calls.append(path)
            return self.media(executable,path) if len(calls)==1 else MediaInfo(path=path,duration=2)
        existing={4}
        with patch.object(module,'probe_media',side_effect=probe):
            with self.assertRaisesRegex(ValueError,'没有可读取的视频'):
                batch_import('ffprobe.exe',self.sources[:3],existing)
        self.assertEqual(len(calls),2);self.assertEqual(existing,{4})
        self.assertEqual(Path(self.sources[0]).read_bytes(),b'unchanged source '+bytes([0]))

    def test_cancelled_batch_does_not_return_partial_success(self):
        cancel=threading.Event()
        with patch.object(module,'probe_media',side_effect=self.media) as probe:
            with self.assertRaises(ExportCancelled):
                batch_import('ffprobe.exe',self.sources[:3],[],cancel_event=cancel,
                    progress=lambda percent,text:cancel.set() if percent>0 else None)
        self.assertEqual(probe.call_count,1)

    def test_empty_and_missing_selection(self):
        with patch.object(module,'probe_media') as probe:
            self.assertEqual(batch_import('',[],[]),[])
            with self.assertRaisesRegex(ValueError,'无法读取'):
                batch_import('ffprobe.exe',[self.folder/'missing.mp4'],[])
            probe.assert_not_called()

    def test_actual_video_probe_one_selected_file_one_camera(self):
        base=Path(__file__).resolve().parents[1]
        ffmpeg=find_binary('ffmpeg',base);ffprobe=find_binary('ffprobe',base)
        if not ffmpeg or not ffprobe:self.skipTest('FFmpeg unavailable')
        source=self.folder/'real.mp4'
        subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-f','lavfi','-i',
                        'color=red:size=160x90:rate=30:duration=0.2','-c:v','libx264','-pix_fmt','yuv420p',str(source)],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,creationflags=CREATE_NO_WINDOW)
        before=source.read_bytes()
        result=batch_import(ffprobe,[source,source],{1})
        self.assertEqual([camera for camera,_ in result],[2,3])
        self.assertTrue(all(info.width==160 and info.height==90 and info.duration>0 for _,info in result))
        self.assertEqual(source.read_bytes(),before)


class PagedCameraPlayerTests(unittest.TestCase):
    def setUp(self):
        self.trace=_Trace()
        fake=_controller_type(self.trace)
        self.patches=[patch.object(player_module,'MpvController',fake),
                      patch.object(player_module,'_AudioController',fake),patch.object(MulticamPlayer,'TICK',.01)]
        for item in self.patches:item.start()
        self.events=queue.Queue();self.received=[]
        self.player=MulticamPlayer('mpv.exe',{1:101,2:102},200,300,self.events)
        self.project=MulticamProject(clips=[CameraClip(f'c{i}',i,f'camera-{i}.mp4',0,20) for i in range(1,21)],
                                     cuts=[Cut(0,1)],master_audio='master.wav',master_duration=20)
        self.player.submit('configure',project=self.project)
        self.wait(lambda:self.player._program.active and all(slot.active for slot in self.player._slots.values()))

    def tearDown(self):
        self.trace.stop_gate.set();self.player.close()
        stopped=self.player.wait_closed(3)
        for item in reversed(self.patches):item.stop()
        self.assertTrue(stopped,'MPV worker leaked')

    def wait(self,predicate,timeout=3):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            while True:
                try:self.received.append(self.events.get_nowait())
                except queue.Empty:break
            if predicate():return
            time.sleep(.005)
        self.fail('Timed out; recent events='+str(self.received[-5:]))

    def test_page_change_releases_only_thumbnails_and_preserves_running_sound_picture(self):
        self.player.submit('play');self.wait(lambda:self.player._playing and self.player._position>.03)
        program=self.player._program.controller;audio=self.player._audio.controller
        start=self.player._position;before=len(self.trace.snapshot())
        self.player.submit('camera_windows',windows={10:1110,20:1120})
        self.wait(lambda:set(self.player._slots)=={10,20} and all(slot.active for slot in self.player._slots.values()))
        self.wait(lambda:self.player._position>start+.03)
        after=self.trace.snapshot()[before:]
        self.assertIs(self.player._program.controller,program);self.assertIs(self.player._audio.controller,audio)
        self.assertEqual({call[1] for call in after if call[0]=='dispose'},{101,102})
        protected=[call for call in after if call[1] in (200,300)]
        self.assertFalse([call for call in protected if call[0]=='load'])
        self.assertFalse([call for call in protected if call[0]=='command' and
                          (call[2][0] in ('loadfile','seek') or call[2][:2]==('set_property','pause'))])
        self.assertIn(('camera_windows_released',[101,102]),self.received)
        self.assertEqual(self.player._slots[20].controller.path,'camera-20.mp4')
        self.assertEqual(self.project.cuts,[Cut(0,1)])
        self.assertEqual(self.project.master_audio,'master.wav')

    def test_program_can_select_camera_twenty_outside_current_thumbnail_page(self):
        self.player.submit('audition',camera=20)
        self.wait(lambda:self.player._program.controller.path=='camera-20.mp4')
        self.assertEqual(set(self.player._slots),{1,2})
        self.assertEqual(self.player._camera_at(0),20)

    def test_unchanged_window_is_reused_and_empty_page_releases_it(self):
        original=self.player._slots[1].controller
        self.player.submit('camera_windows',windows={1:101,10:1110})
        self.wait(lambda:set(self.player._slots)=={1,10} and self.player._slots[10].active)
        self.assertIs(self.player._slots[1].controller,original)
        self.assertIn(('camera_windows_released',[102]),self.received)
        self.player.submit('camera_windows',windows={})
        self.wait(lambda:self.player._slots=={} and ('camera_windows_released',[101,1110]) in self.received)
        self.assertTrue(self.player._program.active);self.assertTrue(self.player._audio.active)
        self.assertIn(('camera_windows_released',[101,1110]),self.received)

    def test_page_mapping_validation_does_not_conflate_total_camera_count(self):
        self.assertEqual(MulticamPlayer._checked_camera_windows({100:700}),{100:700})
        for windows in ({True:1},{1:False},{1:3,2:3},{index:index+1 for index in range(1,38)}):
            with self.assertRaises(ValueError):MulticamPlayer._checked_camera_windows(windows)

    def test_rapid_pending_page_changes_skip_obsolete_decoders_and_release_exact_handles(self):
        self.trace.cleanup_started.clear();self.trace.stop_gate.clear()
        self.player.submit('camera_windows',windows={10:1110})
        self.assertTrue(self.trace.cleanup_started.wait(1))
        self.player.submit('camera_windows',windows={11:1111})
        self.player.submit('camera_windows',windows={20:1120})
        self.trace.stop_gate.set()
        self.wait(lambda:set(self.player._slots)=={20} and self.player._slots[20].active
                  and ('camera_windows_released',[1110,1111]) in self.received)
        created=[call[1] for call in self.trace.snapshot() if call[0]=='create']
        self.assertNotIn(1111,created,'queued obsolete page created an unnecessary decoder')
        self.assertIn(('camera_windows_released',[101,102]),self.received)
        self.assertIn(('camera_windows_released',[1110,1111]),self.received)
        released=[window for kind,windows in self.received if kind=='camera_windows_released' for window in windows]
        self.assertEqual(sorted(released),[101,102,1110,1111])
        self.assertNotIn(1120,released,'the surviving page must retain its current HWND')

    def test_slow_page_load_does_not_stop_master_clock_or_seek_programme(self):
        self.player.submit('play');self.wait(lambda:self.player._playing and self.player._position>.03)
        controller_class=type(self.player._slots[1].controller)
        load=controller_class.load
        def delayed(controller,path):
            if controller.window>=1100:time.sleep(.055)
            return load(controller,path)
        start=self.player._position;before=len(self.trace.snapshot())
        with patch.object(controller_class,'load',delayed):
            self.player.submit('camera_windows',windows={camera:1090+camera for camera in range(10,19)})
            self.wait(lambda:set(self.player._slots)==set(range(10,19)) and all(slot.active for slot in self.player._slots.values()))
        self.assertGreater(self.player._position,start+.35)
        after=self.trace.snapshot()[before:]
        self.assertFalse([call for call in after if call[1] in (200,300) and
                          (call[0]=='load' or (call[0]=='command' and
                           (call[2][0] in ('loadfile','seek') or call[2][:2]==('set_property','pause'))))])
        last_seeks=[call[2][1] for call in after if call[0]=='command' and call[1]==1108 and call[2][0]=='seek']
        self.assertGreater(last_seeks[-1],start+.35)


if __name__=='__main__':unittest.main()
