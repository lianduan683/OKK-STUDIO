"""Dependency-free regressions for the bounded middle-window fallback."""
from __future__ import annotations

from array import array
import builtins
import io
from pathlib import Path
import random
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio_sync
import audio_sync_fallback as fallback
from exporter_core import ExportCancelled


def levels(count, seed=42):
    rng = random.Random(seed)
    return [rng.uniform(100, 12000) for _ in range(count)]


class MiddleSyncFallbackTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="middle_fallback_")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.master, self.camera, self.second = [self.root / name for name in ("master.wav", "camera.mp4", "second.mp4")]
        for path in (self.master, self.camera, self.second):
            path.touch()

    def align(self, energies, durations, **kwargs):
        with patch("audio_sync._duration", side_effect=durations), patch.object(
            fallback, "_stream_energy_python", side_effect=energies
        ) as decode:
            result = fallback.align_without_numpy("ffmpeg", [str(self.camera)], str(self.master), envelope_rate=10, **kwargs)
        return result[0], decode.call_args_list

    def test_middle_waveform_restores_whole_offset_and_does_not_import_numpy(self):
        master = levels(2000)
        # Camera starts at master 12 s; the 180 s camera's middle probe starts at
        # camera 67.5 s, hence master 79.5 s.
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "numpy" or name.startswith("numpy."):
                raise AssertionError("Fallback attempted to import NumPy")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            result, calls = self.align([master, master[795:1245]], [200, 180])
        self.assertAlmostEqual(result.offset_seconds, -12, places=2)
        self.assertGreater(result.confidence, 0.9)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].kwargs["start"], 67.5)
        self.assertEqual(calls[1].kwargs["duration"], 45)

    def test_maximum_constrains_whole_offset_on_both_sides(self):
        master = levels(2000)
        for offset, master_start, maximum in [(-12, 795, 13), (18, 495, 19)]:
            with self.subTest(offset=offset):
                result, _ = self.align([master, master[master_start:master_start + 450]], [200, 180],
                                       max_offset_seconds=maximum)
                self.assertAlmostEqual(result.offset_seconds, offset, places=2)
                self.assertGreater(result.confidence, 0.9)

    def test_master_is_decoded_once_for_batch(self):
        master = levels(2000)
        with patch("audio_sync._duration", side_effect=[200, 180, 180]), patch.object(
            fallback, "_stream_energy_python", side_effect=[master, master[795:1245], master[900:1350]]
        ) as decode:
            result = fallback.align_without_numpy("ffmpeg", [str(self.camera), str(self.second)], str(self.master), envelope_rate=10)
        master_calls = [call for call in decode.call_args_list if call.args[1] == str(self.master)]
        self.assertEqual(len(master_calls), 1)
        self.assertAlmostEqual(result[0].offset_seconds, -12, places=2)
        self.assertAlmostEqual(result[1].offset_seconds, -22.5, places=2)

    def test_silent_middle_tries_nearby_but_never_whole_camera(self):
        master = levels(2000)
        result, calls = self.align([master, [0] * 450, master[345:795]], [200, 180])
        self.assertAlmostEqual(result.offset_seconds, -12, places=2)
        self.assertEqual([call.kwargs["start"] for call in calls[1:]], [67.5, 22.5])
        self.assertTrue(all(call.kwargs["duration"] == 45 for call in calls[1:]))

    def test_all_silent_probes_return_manual_result_after_at_most_three_short_reads(self):
        result, calls = self.align([levels(2000), *([[0] * 450] * 3)], [200, 180], continue_on_error=True)
        self.assertEqual(result.confidence, 0)
        self.assertIn("待手动", result.engine)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(call.kwargs["duration"] == 45 for call in calls[1:]))
        self.assertEqual([call.kwargs["start"] for call in calls[1:]], [67.5, 22.5, 112.5])

    def test_repeated_audio_is_not_trusted_and_does_not_read_more_camera_audio(self):
        take = levels(450, seed=18)
        master = levels(80, 1) + take + levels(80, 2) + take + levels(80, 3)
        result, calls = self.align([master, take], [114, 45], continue_on_error=True)
        self.assertLess(result.confidence, 0.1)
        self.assertIn("待手动", result.engine)
        self.assertEqual(len(calls), 2)

    def test_weak_match_is_manual_and_never_expands_camera_read(self):
        result, calls = self.align([levels(2000, 1), levels(450, 2)], [200, 180], continue_on_error=True)
        self.assertLess(result.confidence, 0.1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].kwargs["duration"], 45)

    def test_isolated_raw_score_024_peak_is_not_trusted(self):
        # A unique accidental peak must remain untrusted even when its second
        # peak is sufficiently lower to produce full uniqueness confidence.
        with patch("editor_core._normalized_fft_correlation_python", return_value=(-795, 0.24, 0.10)):
            result, calls = self.align([levels(2000), levels(450)], [200, 180], continue_on_error=True)
        self.assertLess(result.confidence, 0.1)
        self.assertIn("待手动", result.engine)
        self.assertEqual(len(calls), 2)

    def test_silent_master_is_fatal_even_in_continue_mode(self):
        with self.assertRaisesRegex(RuntimeError, "主录音"):
            self.align([[0] * 2000], [200], continue_on_error=True)

    def test_explicit_full_mode_is_the_only_unbounded_camera_read(self):
        master = levels(2000)
        result, calls = self.align([master, master[120:1920]], [200], mode="full")
        self.assertAlmostEqual(result.offset_seconds, -12, places=2)
        self.assertIsNone(calls[1].kwargs["duration"])
        self.assertEqual(calls[1].kwargs["start"], 0)

    def test_progress_remains_monotonic_and_reaches_100(self):
        master = levels(2000)
        events = []
        self.align([master, master[795:1245]], [200, 180], progress=lambda value, phase: events.append((value, phase)))
        self.assertEqual(events[0][0], 0)
        self.assertEqual(events[-1][0], 100)
        self.assertEqual([value for value, _phase in events], sorted(value for value, _phase in events))

    def test_streaming_reads_bounded_pcm_and_preserves_partial_bins(self):
        class TrackedPipe(io.BytesIO):
            def read(self, count=-1):
                if count < 0 or count > fallback.PCM_CHUNK_BYTES:
                    raise AssertionError("Unbounded PCM read")
                # Unaligned reads exercise int16 samples split across chunks.
                return super().read(min(1403, count))

        class Process:
            def __init__(self, raw):
                self.stdout, self.stderr = TrackedPipe(raw), io.BytesIO()
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

        expected = list(range(1, 801))
        pcm = array("h", [value for value in expected for _ in range(100)])
        if sys.byteorder != "little":
            pcm.byteswap()
        process = Process(pcm.tobytes())
        with patch.object(fallback.subprocess, "Popen", return_value=process) as popen:
            energy = fallback._stream_energy_python("ffmpeg", "camera", 20)
        self.assertEqual(energy, expected)
        command = popen.call_args.args[0]
        self.assertLessEqual(int(command[command.index("-ar") + 1]), 2000)

    def test_blocked_decoder_can_be_cancelled_and_terminated(self):
        class BlockingPipe:
            def __init__(self, process):
                self.process = process

            def read(self, count):
                while self.process.returncode is None:
                    time.sleep(0.01)
                return b""

            def close(self):
                pass

        class Process:
            def __init__(self):
                self.returncode = None
                self.terminated = False
                self.stdout, self.stderr = BlockingPipe(self), io.BytesIO()

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

        event = threading.Event()
        process = Process()
        timer = threading.Timer(0.05, event.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.perf_counter()
        with patch.object(fallback.subprocess, "Popen", return_value=process), self.assertRaises(ExportCancelled):
            fallback._stream_energy_python("ffmpeg", "camera", 50, cancel_event=event)
        self.assertTrue(process.terminated)
        self.assertLess(time.perf_counter() - started, 1)


if __name__ == "__main__":
    unittest.main()
