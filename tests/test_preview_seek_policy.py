"""Playback correction must not repeatedly restart high-resolution GOP seeks."""
from __future__ import annotations

from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

import multicam_player as module
from multicam_player import MulticamPlayer, _Slot
from multicam_core import Cut
try:
    from . import test_program_double_buffer as double_fixture
    from . import test_transition_preview as transition_fixture
except ImportError:
    import test_program_double_buffer as double_fixture
    import test_transition_preview as transition_fixture


class PictureDriftPolicyTests(unittest.TestCase):
    def setUp(self):
        self.player = object.__new__(MulticamPlayer)
        self.player._error = Mock()
        self.calls = []
        self.seeking = False
        self.actual = 0.
        self.failure = None
        def command(value, **kwargs):
            self.calls.append((value, kwargs))
            if self.failure: raise self.failure
            if value == ['get_property', 'seeking']: return self.seeking
            if value == ['get_property', 'time-pos']: return self.actual
        self.slot = _Slot(123, controller=SimpleNamespace(command=command))
        self.now = 100.
        self.clock = patch.object(module.time, 'monotonic', side_effect=lambda: self.now)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()

    def seeks(self):
        return [value for value, _ in self.calls if value[0] == 'seek']

    def test_pending_seek_is_never_restarted_by_playback_drift(self):
        self.seeking = True
        self.assertFalse(self.player._correct_picture_drift(self.slot, 10.))
        self.now += 2.
        self.assertFalse(self.player._correct_picture_drift(self.slot, 12.))
        self.assertEqual(self.seeks(), [])
        self.assertFalse(any(value == ['get_property', 'time-pos'] for value, _ in self.calls))

    def test_programme_catches_up_at_bounded_speed_without_another_seek_and_force_resets_it(self):
        self.assertTrue(self.player._correct_picture_drift(self.slot, 10.))
        self.assertEqual(self.slot.playback_speed, 1.15)
        self.assertEqual(self.seeks(), [])
        self.now += .2
        self.actual = 9.98
        self.assertTrue(self.player._correct_picture_drift(self.slot, 10.))
        self.assertEqual(self.slot.playback_speed, 1.)
        self.now += 1.01
        self.actual = 0.
        self.assertTrue(self.player._correct_picture_drift(self.slot, 12.))
        self.player._seek_picture(self.slot, 10.+1/30)
        self.assertEqual(self.seeks()[-1], ['seek', 10.+1/30, 'absolute+exact'])
        self.assertEqual(self.slot.playback_speed, 1.)
        self.assertEqual(len(self.seeks()), 1)
        self.assertTrue(all(1. <= value[2] <= 1.15 for value, _ in self.calls if value[:2] == ['set_property', 'speed']))

    def test_thumbnail_threshold_cooldown_and_property_deadlines_are_independent(self):
        self.actual = 9.7
        self.assertFalse(self.player._correct_picture_drift(self.slot, 10., thumbnail=True))
        self.assertTrue(all(options == {'timeout': .2} for value, options in self.calls))
        self.actual = 9.
        self.assertTrue(self.player._correct_picture_drift(self.slot, 10., thumbnail=True))
        self.now += 1.1
        self.assertFalse(self.player._correct_picture_drift(self.slot, 11.1, thumbnail=True))
        self.now += 1.
        self.assertTrue(self.player._correct_picture_drift(self.slot, 12.1, thumbnail=True))
        self.assertEqual(len(self.seeks()), 2)

    def test_status_timeout_is_nonfatal_and_does_not_send_seek_or_pause(self):
        self.failure = RuntimeError('IPC query timed out')
        self.assertFalse(self.player._correct_picture_drift(self.slot, 10., thumbnail=True))
        self.assertFalse(self.player._correct_picture_drift(self.slot, 11., thumbnail=True))
        self.assertEqual(self.seeks(), [])
        self.assertFalse(any(value[:2] == ['set_property', 'pause'] for value, _ in self.calls))
        self.player._error.assert_called_once()


