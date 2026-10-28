"""Bounded next-segment preparation and cancellation at effect boundaries."""
from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch

from multicam_core import MulticamProject
from video_stack_preview import PreviewCancelled, StackPreview
from video_stack_player import VideoStackPlayerMixin


class StackPrewarmTests(unittest.TestCase):
    def setUp(self):
        self.preview = StackPreview('ffmpeg', 'ffprobe', lambda *args: None, width=2, height=2)
        self.preview.update_clock(100.)
        self.cancel = threading.Event()
        self.events = []
        self.processes = []
        self.max_owned = 0
        self.read_hook = None

    def tearDown(self):
        self.preview.close()

    def spawn(self, *args, **kwargs):
        fixture = self
        identity = len(self.processes)
        fixture.events.append(('spawn', identity))

        class Pipe(BytesIO):
            def read(self, count=-1):
                fixture.events.append(('read', identity))
                fixture.max_owned = max(fixture.max_owned, len(fixture.preview._processes))
                if fixture.read_hook is not None: fixture.read_hook(identity)
                data = super().read(count)
                if not data: process.returncode = 0
                return data

        class Process:
            returncode = None
            stderr = None
            stdout = Pipe(bytes([identity % 255])*16)

            def poll(self): return self.returncode

            def terminate(self):
                fixture.events.append(('terminate', identity))
                self.returncode = -15

            def kill(self): self.returncode = -9

            def wait(self, timeout=None):
                fixture.events.append(('wait', identity))
                if self.returncode is None: self.returncode = 0
                return self.returncode

        process = Process()
        self.processes.append(process)
        return process

    def render(self, count=5, playing=True):
        self.preview.set_playing(playing)
        segments = [SimpleNamespace(start=float(index), duration=1., frames=30, layers=())
                    for index in range(count)]
        project = MulticamProject(master_duration=float(count), fps=30.)
        job = (0, project, 0., playing, 1, None, False, None)
        with patch('video_stack_preview.preview_segments', return_value=iter(segments)), \
                patch('video_stack_preview.validate_stack_media'), \
                patch('video_stack_preview.preview_command', side_effect=lambda *a, **kw:
                      (['ffmpeg', '-filter_complex', 'null', '-f', 'rawvideo', 'pipe:1'], 30.)), \
                patch('video_stack_preview.subprocess.Popen', side_effect=self.spawn):
            self.preview._render(job, self.cancel)

    def test_next_effect_process_starts_before_current_segment_is_consumed(self):
        self.render(count=12)
        self.assertLess(self.events.index(('spawn', 1)), self.events.index(('read', 0)))
        for identity in range(1, 11):
            self.assertLess(self.events.index(('wait', identity-1)), self.events.index(('spawn', identity+1)))
        self.assertEqual(self.max_owned, 2)
        self.assertEqual(len(self.processes), 12)
        self.assertEqual(self.preview._processes, set())
        self.assertIsNone(self.preview._process)
        self.assertTrue(all(process.stdout.closed for process in self.processes))

    def test_paused_exact_frame_starts_no_speculative_process(self):
        self.render(count=1, playing=False)
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.max_owned, 1)

    def test_navigation_cancellation_reaps_current_and_prepared_successor(self):
        def cancel_on_read(identity):
            self.cancel.set()
            with self.preview._condition: self.preview._terminate_locked()
        self.read_hook = cancel_on_read
        with self.assertRaises(PreviewCancelled): self.render(count=12)
        self.assertEqual(len(self.processes), 2)
        self.assertIn(('terminate', 0), self.events)
        self.assertIn(('terminate', 1), self.events)
        self.assertEqual(self.preview._processes, set())
        self.assertTrue(all(process.stdout.closed for process in self.processes))

    def test_failed_successor_startup_also_reaps_the_current_process(self):
        original_spawn = self.spawn
        def fail_second(*args, **kwargs):
            if self.processes: raise OSError('cannot start successor')
            return original_spawn(*args, **kwargs)
        self.spawn = fail_second
        with self.assertRaisesRegex(OSError, 'successor'): self.render(count=3)
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.preview._processes, set())
        self.assertTrue(self.processes[0].stdout.closed)

    def test_mailbox_rechecks_decoder_clock_but_preserves_paused_frame(self):
        payload = {'time': 1., 'rgba': b'frame', 'fps': 30., 'playback_frame': True}
        self.preview.set_playing(True)
        self.preview.update_clock(1.)
        self.preview._publish(0, payload)
        self.preview.update_clock(1.2)
        self.assertIsNone(self.preview.take_frame())
        self.preview.set_playing(False)
        self.preview._publish(0, payload)
        frame = self.preview.take_frame()
        self.assertEqual(frame['rgba'], b'frame')
        self.assertEqual(frame['master_time'], 1.2)
        self.assertFalse(frame['playing'])


