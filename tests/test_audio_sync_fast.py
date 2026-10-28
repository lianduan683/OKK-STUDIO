"""Small internal regressions for streaming, long clocks and cancellation."""
from __future__ import annotations

import builtins
import io
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import numpy as np
except ImportError:
    np = None

from audio_sync import (_EnergyCache, _match_probes, _stream_energy,
                        align_audio_segments_fast, PCM_CHUNK_BYTES)
from exporter_core import ExportCancelled


@unittest.skipIf(np is None, "NumPy unavailable")
class AudioSyncFastTests(unittest.TestCase):
    def test_full_master_search_reaches_past_two_hours(self):
        rate = 50
        rng = np.random.default_rng(153)
        master = rng.uniform(100, 12000, size=(2 * 3600 + 80) * rate).astype(np.float32)
        start = 2 * 3600 + 12.34
        first = round(start * rate)
        camera = master[first:first + 45 * rate].copy()
        offset, confidence, _score, _agreement = _match_probes([(0, camera)], master, rate, 0, None)
        self.assertAlmostEqual(offset, -start, places=2)
        self.assertGreater(confidence, 0.95)

    def test_probe_start_is_removed_from_positive_master_delay(self):
        rng = np.random.default_rng(19)
        master = rng.uniform(100, 12000, 120 * 50).astype(np.float32)
        # Camera begins ten seconds before master. Camera's 20-second probe
        # therefore starts at master 10 seconds, yielding +10, not -10.
        probe = master[10 * 50:55 * 50]
        offset, confidence, _score, _agreement = _match_probes([(20, probe)], master, 50, 1800, None)
        self.assertAlmostEqual(offset, 10, places=2)
        self.assertGreater(confidence, 0.95)

    def test_pcm_is_read_only_in_bounded_chunks_and_partial_bins_are_preserved(self):
        class TrackedPipe(io.BytesIO):
            def read(self, count=-1):
                if count < 0 or count > PCM_CHUNK_BYTES:
                    raise AssertionError("unbounded PCM read")
                return super().read(count)

        class Process:
            def __init__(self, data):
                self.stdout, self.stderr = TrackedPipe(data), io.BytesIO()
                self.returncode = None

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

            def poll(self):
                return self.returncode

        levels = np.arange(1, 2001, dtype=np.int16)
        raw = np.repeat(levels, 80).astype("<i2").tobytes()
        process = Process(raw)
        with patch("audio_sync.subprocess.Popen", return_value=process):
            energy = _stream_energy("ffmpeg", "camera", 50)
        self.assertEqual(energy.dtype, np.float32)
        np.testing.assert_allclose(energy, levels, atol=0.001)

    def test_running_decoder_is_terminated_and_no_cache_is_committed_on_cancel(self):
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

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                return self.returncode

        event = threading.Event()
        process = Process()
        with tempfile.TemporaryDirectory(prefix="sync_cancel_") as folder:
            root = Path(folder)
            source = root / "camera.wav"
            source.write_bytes(b"placeholder")
            cache = _EnergyCache(root / "cache", 50, "ffmpeg", event)
            timer = threading.Timer(0.08, event.set)
            timer.start()
            started = time.perf_counter()
            try:
                with patch("audio_sync.subprocess.Popen", return_value=process), self.assertRaises(ExportCancelled):
                    cache.energy(source)
            finally:
                timer.cancel()
            self.assertTrue(process.terminated)
            self.assertLess(time.perf_counter() - started, 1)
            self.assertEqual(list((root / "cache").iterdir()), [])

    def test_progress_is_monotonic_and_always_reaches_100_on_warm_cache(self):
        rng = np.random.default_rng(77)
        master = rng.uniform(100, 12000, 60 * 50).astype(np.float32)
        camera = master[10 * 50:30 * 50]
        with tempfile.TemporaryDirectory(prefix="sync_progress_") as folder:
            root = Path(folder)
            main, clip = root / "master.wav", root / "camera.wav"
            main.touch()
            clip.touch()
            events = []
            with patch("audio_sync._duration", side_effect=[60, 20]), patch.object(
                _EnergyCache, "energy", side_effect=[master, camera]
            ):
                result = align_audio_segments_fast("ffmpeg", [str(clip)], str(main), cache_dir=root / "cache",
                                                   progress=lambda percent, phase: events.append((percent, phase)))
            self.assertAlmostEqual(result[0].offset_seconds, -10, places=2)
            self.assertEqual(events[0][0], 0)
            self.assertEqual(events[-1][0], 100)
            self.assertEqual([percent for percent, _phase in events], sorted(percent for percent, _phase in events))
            self.assertTrue(all(phase for _percent, phase in events))

    def test_batch_continues_with_manual_placeholder_for_a_silent_camera(self):
        rng = np.random.default_rng(77)
        master = rng.uniform(100, 12000, 60 * 50).astype(np.float32)
        camera = master[10 * 50:30 * 50]
        silence = np.zeros(20 * 50, dtype=np.float32)
        with tempfile.TemporaryDirectory(prefix="sync_partial_") as folder:
            root = Path(folder)
            paths = [root / name for name in ("master.wav", "camera.wav", "silent.wav")]
            for path in paths:
                path.touch()
            with patch("audio_sync._duration", side_effect=[60, 20, 20]), patch.object(
                _EnergyCache, "energy", side_effect=[master, camera, silence, silence]
            ):
                results = align_audio_segments_fast("ffmpeg", [str(path) for path in paths[1:]], str(paths[0]),
                                                    cache_dir=root / "cache", continue_on_error=True)
            self.assertEqual(len(results), 2)
            self.assertAlmostEqual(results[0].offset_seconds, -10, places=2)
            self.assertGreater(results[0].confidence, 0.9)
            self.assertEqual(results[1].confidence, 0)
            self.assertIn("待手动", results[1].engine)

    def test_batch_continue_never_swallows_cancellation_or_bad_master(self):
        rng = np.random.default_rng(77)
        master = rng.uniform(100, 12000, 60 * 50).astype(np.float32)
        with tempfile.TemporaryDirectory(prefix="sync_partial_cancel_") as folder:
            root = Path(folder)
            main, clip = root / "master.wav", root / "camera.wav"
            main.touch()
            clip.touch()
            with patch("audio_sync._duration", side_effect=[60, 20]), patch.object(
                _EnergyCache, "energy", side_effect=[master, ExportCancelled("cancel now")]
            ), self.assertRaises(ExportCancelled):
                align_audio_segments_fast("ffmpeg", [str(clip)], str(main), cache_dir=root / "cache", continue_on_error=True)
            with patch("audio_sync._duration", return_value=60), patch.object(
                _EnergyCache, "energy", return_value=np.zeros(60 * 50, dtype=np.float32)
            ), self.assertRaises(RuntimeError):
                align_audio_segments_fast("ffmpeg", [str(clip)], str(main), cache_dir=root / "cache", continue_on_error=True)

    def test_no_numpy_delegates_to_middle_waveform_compatibility(self):
        original_import = builtins.__import__

        def without_numpy(name, *args, **kwargs):
            if name == "numpy":
                raise ImportError("test without NumPy")
            return original_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="sync_no_numpy_") as folder:
            root = Path(folder)
            main, good, silent = [root / name for name in ("master.wav", "good.wav", "silent.wav")]
            for path in (main, good, silent):
                path.touch()
            from editor_core import AlignmentResult
            expected = [AlignmentResult(-10, 0.9, 50, "中段波形"),
                        AlignmentResult(0, 0, 50, "待手动：无声")]
            with patch("audio_sync_fallback.align_without_numpy", return_value=expected) as fallback, \
                    patch("builtins.__import__", side_effect=without_numpy):
                result = align_audio_segments_fast("ffmpeg", [str(good), str(silent)], str(main), continue_on_error=True)
            self.assertEqual(result, expected)
            fallback.assert_called_once()
            self.assertEqual(fallback.call_args.kwargs['mode'], 'fast')
            self.assertTrue(fallback.call_args.kwargs['continue_on_error'])


if __name__ == "__main__":
    unittest.main()
