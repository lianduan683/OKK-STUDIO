"""Preview-cache ownership and pause preservation; no real media processes."""
from __future__ import annotations

import copy
from pathlib import Path
import queue
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from . import test_transport_controls as controls
except ImportError:
    import test_transport_controls as controls


class GatedPlayer(controls.RecordingPlayer):
    def __init__(self, *, result=True):
        super().__init__()
        self.waiting = threading.Event()
        self.release = threading.Event()
        self.result = result
        self.finished = False

    def wait_closed(self, timeout=None):
        if timeout == 0: return self.finished
        self.waiting.set()
        if not self.release.wait(2.):
            raise AssertionError('test did not release its owned fake player')
        self.finished = self.result
        return self.result


class CacheHarness(controls.TransportHarness):
    def __init__(self):
        super().__init__()
        self.jobs = queue.Queue()
        self.app._log = Mock()
        self.player = GatedPlayer()
        self.previous = self.player
        self.started = []

    def stop_monitors(self):
        self._reset_transport_controls()
        if self.player:
            self.player.close()
        self.player = None
        self.playing = False

    def start_monitors(self):
        if not self.previous.finished:
            raise AssertionError('replacement decoder started before old decoder released')
        self.player = controls.RecordingPlayer()
        self.started.append(self.player)


class CacheReleaseTests(unittest.TestCase):
    def setUp(self):
        self.ws = CacheHarness()

    def tearDown(self):
        self.ws.previous.release.set()

    def finish(self):
        self.assertTrue(self.ws.previous.waiting.wait(1.))
        self.ws.previous.release.set()
        kind, payload = self.ws.jobs.get(timeout=2.)
        self.assertTrue(self.ws._handle_transport_job(kind, payload))
        return kind, payload

    def test_replacement_waits_for_old_decoder_and_preserves_project_proxy_and_position(self):
        ws = self.ws
        ws.project.clips[0].proxy_path = 'one-720p.mp4'
        before = copy.deepcopy(ws.project)
        same_project = ws.project
        ws.current = 7.
        self.assertTrue(ws.unload_preview_cache())
        self.assertTrue(ws.previous.closed)
        self.assertTrue(ws.previous.waiting.wait(1.))
        self.assertIsNone(ws.player)
        self.assertEqual(ws.started, [])
        self.assertTrue(ws.busy)
        self.assertFalse(ws.app.working)
        self.assertFalse(ws.playing)
        self.finish()
        self.assertEqual(len(ws.started), 1)
        self.assertFalse(ws.busy)
        self.assertFalse(ws.playing)
        self.assertIs(ws.project, same_project)
        self.assertEqual(ws.project, before)
        self.assertEqual(ws.current, 7.)
        self.assertEqual(ws.player.scrubs[-1]['time'], 7.)
        self.assertTrue(ws.player.scrubs[-1]['pause'])

    def test_failed_release_reports_error_without_starting_replacement(self):
        ws = self.ws
        ws.previous.result = False
        ws.unload_preview_cache()
        self.finish()
        self.assertEqual(ws.started, [])
        self.assertIsNone(ws.player)
        self.assertFalse(ws.busy)
        self.assertFalse(ws.playing)
        ws.app._log.assert_called_once()
        self.assertIn('播放器仍在释放资源', ws.status.set.call_args.args[0])

    def test_timeout_retains_owner_and_blocks_manual_start_until_closed(self):
        ws = self.ws
        ws.previous.result = False
        ws.unload_preview_cache()
        self.finish()
        self.assertEqual(ws._transport_retiring_players, [ws.previous])
        self.assertFalse(ws._can_start_transport_monitors())
        self.assertFalse(ws.step_frame(1))
        self.assertEqual(ws.started, [])
        ws.previous.finished = True
        self.assertTrue(ws._can_start_transport_monitors())
        self.assertEqual(ws._transport_retiring_players, [])

    def test_closed_player_releases_retired_frames_but_active_decoder_keeps_them(self):
        ws = self.ws
        ws._retired_camera_frames = {44: object(), 55: object()}
        ws._release_camera_frames = Mock()
        self.assertTrue(ws._can_start_transport_monitors())
        ws._release_camera_frames.assert_not_called()
        ws._track_stopped_player(ws.previous)
        ws.player = None
        self.assertFalse(ws._can_start_transport_monitors(report=False))
        ws._release_camera_frames.assert_not_called()
        ws.previous.finished = True
        self.assertTrue(ws._can_start_transport_monitors())
        ws._release_camera_frames.assert_called_once_with([44, 55])

    def test_native_cleanup_error_keeps_owner_for_retry(self):
        ws = self.ws
        ws.player = None
        ws.previous.wait_closed = Mock(side_effect=RuntimeError('GPU still owns a texture'))
        ws._track_stopped_player(ws.previous)
        self.assertFalse(ws._can_start_transport_monitors())
        self.assertEqual(ws._transport_retiring_players, [ws.previous])
        self.assertIn('重试释放缓存', ws.status.set.call_args.args[0])
        ws.previous.wait_closed.side_effect = None
        ws.previous.wait_closed.return_value = True
        self.assertTrue(ws._can_start_transport_monitors())
        self.assertEqual(ws._transport_retiring_players, [])

    def test_changed_project_does_not_reload_old_position_or_restart(self):
        ws = self.ws
        ws.unload_preview_cache()
        ws.project = copy.deepcopy(ws.project)
        ws.current = 0.
        self.finish()
        self.assertEqual(ws.started, [])
        self.assertEqual(ws.current, 0.)
        self.assertFalse(ws.busy)

    def test_stale_token_or_closed_workspace_cannot_restart(self):
        ws = self.ws
        ws.unload_preview_cache()
        self.assertTrue(ws.previous.waiting.wait(1.))
        ws.previous.release.set()
        kind, payload = ws.jobs.get(timeout=2.)
        ws._cache_unload_token += 1
        self.assertTrue(ws._handle_transport_job(kind, payload))
        self.assertEqual(ws.started, [])
        ws._cache_unload_token = payload[0]
        ws.alive = False
        self.assertTrue(ws._handle_transport_job(kind, payload))
        self.assertEqual(ws.started, [])
        self.assertFalse(ws._handle_transport_job('unrelated_job', ()))

    def test_no_player_and_busy_paths_do_not_spawn_decoder_or_worker(self):
        ws = self.ws
        ws.busy = True
        with patch('transport_controls.threading.Thread') as thread:
            self.assertFalse(ws.unload_preview_cache())
            self.assertFalse(ws.previous.closed)
            ws.busy = False
            ws.player = None
            self.assertTrue(ws.unload_preview_cache())
            thread.assert_not_called()
        self.assertEqual(ws.started, [])
        self.assertFalse(ws.busy)
        self.assertFalse(ws.playing)


