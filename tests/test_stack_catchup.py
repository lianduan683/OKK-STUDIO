"""Continuous overload must skip stale decode work without cache growth."""
import threading
import time
import unittest
from unittest.mock import patch

from video_stack_preview import PreviewCatchUp, StackPreview


class StackCatchupTests(unittest.TestCase):
    def setUp(self):
        self.preview = StackPreview('ffmpeg', 'ffprobe', lambda *args: None, width=2, height=2)
        self.preview._playing = True
        self.preview._clock = 10.
        self.preview._startup_seconds = .12
        self.preview._last_catchup = 0.

    def tearDown(self): self.preview.close()

    def test_large_lag_skips_to_master_plus_measured_startup_cost(self):
        with patch('video_stack_preview.time.monotonic', return_value=5.):
            target = self.preview._catchup_target(0, 8., 30., 100.)
        self.assertAlmostEqual(target, 10.+.12+1/30)
        self.assertEqual(self.preview._last_catchup, 5.)

    def test_small_lag_pause_cancellation_and_pending_seek_never_restart(self):
        self.assertIsNone(self.preview._catchup_target(0, 9.6, 30., 100.))
        self.preview._playing = False
        self.assertIsNone(self.preview._catchup_target(0, 0., 30., 100.))
        self.preview._playing = True
        self.preview._cancel.set()
        self.assertIsNone(self.preview._catchup_target(0, 0., 30., 100.))
        self.preview._cancel.clear()
        with self.preview._condition:
            self.preview._pending = ('user seek',)
            self.assertIsNone(self.preview._catchup_target(0, 0., 30., 100.))
            self.preview._pending = None
        self.assertIsNone(self.preview._catchup_target(1, 0., 30., 100.))

    def test_sixty_seconds_of_overload_is_throttled_and_has_one_target_at_a_time(self):
        targets = []
        for frame in range(1800):
            now = frame/30
            self.preview._clock = now+10.
            with patch('video_stack_preview.time.monotonic', return_value=now):
                target = self.preview._catchup_target(0, now+8., 30., 100.)
            if target is not None: targets.append((now, target))
        self.assertGreater(len(targets), 0)
        self.assertLessEqual(len(targets), 24)
        self.assertTrue(all(b[0]-a[0] >= 2.5 for a, b in zip(targets, targets[1:])))
        self.assertIsNone(self.preview._pending)
        self.assertIsNone(self.preview._latest)
        self.assertEqual(self.preview._processes, set())

    def test_end_of_programme_never_loops_restarting_last_frame(self):
        self.preview._clock = 9.99
        self.assertIsNone(self.preview._catchup_target(0, 0., 30., 10.))

    def test_worker_replaces_stale_job_without_new_generation_or_probe_owner(self):
        calls = []
        finished = threading.Event()
        project = object()
        self.preview._media['source'] = 'retained metadata'
        def render(job, cancel):
            calls.append(job)
            if len(calls) == 1: raise PreviewCatchUp(15.2)
            finished.set()
        self.preview._render = render
        self.preview.request(project, 0., True, 123, use_proxies=False)
        self.assertTrue(finished.wait(2.))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertEqual(calls[1][1:5], (project, 15.2, True, 123))
        self.assertEqual(self.preview._catchup_count, 1)
        self.assertEqual(self.preview._media, {'source': 'retained metadata'})


if __name__ == '__main__': unittest.main(verbosity=2)
