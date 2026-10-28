"""Latest-target transport tests with slow decoders and nine viewer IPCs."""
from __future__ import annotations

import io
import queue
import threading
import time
import unittest
from unittest.mock import patch

import multicam_player as module
from multicam_player import MulticamPlayer
from multicam_core import CameraClip, Cut, MulticamProject
from mpv_controller import MpvController
try:
    from .test_multicam_player import _Trace, _controller_type
except ImportError:
    from test_multicam_player import _Trace, _controller_type


class ScrubTransportTests(unittest.TestCase):
    def setUp(self):
        self.trace = _Trace()
        self.frame_gate = threading.Event()
        self.frame_gate.set()
        self.thumbnail_delay = 0.
        owner = self
        base = _controller_type(self.trace)

        class Controller(base):
            def command(self, command, **kwargs):
                if 101 <= self.window <= 109 and command[0] != 'get_property':
                    time.sleep(owner.thumbnail_delay)
                return super().command(command, **kwargs)

            def wait_for_frame(self, target, *, cancel_event=None, **kwargs):
                owner.trace.record('frame_wait', self.window, target)
                while not owner.frame_gate.wait(.005):
                    if cancel_event is not None and cancel_event.is_set():
                        owner.trace.record('frame_cancel', self.window, target)
                        raise RuntimeError('superseded frame wait')
                return target

        self.patches = [patch.object(module, 'MpvController', Controller),
                        patch.object(module, '_AudioController', Controller),
                        patch.object(MulticamPlayer, 'TICK', .005)]
        for item in self.patches: item.start()
        self.events = queue.Queue()
        self.received = []
        self.player = MulticamPlayer('mpv', {camera: 100+camera for camera in range(1, 10)},
                                    200, 300, self.events, program_standby_window=201)
        self.project = MulticamProject(
            clips=[CameraClip(str(camera), camera, f'camera-{camera}.mp4', 0., 12.)
                   for camera in range(1, 10)],
            cuts=[Cut(0., 1), Cut(7., 2)], master_audio='master.wav', master_duration=12.)
        self.player.submit('configure', project=self.project)
        self.wait(lambda: all(slot.active for slot in self.player._slots.values()) and self.player._program.active)
        self.wait(lambda: self.player._awaiting_program_swap is None)

    def tearDown(self):
        self.frame_gate.set()
        self.thumbnail_delay = 0.
        self.player.close()
        self.assertTrue(self.player.wait_closed(3.))
        for item in reversed(self.patches): item.stop()

    def drain(self):
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            self.received.append((kind, value))
            if kind == 'program_swap':
                self.player.submit('program_swap_ack', window=value)

    def wait(self, predicate, timeout=2.):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            self.drain()
            if predicate(): return
            time.sleep(.003)
        self.fail(f'transport did not settle: {self.received[-5:]}')

    def commands(self, after=0, window=None):
        return [(hwnd, value) for kind, hwnd, value, _ in self.trace.snapshot()[after:]
                if kind == 'command' and (window is None or window == hwnd)]

    def test_programme_seeks_before_nine_slow_thumbnails_and_all_share_final_target(self):
        self.thumbnail_delay = .015
        before = len(self.trace.snapshot())
        began = time.monotonic()
        self.player.submit('scrub', time=3., request_id=1, pause=True)
        self.wait(lambda: any(value == ('seek', 3., 'absolute+exact')
                              for _, value in self.commands(before, 200)), timeout=.2)
        self.assertLess(time.monotonic()-began, .15)
        commands = self.commands(before)
        programme = commands.index((200, ('seek', 3., 'absolute+exact')))
        self.assertFalse(any(101 <= hwnd <= 109 for hwnd, _ in commands[:programme]))
        self.wait(lambda: all(slot.navigation_serial == self.player._active_navigation_serial
                               and slot.controller.paused for slot in self.player._slots.values()))
        self.assertTrue(all(abs(slot.controller._position()-3.) < 1e-8
                            for slot in [self.player._program, self.player._audio, *self.player._slots.values()]))
        self.wait(lambda: any(kind == 'state' and value['request_id'] == 1 and value['time'] == 3.
                              for kind, value in self.received))
        self.assertFalse(self.player._playing)

    def test_hundreds_of_scrubs_cancel_old_frame_wait_without_queueing_old_targets(self):
        self.frame_gate.clear()
        self.player.submit('scrub', time=8., request_id=1, pause=True)
        self.wait(lambda: any(kind == 'frame_wait' and hwnd == 201 for kind, hwnd, _, _ in self.trace.snapshot()))
        before = len(self.trace.snapshot())
        began = time.monotonic()
        for request in range(2, 302):
            self.player.submit('scrub', time=2.+request/10000, request_id=request, pause=True)
        self.wait(lambda: self.player._request_id == 301 and
                  abs(self.player._program.controller._position()-2.0301) < 1e-8, timeout=.4)
        self.assertLess(time.monotonic()-began, .3)
        self.assertTrue(any(kind == 'frame_cancel' for kind, _, _, _ in self.trace.snapshot()[before:]))
        self.assertIsNone(self.player._program_standby.failed_key)
        self.assertLessEqual(len([value for hwnd, value in self.commands(before, 200) if value[0] == 'seek']), 3)
        self.drain()
        self.assertFalse([value for kind, value in self.received if kind == 'error'])

    def test_continuous_input_interrupts_slow_camera_sweep_and_release_finishes_latest_target(self):
        self.thumbnail_delay = .012
        before = len(self.trace.snapshot())
        for request in range(1, 31):
            self.player.submit('scrub', time=request/30., request_id=request, pause=True)
            time.sleep(.007)
        began = time.monotonic()
        self.wait(lambda: self.player._request_id == 30 and
                  abs(self.player._program.controller._position()-1.) < 1e-8, timeout=.3)
        self.assertLess(time.monotonic()-began, .2)
        self.wait(lambda: all(slot.navigation_serial == self.player._active_navigation_serial
                               for slot in self.player._slots.values()))
        self.assertTrue(all(abs(slot.controller._position()-1.) < 1e-8 for slot in self.player._slots.values()))
        latest = [value[1] for hwnd, value in self.commands(before, 200) if value[0] == 'seek']
        self.assertEqual(latest, sorted(latest))
        self.assertEqual(latest[-1], 1.)

    def test_playing_scrub_does_not_sample_old_audio_seek_position_or_force_play(self):
        self.player.submit('play')
        self.wait(lambda: self.player._playing and not self.player._audio.controller.paused)
        audio = self.player._audio.controller
        original = audio.command
        def stale_audio(command, **kwargs):
            if command == ['get_property', 'seeking']: return True
            if command == ['get_property', 'time-pos']: return 0.
            return original(command, **kwargs)
        with patch.object(audio, 'command', side_effect=stale_audio):
            self.player.submit('scrub', time=3., request_id=1, pause=False)
            self.wait(lambda: self.player._request_id == 1 and self.player._program.controller._position() >= 3.)
            self.wait(lambda: any(kind == 'state' and value['request_id'] == 1
                                  for kind, value in self.received))
            self.assertTrue(self.player._playing)
            self.assertGreaterEqual(self.player._position, 3.)
        self.player.submit('scrub', time=2., request_id=2, pause=True)
        self.wait(lambda: self.player._request_id == 2 and not self.player._playing)
        self.player.submit('scrub', time=1., request_id=3, pause=False)
        self.wait(lambda: self.player._request_id == 3)
        self.assertFalse(self.player._playing, 'pause=False must preserve paused state')

    def test_stale_request_is_ignored_and_legacy_seek_preserves_latest_public_id(self):
        self.player.submit('scrub', time=3., request_id=10, pause=True)
        self.wait(lambda: self.player._request_id == 10)
        self.player.submit('scrub', time=1., request_id=9, pause=True)
        self.player.submit('seek', time=2.)
        self.wait(lambda: self.player._position == 2. and self.player._program.controller._position() == 2.)
        self.assertEqual(self.player._request_id, 10)
        self.assertFalse(self.player._playing)
        self.assertFalse(any(kind == 'state' and value['request_id'] < 10 and value['time'] == 1.
                             for kind, value in self.received))


class MpvCancellationTests(unittest.TestCase):
    def test_busy_pipe_read_and_decoder_wait_cancel_before_normal_timeout(self):
        controller = MpvController('mpv', 123)
        controller._pipe = io.BytesIO()
        cancel = threading.Event()
        controller.operation_cancel_event = cancel
        timer = threading.Timer(.025, cancel.set)
        timer.start()
        began = time.monotonic()
        try:
            with patch.object(controller, '_available_bytes', return_value=0):
                with self.assertRaisesRegex(RuntimeError, '取消'):
                    controller._read_line(began+3.)
            self.assertLess(time.monotonic()-began, .15)
            with patch.object(controller, 'command') as command:
                with self.assertRaisesRegex(RuntimeError, '取消'):
                    controller._wait_for_gpu_decoder(8.)
            command.assert_not_called()
        finally:
            timer.join()


if __name__ == '__main__':
    unittest.main()