class SlowHandoffTests(unittest.TestCase):
    setUp = double_fixture.DoubleBufferTests.setUp
    tearDown = double_fixture.DoubleBufferTests.tearDown
    wait = double_fixture.DoubleBufferTests.wait
    drain = double_fixture.DoubleBufferTests.drain

    def test_slow_first_frame_handoff_uses_one_seek_and_keeps_old_picture_until_ready(self):
        self.player.submit('play')
        self.wait(lambda: self.player._playing and not self.player._audio.controller.paused)
        old = self.player._program
        before = len(self.trace.snapshot())
        self.gate.clear()
        self.player.submit('audition', camera=2)
        self.wait(lambda: any(kind == 'frame_wait' and window == 201 for kind, window, _, _ in self.trace.snapshot()[before:]))
        self.assertIs(self.player._program, old)
        self.assertFalse(old.controller.paused)
        self.assertFalse(self.player._audio.controller.paused)
        self.assertFalse(any(kind == 'program_swap' for kind, _ in self.drain()))
        time.sleep(.16)
        self.gate.set()
        self.wait(lambda: self.player._program.window == 201)
        calls = self.trace.snapshot()[before:]
        self.assertEqual(len([value for kind, window, value, _ in calls
                              if kind == 'command' and window == 201 and value[0] == 'seek']), 1)
        self.assertEqual(self.player._program.controller.path, 'blue.mp4')
        self.assertFalse(any(kind == 'command' and window == 300 and
            (value[0] in {'seek', 'loadfile', 'stop'} or value == ('set_property', 'pause', True))
            for kind, window, value, _ in calls))

    def test_prepared_next_cut_reuses_the_frame_without_another_exact_seek(self):
        self.player.submit('seek', time=3.8)
        self.wait(lambda: self.player._program_standby.key is not None
                  and self.player._program_standby.controller.path == 'blue.mp4')
        self.drain()
        self.player.submit('play')
        self.wait(lambda: self.player._playing and not self.player._audio.controller.paused)
        before = len(self.trace.snapshot())
        self.wait(lambda: self.player._program.controller.path == 'blue.mp4')
        calls = self.trace.snapshot()[before:]
        self.assertFalse(any(kind == 'command' and window == 201 and value[0] == 'seek'
                             for kind, window, value, _ in calls))
        self.assertTrue(self.player._program.active)

    def test_paused_frame_steps_keep_every_explicit_exact_seek(self):
        self.player.submit('audition', camera=2)
        self.wait(lambda: self.player._program.controller.path == 'blue.mp4')
        self.drain()
        before = len(self.trace.snapshot())
        for index, position in enumerate((1/30, 2/30, 1/30)):
            self.player.submit('seek', time=position)
            self.wait(lambda: self.player._position == position and len([
                value for kind, window, value, _ in self.trace.snapshot()[before:]
                if kind == 'command' and window == self.player._program.window and value[0] == 'seek']) >= index+1)
            self.assertFalse(self.player._playing)
        values = [value for kind, window, value, _ in self.trace.snapshot()[before:]
                  if kind == 'command' and window == self.player._program.window and value[0] == 'seek']
        self.assertEqual(values, [('seek', value, 'absolute+exact') for value in (1/30, 2/30, 1/30)])

    def test_pause_and_forced_seek_restore_normal_video_speed_after_catchup(self):
        self.player.submit('play')
        self.wait(lambda: self.player._playing and not self.player._audio.controller.paused)
        self.gate.clear()
        self.player.submit('audition', camera=2)
        self.wait(lambda: any(kind == 'frame_wait' and window == 201 for kind, window, _, _ in self.trace.snapshot()))
        time.sleep(.16)
        self.gate.set()
        self.wait(lambda: self.player._program.window == 201 and self.player._program.playback_speed > 1.)
        self.player.submit('pause')
        self.wait(lambda: not self.player._playing and self.player._program.playback_speed == 1.)
        self.assertEqual(self.player._program.controller.speed, 1.)
        self.player.submit('seek', time=2.+1/30)
        self.wait(lambda: self.player._position == 2.+1/30 and abs(self.player._program.controller._position()-(2.+1/30)) < 1e-8)
        self.assertEqual(self.player._program.playback_speed, 1.)
        self.assertEqual(self.player._program.controller.speed, 1.)
        speeds = [value[2] for kind, window, value, _ in self.trace.snapshot()
                  if kind == 'command' and value[:2] == ('set_property', 'speed')]
        self.assertTrue(speeds and all(1. <= value <= 1.15 for value in speeds))


