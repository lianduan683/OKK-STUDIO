"""Shutdown errors must not skip independent camera owners or claim success."""
from __future__ import annotations

import queue
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from multicam_player import MulticamPlayer


class ShutdownPlayer(MulticamPlayer):
    def _dispatch(self, action, values):
        if action != 'shutdown_fixture':
            return super()._dispatch(action, values)
        self._program.controller = values['programme']
        for slot, controller in zip(self._slots.values(), values['cameras']):
            slot.controller = controller
        self._gpu_session = values.get('session')
        self._subtitle_cache = values['subtitles']
        self._grade_engine = values['grades']
        values['installed'].set()


def ordinary_controller():
    return SimpleNamespace(release_only=False, process=None, stop=Mock())


class PlayerShutdownTests(unittest.TestCase):
    def setUp(self):
        self.player = ShutdownPlayer('unused', {i: 100+i for i in range(1,10)}, 200, 300, queue.Queue())
        self.cameras = [ordinary_controller() for _ in range(9)]
        self.subtitles = SimpleNamespace(cleanup=Mock())
        self.grades = SimpleNamespace(close=Mock())

    def tearDown(self):
        # Any intentional failed owner must be made closable before teardown.
        self.player.close()
        self.assertTrue(self.player.wait_closed(2.))

    def install(self, programme, session=None):
        installed = threading.Event()
        self.player.submit('shutdown_fixture', programme=programme, cameras=self.cameras,
                           subtitles=self.subtitles, grades=self.grades, session=session, installed=installed)
        self.assertTrue(installed.wait(1.))

    def test_native_release_failure_closes_nine_cameras_signals_failure_and_retries_only_owner(self):
        broken = SimpleNamespace(release_only=True, stop=Mock(side_effect=RuntimeError('native close timeout')))
        self.install(broken)
        self.player.close()
        self.assertTrue(self.player._done.wait(1.))
        with self.assertRaisesRegex(RuntimeError, 'native close timeout'):
            self.player.wait_closed(0)
        self.assertTrue(all(controller.stop.call_count == 1 for controller in self.cameras))
        self.subtitles.cleanup.assert_called_once()
        self.grades.close.assert_called_once()
        self.assertIs(self.player._program.controller, broken)
        self.assertTrue(all(slot.controller is None for slot in self.player._slots.values()))
        broken.stop.side_effect = None
        self.player.close()
        self.assertTrue(self.player.wait_closed(1.))
        self.assertEqual(broken.stop.call_count, 2)
        self.assertTrue(all(controller.stop.call_count == 1 for controller in self.cameras))
        self.player.close()
        self.assertEqual(broken.stop.call_count, 2, 'successful close must stay idempotent')

    def test_failed_shared_backend_is_retained_for_retry_even_if_texture_already_released(self):
        backend = SimpleNamespace(close=Mock(side_effect=RuntimeError('GPU event pump timeout')))
        texture = SimpleNamespace(release_only=True, session=backend, stop=Mock())
        self.install(texture, backend)
        self.player.close()
        with self.assertRaisesRegex(RuntimeError, 'GPU event pump timeout'):
            self.player.wait_closed(1.)
        self.assertIs(self.player._gpu_session, backend)
        self.assertIsNone(self.player._program.controller)
        self.assertTrue(all(controller.stop.called for controller in self.cameras))
        backend.close.side_effect = None
        self.player.close()
        self.assertTrue(self.player.wait_closed(1.))
        self.assertIsNone(self.player._gpu_session)
        texture.stop.assert_called_once()
        self.assertEqual(backend.close.call_count, 2)

    def test_final_shared_session_close_can_recover_a_texture_release_failure(self):
        backend = SimpleNamespace(close=Mock())
        texture = SimpleNamespace(release_only=True, session=backend,
                                  stop=Mock(side_effect=RuntimeError('texture first close failed')))
        self.install(texture, backend)
        self.player.close()
        self.assertTrue(self.player.wait_closed(1.))
        backend.close.assert_called_once()
        self.assertIsNone(self.player._gpu_session)
        self.assertIsNone(self.player._program.controller)

    def test_timeout_means_pending_and_retry_never_runs_on_callers_thread(self):
        entered, gate = threading.Event(), threading.Event()
        caller = threading.get_ident()
        threads = []
        def stop():
            threads.append(threading.get_ident())
            entered.set()
            if not gate.wait(2.): raise RuntimeError('test gate timed out')
        programme = SimpleNamespace(release_only=True, stop=stop)
        self.install(programme)
        began = time.monotonic()
        self.player.close()
        self.assertLess(time.monotonic()-began, .1)
        self.assertTrue(entered.wait(1.))
        self.assertFalse(self.player.wait_closed(.01))
        gate.set()
        self.assertTrue(self.player.wait_closed(1.))
        self.assertNotIn(caller, threads)

    def test_cache_cleanup_failure_is_reported_after_all_media_owners_close_and_can_retry(self):
        programme = ordinary_controller()
        self.subtitles.cleanup.side_effect = PermissionError('subtitle cache still locked')
        self.install(programme)
        self.player.close()
        with self.assertRaisesRegex(RuntimeError, 'subtitle cache still locked'):
            self.player.wait_closed(1.)
        programme.stop.assert_called_once()
        self.assertTrue(all(controller.stop.called for controller in self.cameras))
        self.grades.close.assert_called_once()
        self.assertIs(self.player._subtitle_cache, self.subtitles)
        self.subtitles.cleanup.side_effect = None
        self.player.close()
        self.assertTrue(self.player.wait_closed(1.))

    def test_retry_thread_start_failure_keeps_error_observable_and_can_retry_again(self):
        programme = SimpleNamespace(release_only=True, stop=Mock(side_effect=RuntimeError('first close failed')))
        self.install(programme)
        self.player.close()
        with self.assertRaisesRegex(RuntimeError, 'first close failed'):
            self.player.wait_closed(1.)
        with patch('multicam_player.threading.Thread.start', side_effect=RuntimeError('thread unavailable')):
            self.player.close()
        with self.assertRaisesRegex(RuntimeError, 'thread unavailable'):
            self.player.wait_closed(0)
        programme.stop.side_effect = None
        self.player.close()
        self.assertTrue(self.player.wait_closed(1.))


class ProcessShutdownTests(unittest.TestCase):
    def test_failed_force_kill_retains_process_owner_and_reports_failure(self):
        process = SimpleNamespace(pid=123, wait=Mock(side_effect=subprocess.TimeoutExpired('mpv', .7)),
                                  kill=Mock(side_effect=PermissionError('kill denied')))
        controller = SimpleNamespace(release_only=False, process=process)
        def stop(): controller.process = None
        controller.stop = Mock(side_effect=stop)
        with self.assertRaisesRegex(RuntimeError, '123.*kill denied'):
            MulticamPlayer._dispose(controller)
        self.assertIs(controller.process, process)
        process.kill.side_effect = None
        process.wait.side_effect = None
        MulticamPlayer._dispose(controller)
        self.assertIsNone(controller.process)

    def test_quit_error_recovers_if_process_is_reaped_and_pipe_is_closed(self):
        process = SimpleNamespace(wait=Mock(), kill=Mock())
        controller = SimpleNamespace(release_only=False, process=process,
            stop=Mock(side_effect=RuntimeError('quit failed')), _close_pipe=Mock())
        MulticamPlayer._dispose(controller)
        controller._close_pipe.assert_called_once()
        self.assertIsNone(controller.process)
        process.kill.assert_not_called()


if __name__ == '__main__': unittest.main()
