"""Effect handoffs prepare hidden rendering and retain a moving visible buffer."""
import queue
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from creative_core import ColorGrade
from multicam_core import CameraClip, Cut, MulticamProject
import multicam_player as module
from multicam_player import MulticamPlayer, _Slot, _PreviewSuperseded
from program_edits import set_grade_on_range
from test_multicam_player import _Trace, _controller_type


class PreparedFrameTests(unittest.TestCase):
    def test_gpu_foreground_refresh_covers_both_mix_textures_and_paused_refresh_only_wakes(self):
        player = MulticamPlayer.__new__(MulticamPlayer)
        player._project = SimpleNamespace(fps=30.)
        player._playing = True
        player._gpu_live_enabled = True
        player._stack_enabled = False
        player._slots = {}
        player._comparison_enabled = False
        player._clock = lambda: 10.
        player._duration = lambda: 30.
        player._position = 10.
        a, b = object(), object()
        player._gpu_context = {'a': a, 'b': b, 'plan': SimpleNamespace(
            outgoing=SimpleNamespace(native_time=lambda position: position+2),
            incoming=SimpleNamespace(native_time=lambda position: position-1))}
        corrected, wakes = [], []
        player._refresh_picture_clock = lambda slot, target: corrected.append((slot, target))
        player._gpu_session = SimpleNamespace(wake=lambda: wakes.append(True))
        player._refresh_presentation()
        self.assertEqual([item[0] for item in corrected], [a, b])
        self.assertAlmostEqual(corrected[0][1], 12., places=2)
        self.assertAlmostEqual(corrected[1][1], 9., places=2)
        self.assertEqual(len(wakes), 1)
        corrected.clear()
        player._playing = False
        player._last_presentation_refresh = float('-inf')
        player._refresh_presentation()
        self.assertEqual(corrected, [])
        self.assertEqual(len(wakes), 2)

    def test_early_programme_and_camera_slow_down_without_restarting_decode(self):
        for thumbnail in (False, True):
            with self.subTest(thumbnail=thumbnail):
                player = MulticamPlayer.__new__(MulticamPlayer)
                player._project = SimpleNamespace(fps=30.)
                commands = []
                class Controller:
                    def command(self, command, **kwargs):
                        commands.append(command)
                        if command == ['get_property', 'seeking']: return False
                        if command == ['get_property', 'time-pos']: return 4.1
                slot = _Slot(1, controller=Controller())
                player._correct_picture_drift(slot, 4., thumbnail=thumbnail)
                self.assertLess(slot.playback_speed, 1.)
                self.assertFalse(any(command[0] == 'seek' for command in commands))

    def test_cancelled_load_invalidates_asset_key_after_decoder_already_changed_file(self):
        player = MulticamPlayer.__new__(MulticamPlayer)
        player._closing = threading.Event()
        player._navigation_serial = player._active_navigation_serial = 0
        class Controller:
            path = 'red.mp4'
            def load(self, path):
                self.path = path
                player._navigation_serial += 1  # Drag arrived after loadfile.
        controller = Controller()
        slot = _Slot(1, controller=controller, key=('red.mp4',), active=True,
                     prewarm_target=(('red.mp4',), 0.))
        player._all_slots = lambda: [slot]
        with self.assertRaises(_PreviewSuperseded):
            player._load_picture(controller, 'blue.mp4')
        self.assertEqual(controller.path, 'blue.mp4')
        self.assertIsNone(slot.key, 'old source key would wrongly reuse the blue decoder for red')
        self.assertFalse(slot.active)
        self.assertIsNone(slot.prewarm_target)

    def test_running_handoff_corrects_paused_frame_outside_native_frame_tolerance(self):
        # A 70 ms-old frame passed the old 120 ms reuse test, then could never
        # pass a native 60 fps readiness check while the decoder stayed paused.
        for actual, target, native_fps in [(4., 4.07, 30.), (4., 4.013, 60.)]:
            with self.subTest(target=target, native_fps=native_fps):
                player = MulticamPlayer.__new__(MulticamPlayer)
                player._project = SimpleNamespace(fps=30.)
                player._playing = True
                player._closing = threading.Event()
                player._navigation_serial = player._active_navigation_serial = 0
                commands = []
                class Controller:
                    position = actual
                    def command(self, command, **kwargs):
                        commands.append(command)
                        if command[0] == 'seek': self.position = command[1]
                        if command == ['get_property', 'time-pos']: return self.position
                        if command == ['get_property', 'container-fps']: return native_fps
                    def wait_for_frame(self, expected, **kwargs):
                        if abs(self.position-expected) > .5/native_fps+.002:
                            raise RuntimeError('paused stale frame cannot become ready')
                        return self.position
                slot = _Slot(1, controller=Controller())
                self.assertAlmostEqual(player._prepare_picture_frame(slot, target), target)
                self.assertEqual(sum(command[0] == 'seek' for command in commands), 1)

    def test_same_ready_frame_is_reused_without_another_gop_seek(self):
        player = MulticamPlayer.__new__(MulticamPlayer)
        player._project = SimpleNamespace(fps=30.)
        player._playing = True
        player._closing = threading.Event()
        player._navigation_serial = player._active_navigation_serial = 0
        commands = []
        class Controller:
            def command(self, command, **kwargs):
                commands.append(command)
                if command == ['get_property', 'time-pos']: return 4.
                if command == ['get_property', 'container-fps']: return 30.
            def wait_for_frame(self, target, **kwargs): return 4.
        self.assertEqual(player._prepare_picture_frame(_Slot(1, controller=Controller()), 4.005), 4.)
        self.assertFalse(any(command[0] == 'seek' for command in commands))