class StackPauseIntegrationTests(unittest.TestCase):
    def test_space_pause_resume_reuses_stream_but_navigation_and_revision_replace_it(self):
        class Preview:
            live = False
            def __init__(self): self.requests = []; self.cancelled = 0
            def update_clock(self, position): pass
            def set_playing(self, playing): return self.live
            def request(self, *args, **kwargs): self.requests.append((args, kwargs)); self.live = args[2]
            def cancel(self): self.cancelled += 1; self.live = False
        class Player(VideoStackPlayerMixin):
            def _stack_project(self): return 'project snapshot'
            def _emit(self, *args): pass
        player = Player()
        player._init_stack_preview()
        player._stack_enabled = True
        preview = player._stack_preview = Preview()
        player._position = 0.
        player._active_navigation_serial = player._request_id = 0
        player._subtitle_ass_path = None
        player._use_proxies = True
        player._playing = False
        player._sync_stack_preview()
        self.assertEqual(len(preview.requests), 1)  # Initial exact paused frame.
        player._playing = True
        player._sync_stack_preview()
        self.assertEqual(len(preview.requests), 2)  # Start continuous stream once.
        player._playing = False
        player._sync_stack_preview()
        player._playing = True
        player._sync_stack_preview()
        self.assertEqual(len(preview.requests), 2)
        self.assertEqual(preview.cancelled, 0)
        player._active_navigation_serial += 1
        player._sync_stack_preview()
        self.assertEqual(len(preview.requests), 3)
        player._stack_revision += 1
        player._sync_stack_preview()
        self.assertEqual(len(preview.requests), 4)
        self.assertEqual(preview.cancelled, 1)


class StackPrewarmMediaTests(unittest.TestCase):
    def test_real_effect_boundaries_keep_export_pixels_and_bounded_processes(self):
        try:
            from .test_video_stack_render import StackMediaTests
        except ImportError:
            from test_video_stack_render import StackMediaTests
        from creative_core import ColorGrade
        from exporter_core import probe_media
        from program_edits import ClipGrade
        from video_stack_preview import preview_command, preview_segments

        StackMediaTests.setUpClass()
        preview = None
        try:
            fixture = StackMediaTests()
            project = fixture.project()
            project.fps = 30.
            project.clips[0].path = str(fixture.dynamic25)
            project.clip_grades = [ClipGrade(.4, .8, ColorGrade(exposure=.3)),
                                  ClipGrade(.8, 1.2, ColorGrade(exposure=-.2)),
                                  ClipGrade(1.2, 1.6, ColorGrade(exposure=.5))]
            media = {clip.path: probe_media(fixture.ffprobe, clip.path) for clip in project.clips}
            expected = b''.join(fixture.command(preview_command(segment, project, fixture.ffmpeg,
                media, 128, 72)[0]) for segment in preview_segments(project, 0.))
            events = []
            preview = StackPreview(fixture.ffmpeg, fixture.ffprobe,
                lambda *event: events.append(event), width=128, height=72)
            preview.request(project, 0., True, 1, use_proxies=False)
            frames = []
            peak = 0
            deadline = time.monotonic()+12.
            while len(frames) < 60 and time.monotonic() < deadline:
                with preview._condition: peak = max(peak, len(preview._processes))
                frame = preview.take_frame()
                if frame is not None:
                    frames.append(frame)
                    if len(frames) == 6:
                        self.assertTrue(preview.set_playing(False))
                        with preview._condition: retained = set(preview._processes)
                        time.sleep(1.)
                        with preview._condition: self.assertEqual(preview._processes, retained)
                        self.assertIsNone(preview.take_frame())
                        self.assertTrue(preview.set_playing(True))
                    # Drive a deterministic master clock, permitting exactly
                    # one output frame at a time; machine speed cannot skip it.
                    preview.update_clock(len(frames)/30.)
                time.sleep(.001)
            self.assertEqual(len(frames), 60, events)
            self.assertEqual(peak, 2)
            self.assertEqual(b''.join(frame['rgba'] for frame in frames), expected)
            for index, frame in enumerate(frames): self.assertAlmostEqual(frame['time'], index/30.)
            self.assertFalse([event for event in events if event[0] == 'error'], events)
            preview.close()
            self.assertEqual(preview._processes, set())
            self.assertFalse(preview._thread.is_alive())
        finally:
            if preview is not None: preview.close()
            StackMediaTests.tearDownClass()


if __name__ == '__main__':
    unittest.main(verbosity=2)