class CacheReleaseTkTests(unittest.TestCase):
    def setUp(self):
        controls.TransportTkTests.setUp(self)

    def tearDown(self):
        controls.TransportTkTests.tearDown(self)

    def test_cache_button_uses_async_release_and_poll_restores_paused_current_frame(self):
        ws = self.workspace
        previous = GatedPlayer()
        ws.player = previous
        ws.project.clips[0].proxy_path = 'one-720p.mp4'
        before = copy.deepcopy(ws.project)
        ws.current = 5.
        ws.playing = True
        ws.undo_stack = [copy.deepcopy(ws.project)]
        previous_undo = copy.deepcopy(ws.undo_stack)
        created = []

        def restart():
            self.assertTrue(previous.finished)
            ws.player = controls.RecordingPlayer()
            created.append(ws.player)

        try:
            with patch.object(ws, 'start_monitors', side_effect=restart) as start:
                ws.release_cache_button.invoke()
                self.assertTrue(previous.closed)
                self.assertTrue(previous.waiting.wait(1.))
                start.assert_not_called()
                self.assertIsNone(ws.player)
                self.assertFalse(ws.playing)
                previous.release.set()
                job = ws.jobs.get(timeout=2.)
                ws.jobs.put(job)
                ws._poll()
                start.assert_called_once()
            self.assertEqual(len(created), 1)
            self.assertEqual(ws.current, 5.)
            self.assertFalse(ws.playing)
            self.assertFalse(ws.busy)
            self.assertEqual(ws.project, before)
            self.assertEqual(ws.undo_stack, previous_undo)
            self.assertTrue(ws.player.scrubs[-1]['pause'])
            self.assertEqual(ws.player.scrubs[-1]['time'], 5.)
            self.assertEqual(self.errors, [])
        finally:
            previous.release.set()


if __name__ == '__main__':
    unittest.main()