class TransitionExitPrewarmTests(unittest.TestCase):
    setUp = transition_fixture.TransitionPlaybackTests.setUp
    tearDown = transition_fixture.TransitionPlaybackTests.tearDown
    wait = transition_fixture.TransitionPlaybackTests.wait
    drain = transition_fixture.TransitionPlaybackTests.drain
    publish = transition_fixture.TransitionPlaybackTests.publish
    seek = transition_fixture.TransitionPlaybackTests.seek

    def test_paused_clock_zero_prewarms_1_2_second_entry_without_later_edge_eviction(self):
        self.project.cuts = [Cut(0., 1), Cut(1.6, 2)]
        self.project.transitions = [transition_fixture.Transition(1.6, 'fade', .8)]
        self.player.submit('configure', project=self.project)
        signature = transition_fixture.transition_signature(self.project)
        self.wait(lambda: self.player._transition_signature == signature)
        self.player.submit('transition_previews', signature=signature,
            items=[{'path': str(self.file), 'start': 1.2, 'end': 2.0}])
        self.wait(lambda: self.player._program_standby.key is not None
                  and self.player._program_standby.key[0] == 'transition-preview'
                  and self.player._program_standby.prewarm_target is not None)
        standby = self.player._program_standby
        self.assertEqual(self.player._position, 0.)
        self.assertFalse(self.player._playing)
        self.assertTrue(self.player._audio.controller.paused)
        self.assertEqual(self.player._program.controller.path, 'red.mp4')
        self.assertEqual(standby.prewarm_target, (standby.key, 0.))
        before = len(self.trace.snapshot())
        time.sleep(.08)
        self.assertIs(self.player._program_standby, standby)
        self.assertEqual(standby.controller.path, str(self.file))
        self.assertFalse(any(kind == 'load' or (kind == 'command' and value[0] == 'seek')
            for kind, window, value, _ in self.trace.snapshot()[before:] if window == standby.window),
            'later source/exit edge displaced the prepared earliest transition')

    def test_transition_exit_repositions_same_file_standby_before_boundary_then_reuses_it(self):
        # Same source on both sides of a graded cut: when the rendered mix
        # becomes visible, its standby retains the source at the entry time.
        # File-key equality must not prevent preparing the exit source time.
        self.project.cuts = [Cut(0., 2)]
        self.project.edit_points = [4.]
        self.player.submit('configure', project=self.project)
        self.wait(lambda: self.player._program.controller.path == 'blue.mp4')
        self.publish()
        self.seek(2.8)
        old_position = self.player._program.controller._position()
        self.assertLess(old_position, 3.)
        self.wait(lambda: self.player._program_standby.key and self.player._program_standby.key[0] == 'transition-preview')
        self.seek(3.4)
        standby = self.player._program_standby
        self.assertEqual(standby.controller.path, 'blue.mp4')
        self.seek(4.2)
        self.wait(lambda: standby.prewarm_target is not None and standby.prewarm_target[1] == 5.)
        self.assertAlmostEqual(standby.controller._position(), 5.)
        source_window = standby.window
        loads = len([item for item in self.trace.snapshot() if item[0] == 'load' and item[1] == source_window])
        self.player.submit('play')
        self.wait(lambda: self.player._playing and not self.player._audio.controller.paused)
        before = len(self.trace.snapshot())
        self.wait(lambda: self.player._program.controller.path == 'blue.mp4' and self.player._position >= 5.)
        calls = self.trace.snapshot()[before:]
        self.assertFalse(any(kind == 'command' and window == source_window and value[0] == 'seek'
                             for kind, window, value, _ in calls), 'transition exit repeated a GOP seek')
        self.assertEqual(len([item for item in self.trace.snapshot() if item[0] == 'load' and item[1] == source_window]), loads)
        self.assertTrue(self.player._program.active)
        self.assertFalse(any(kind == 'command' and window == 300 and
            (value[0] in {'seek', 'loadfile', 'stop'} or value == ('set_property', 'pause', True))
            for kind, window, value, _ in calls))


if __name__ == '__main__':
    unittest.main(verbosity=2)
