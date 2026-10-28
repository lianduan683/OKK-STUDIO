"""Large visible grids keep programme priority and bounded decoder ownership."""
import queue
import threading
import time
import unittest
from unittest.mock import patch

import multicam_player as module
from multicam_player import MulticamPlayer
from multicam_core import CameraClip, Cut, MulticamProject
try:
    from .test_multicam_player import _Trace, _controller_type
except ImportError:
    from test_multicam_player import _Trace, _controller_type


class Grid36Tests(unittest.TestCase):
    def setUp(self):
        self.trace = _Trace()
        base = _controller_type(self.trace)
        owner = self
        self.live = set(); self.peak = 0; self.slow_close = False
        class Controller(base):
            def start(self):
                if self.window not in (9000,9001):
                    owner.live.add(self.window)
                    owner.peak = max(owner.peak,len(owner.live))
                super().start()
            def stop(self):
                super().stop()
                if owner.slow_close and self.window not in (9000,9001):time.sleep(.012)
                owner.live.discard(self.window)
        self.patches = [patch.object(module,'MpvController',Controller),patch.object(module,'_AudioController',Controller),
                        patch.object(MulticamPlayer,'TICK',.005)]
        for value in self.patches:value.start()
        self.events=queue.Queue();self.received=[]
        self.player=MulticamPlayer('mpv',{i:100+i for i in range(1,37)},9000,9001,self.events)
        self.project=MulticamProject(clips=[CameraClip(str(i),i,f'camera-{i}.mp4',0.,20.) for i in range(1,73)],
                                    cuts=[Cut(0.,1)],master_audio='master.wav',master_duration=20.)
        self.player.submit('configure',project=self.project)
        self.wait(lambda:len(self.player._slots)==36 and all(slot.active for slot in self.player._slots.values()))

    def tearDown(self):
        self.trace.stop_gate.set();self.slow_close=False
        self.player.close();self.assertTrue(self.player.wait_closed(3.))
        for value in reversed(self.patches):value.stop()

    def wait(self,predicate,timeout=3.):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            while not self.events.empty():self.received.append(self.events.get_nowait())
            if predicate():return
            time.sleep(.002)
        self.fail(str(self.received[-5:]))

    def test_36_visible_sources_only_and_off_page_programme_is_independent(self):
        self.assertEqual(len(self.live),36)
        self.assertEqual(self.peak,36)
        loaded={value for kind,window,value,_ in self.trace.snapshot() if kind=='load' and window!=9000}
        self.assertEqual(loaded,{f'camera-{i}.mp4' for i in range(1,37)})
        self.player.submit('audition',camera=70)
        self.wait(lambda:self.player._program.controller.path=='camera-70.mp4')
        self.assertEqual(set(self.player._slots),set(range(1,37)))
        self.assertEqual(len(self.live),36)

    def test_latest_scrub_preempts_36_retirements_and_obsolete_page_never_allocates(self):
        self.trace.stop_gate.clear();self.trace.cleanup_started.clear();self.slow_close=True
        self.player.submit('camera_windows',windows={i:1000+i for i in range(37,73)})
        self.assertTrue(self.trace.cleanup_started.wait(1.))
        self.player.submit('camera_windows',windows={i:2000+i for i in range(37,73)})
        self.player.submit('camera_windows',windows={i:3000+i for i in range(37,73)})
        for request in range(1,101):self.player.submit('scrub',time=request/30.,request_id=request,pause=True)
        began=time.monotonic();self.trace.stop_gate.set()
        self.wait(lambda:self.player._request_id==100 and abs(self.player._program.controller._position()-100/30.)<1e-8,.2)
        self.assertLess(time.monotonic()-began,.18)
        self.assertTrue(self.player._retired_camera_slots,'programme waited for all 36 decoder exits')
        self.wait(lambda:all(slot.active and slot.navigation_serial==self.player._active_navigation_serial
                             for slot in self.player._slots.values()) and not self.player._retired_camera_slots)
        created={window for kind,window,_,_ in self.trace.snapshot() if kind=='create'}
        self.assertFalse(created & set(range(1037,1073)))
        self.assertFalse(created & set(range(2037,2073)))
        self.assertEqual(self.peak,36,'new page doubled live camera decoder ownership')
        released={window for kind,windows in self.received if kind=='camera_windows_released' for window in windows}
        self.assertEqual(released,set(range(101,137))|set(range(1037,1073))|set(range(2037,2073)))
        self.assertTrue(all(abs(slot.controller._position()-100/30.)<1e-8 for slot in self.player._slots.values()))

    def test_close_reaps_current_and_pending_old_grid_owners(self):
        self.trace.stop_gate.clear();self.trace.cleanup_started.clear()
        self.player.submit('camera_windows',windows={i:3000+i for i in range(37,73)})
        self.assertTrue(self.trace.cleanup_started.wait(1.))
        self.player.close();self.trace.stop_gate.set()
        self.assertTrue(self.player.wait_closed(3.))
        self.assertEqual(self.live,set())
        disposed=[window for kind,window,_,_ in self.trace.snapshot() if kind=='dispose']
        self.assertEqual(set(disposed),set(range(101,137))|{9000,9001})
        self.assertEqual(len(disposed),len(set(disposed)))

    def test_large_grid_play_seeks_programme_before_thumbnail_commands(self):
        # Synchronize a read-only fixture observation onto the worker by using
        # one normal play action, then inspect its command trace.
        before=len(self.trace.snapshot())
        self.player.submit('play')
        self.wait(lambda:self.player._playing and any(kind=='state' and value['playing'] for kind,value in self.received))
        calls=self.trace.snapshot()[before:]
        program_seek=next(index for index,(kind,window,value,_) in enumerate(calls)
                          if kind=='command' and window==9000 and value[0]=='seek')
        self.assertFalse(any(kind=='command' and window in range(101,137) for kind,window,_,_ in calls[:program_seek]))


class GridMappingTests(unittest.TestCase):
    def test_36_is_visible_capacity_not_total_camera_id_limit(self):
        windows={100+i:500+i for i in range(36)}
        self.assertEqual(MulticamPlayer._checked_camera_windows(windows),windows)
        with self.assertRaises(ValueError):MulticamPlayer._checked_camera_windows({i+1:i+100 for i in range(37)})


if __name__=='__main__':unittest.main()
