"""Foreground recovery drops stale presentation work, retaining healthy caches."""
from __future__ import annotations

import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from gpu_live_session import GpuLiveSession as NativeSession
from video_stack_player import VideoStackPlayerMixin
from video_stack_preview import StackPreview


class ForegroundPreviewTests(unittest.TestCase):
    def setUp(self):
        self.preview = StackPreview('ffmpeg', 'ffprobe', lambda *args: None, width=2, height=2)
        self.preview._stream_generation = self.preview._generation
        self.preview._decoded_time = 10.
        self.preview._decoded_fps = 30.
        self.preview._media['cached source'] = object()
        self.preview._rates['cached source'] = 30.
        self.preview._latest = {'time': 9.9, 'rgba': b'old'}
        self.preview._notified = True

    def tearDown(self): self.preview.close()

    def test_gpu_wake_only_signals_retained_renderer(self):
        session = NativeSession.__new__(NativeSession)
        session._error = None
        session._closed = False
        session._wake = threading.Event()
        session._visible = False
        session._cores = {'a': object(), 'b': object()}
        before = dict(session._cores)
        session.request({'op': 'wake'})
        self.assertTrue(session._wake.is_set())
        self.assertEqual(session._cores, before)
        self.assertFalse(session._visible)  # Do not reveal GPU under stack overlay.

    def test_healthy_decoder_drops_stale_mailbox_without_restarting(self):
        self.assertFalse(self.preview.refresh_presentation(10.05, True))
        self.assertIsNone(self.preview.take_frame())
        self.assertEqual(self.preview._clock, 10.05)
        self.assertEqual(self.preview._stream_generation, self.preview._generation)
        self.assertIn('cached source', self.preview._media)
        self.assertIn('cached source', self.preview._rates)

    def test_paused_activation_keeps_exact_frame_and_cached_session(self):
        frame = self.preview._latest
        self.assertFalse(self.preview.refresh_presentation(20., False))
        self.assertIs(self.preview.take_frame(), frame)
        self.assertEqual(self.preview._decoded_time, 10.)

    def test_large_background_decode_lag_requests_one_current_time_restart(self):
        class Player(VideoStackPlayerMixin):
            def _stack_project(self): return 'same project'
        player = Player()
        player._stack_preview = self.preview
        player._playing = True
        player._request_id = 7
        player._subtitle_ass_path = None
        player._use_proxies = True
        player._stack_revision = 2
        player._active_navigation_serial = 5
        player._audio = SimpleNamespace(controller=Mock())
        self.preview.request = Mock()
        player._resync_stack_preview(15.)
        self.preview.request.assert_called_once_with('same project', 15., True, 7,
            ass_path=None, use_proxies=True, preview_epoch=None)
        self.assertEqual(player._stack_job_key, (2, 5))
        player._audio.controller.assert_not_called()
        self.assertIn('cached source', self.preview._media)

    def test_unstarted_decoder_is_not_repeatedly_restarted_on_activation(self):
        self.preview._decoded_time = None
        self.assertFalse(self.preview.refresh_presentation(100., True))


if __name__ == '__main__':
    unittest.main(verbosity=2)