class BoundaryWorkerTests(unittest.TestCase):
    def setUp(self):
        self.trace = _Trace()
        trace = self.trace
        base = _controller_type(trace)
        class Controller(base):
            def wait_for_frame(self, target, **kwargs):
                trace.record('ready', self.window, target)
                return self._position()
        self.patches = [patch.object(module, 'MpvController', Controller),
                        patch.object(module, '_AudioController', Controller),
                        patch.object(MulticamPlayer, 'TICK', .005)]
        for item in self.patches: item.start()
        self.events = queue.Queue()
        self.player = MulticamPlayer('mpv', {}, 200, 300, self.events, program_standby_window=201)
        self.project = MulticamProject(clips=[CameraClip('a', 1, 'red.mp4', 0, 8),
                                             CameraClip('b', 2, 'blue.mp4', 0, 8)],
                                       cuts=[Cut(0, 1), Cut(2, 2)], fps=30.,
                                       master_audio='master.wav', master_duration=8.)

    def tearDown(self):
        self.player.close()
        stopped = self.player.wait_closed(3)
        for item in reversed(self.patches): item.stop()
        self.assertTrue(stopped, 'effect test leaked a player worker')

    def wait(self, predicate):
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            if predicate(): return
            time.sleep(.003)
        self.fail('effect handoff did not reach expected state')

    def configure(self):
        self.player.submit('configure', project=self.project)
        self.wait(lambda: self.player._awaiting_program_swap == 200)
        self.player.submit('program_swap_ack', window=200)
        self.wait(lambda: self.player._program_standby.prewarm_target is not None)

    def test_effect_shader_and_first_frame_are_prepared_before_the_cut(self):
        set_grade_on_range(self.project, 2, 8, ColorGrade(exposure=.5))
        self.configure()
        standby = self.player._program_standby
        calls = self.trace.snapshot()
        shader = [index for index, (kind, window, payload, _) in enumerate(calls)
                  if window == standby.window and kind == 'command'
                  and payload[:2] == ('change-list', 'glsl-shaders')]
        ready = [index for index, (kind, window, _, _) in enumerate(calls)
                 if window == standby.window and kind == 'ready']
        self.assertTrue(shader, 'first effect shader was deferred until the boundary')
        self.assertTrue(ready, 'prewarming did not produce a rendered frame')
        self.assertLess(shader[0], ready[-1])
        self.assertEqual(self.player._program.controller.path, 'red.mp4')
        self.assertEqual(self.player._position, 0.)

    def test_old_visible_buffer_keeps_playing_until_ui_acknowledges_swap(self):
        self.configure()
        self.player.submit('play')
        self.wait(lambda: self.player._playing and not self.player._program.controller.paused)
        previous = self.player._program
        self.player.submit('audition', camera=2)
        self.wait(lambda: self.player._awaiting_program_swap == 201)
        start = previous.controller._position()
        time.sleep(.06)  # Simulate a delayed Tk poll, not a decoder stall.
        self.assertFalse(previous.controller.paused)
        self.assertGreater(previous.controller._position()-start, .04)
        self.assertFalse(self.player._audio.controller.paused)
        self.player.submit('program_swap_ack', window=999)  # Stale ACK cannot park it.
        time.sleep(.02)
        self.assertFalse(previous.controller.paused)
        self.player.submit('program_swap_ack', window=201)
        self.wait(lambda: previous.controller.paused)
        self.assertFalse(self.player._program.controller.paused)

    def test_changed_future_grade_reprepares_standby_before_playback(self):
        self.configure()
        set_grade_on_range(self.project, 2, 8, ColorGrade(exposure=.75))
        self.player.submit('configure', project=self.project)
        self.wait(lambda: any(kind == 'command' and window == 201 and
                             payload[:2] == ('set_property', 'glsl-shader-opts') and
                             payload[2]['g1_exposure'] == '0.75'
                             for kind, window, payload, _ in self.trace.snapshot()))
        self.assertEqual(self.player._program.window, 200)

    def test_prerolled_source_crosses_boundary_without_pause_seek_or_frame_wait(self):
        self.configure()
        self.player.submit('seek', time=1.75)
        self.wait(lambda: self.player._position == 1.75)
        self.player.submit('play')
        self.wait(lambda: self.player._program_standby.prewarm_running)
        incoming = self.player._program_standby
        self.assertFalse(incoming.controller.paused)
        start = len(self.trace.snapshot())
        self.wait(lambda: self.player._awaiting_program_swap == 201)
        calls = [item for item in self.trace.snapshot()[start:] if item[1] == incoming.window]
        self.assertFalse(any(kind in ('load', 'ready') for kind, _, _, _ in calls), calls)
        self.assertFalse(any(kind == 'command' and
                             (payload[0] == 'seek' or payload == ('set_property', 'pause', True))
                             for kind, _, payload, _ in calls), calls)
        self.assertLess(abs(incoming.controller._position()-self.player._audio.controller._position()), .04)

    def test_pause_preserves_hidden_preroll_and_resume_does_not_reload_or_seek(self):
        self.configure()
        self.player.submit('play')
        self.wait(lambda: self.player._program_standby.prewarm_running)
        incoming = self.player._program_standby
        self.player.submit('pause')
        self.wait(lambda: not self.player._playing and incoming.controller.paused)
        self.assertTrue(incoming.prewarm_running, 'pause discarded the prepared decoder clock')
        start = len(self.trace.snapshot())
        self.player.submit('play')
        self.wait(lambda: incoming.prewarm_running and not incoming.controller.paused)
        calls = self.trace.snapshot()[start:]
        self.assertFalse(any(kind in ('load', 'ready') for kind, _, _, _ in calls), calls)
        self.assertFalse(any(kind == 'command' and payload[0] in ('seek', 'loadfile', 'stop')
                             for kind, _, payload, _ in calls), calls)
        self.assertLess(abs(incoming.controller._position()-self.player._audio.controller._position()), .04)

    def test_play_after_long_paused_seek_accepts_audio_clock_without_reseeking(self):
        self.configure()
        self.player.submit('seek', time=.5)
        self.wait(lambda: self.player._position == .5)
        time.sleep(.26)  # Exceeds the audio seek acknowledgement's 200 ms band.
        start = len(self.trace.snapshot())
        self.player.submit('play')
        self.wait(lambda: self.player._playing and self.player._audio_seek_pending is None)
        self.assertFalse(any(kind == 'command' and window == 300 and payload[0] == 'seek'
                             for kind, window, payload, _ in self.trace.snapshot()[start:]))

    def test_delayed_tick_never_seeks_master_audio_back_to_its_old_sample(self):
        self.configure()
        self.player.submit('play')
        self.wait(lambda: self.player._playing)
        start = len(self.trace.snapshot())
        # Model an old clock sample retained during slow rendering or IPC.
        with patch.object(self.player, '_clock', return_value=0.), patch.object(MulticamPlayer, 'DRIFT_INTERVAL', 0.):
            self.player._audio.controller.position = .5
            time.sleep(.04)
        self.assertFalse(any(kind == 'command' and window == 300 and payload[0] == 'seek'
                             for kind, window, payload, _ in self.trace.snapshot()[start:]))

    def test_foreground_refresh_corrects_stale_programme_and_camera_once_keeps_audio_and_cache(self):
        self.configure()
        self.player.submit('camera_windows', windows={1: 101})
        self.wait(lambda: 1 in self.player._slots and self.player._slots[1].active)
        self.player.submit('play')
        self.wait(lambda: self.player._playing and not self.player._program.controller.paused)
        self.player._program.controller.position -= .3
        self.player._slots[1].controller.position -= .3
        saved = [(slot, slot.controller, slot.key) for slot in self.player._all_slots() if slot.controller]
        request_id = self.player._request_id
        start = len(self.trace.snapshot())
        for _ in range(20): self.player.submit('refresh_presentation')
        self.wait(lambda: any(kind == 'command' and window == 101 and payload[0] == 'seek'
                             for kind, window, payload, _ in self.trace.snapshot()[start:]))
        time.sleep(.02)
        calls = self.trace.snapshot()[start:]
        for window in (200, 101):
            self.assertEqual(sum(kind == 'command' and target == window and payload[0] == 'seek'
                                 for kind, target, payload, _ in calls), 1)
        self.assertFalse(any(kind == 'load' or kind == 'command' and window == 300 and
                             (payload[0] in ('seek', 'loadfile', 'stop') or payload[:2] == ('set_property', 'pause'))
                             for kind, window, payload, _ in calls))
        self.assertTrue(all(slot.controller is controller and slot.key == key for slot, controller, key in saved))
        self.assertEqual(self.player._request_id, request_id)
        self.assertTrue(self.player._playing)

    def test_foreground_refresh_while_paused_preserves_every_cached_frame(self):
        self.configure()
        start = len(self.trace.snapshot())
        position = self.player._position
        self.player.submit('refresh_presentation')
        self.wait(lambda: self.player._last_presentation_refresh > float('-inf'))
        calls = self.trace.snapshot()[start:]
        self.assertFalse(any(kind == 'load' or kind == 'command' and payload[0] in ('seek', 'loadfile', 'stop')
                             for kind, _, payload, _ in calls))
        self.assertEqual(self.player._position, position)
        self.assertFalse(self.player._playing)


if __name__ == '__main__':
    unittest.main()
