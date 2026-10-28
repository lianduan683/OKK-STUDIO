"""Independent, real-FFmpeg regression checks for middle-window alignment.

Fixtures are temporary synthetic recordings with reproducible, nonperiodic
waveforms. No user recordings or their existing synchronization cache are read.
"""
from __future__ import annotations

import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import numpy as np
except ImportError:
    np = None

import audio_sync
from editor_core import _normalized_fft_correlation, _normalized_fft_correlation_python
from exporter_core import find_binary


@unittest.skipIf(np is None, "NumPy unavailable")
class MiddleWindowSyncTests(unittest.TestCase):
    RATE = 8000
    CAMERA_SECONDS = 122.0

    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = find_binary("ffmpeg", Path(__file__).resolve().parents[1])
        if not cls.ffmpeg:
            raise unittest.SkipTest("FFmpeg unavailable")
        cls.temp = tempfile.TemporaryDirectory(prefix="middle_sync_real_")
        cls.root = Path(cls.temp.name)
        source = cls.signal(240, 4581)
        cls.master = cls.root / "continuous master.wav"
        cls.write_wave(cls.master, source[30 * cls.RATE:230 * cls.RATE])
        cls.before = cls.root / "camera starts before master.wav"
        cls.write_wave(cls.before, source[12 * cls.RATE:134 * cls.RATE])
        after_pcm = source[round(50.24 * cls.RATE):round(172.24 * cls.RATE)]
        after_wave = cls.root / "camera after original.wav"
        cls.write_wave(after_wave, after_pcm)
        cls.after = cls.root / "camera after with AAC.mp4"
        command = [cls.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                   "color=c=black:s=16x16:r=1:d=122", "-i", str(after_wave),
                   "-c:v", "mpeg4", "-q:v", "31", "-c:a", "aac", "-ar", "16000",
                   "-b:a", "64k", "-shortest", str(cls.after)]
        converted = subprocess.run(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=30)
        if converted.returncode:
            raise AssertionError(converted.stderr.decode("utf-8", "replace")[-3000:])
        cls.silent = cls.root / "silent long camera.wav"
        cls.write_wave(cls.silent, np.zeros(round(cls.CAMERA_SECONDS * cls.RATE), dtype="<i2"))
        cls.middle_silent_camera = cls.root / "camera with silent center.wav"
        cls.middle_silent_master = cls.root / "master with same silent center.wav"
        center_gap_camera = after_pcm.copy()
        center_gap_master = source[30 * cls.RATE:230 * cls.RATE].copy()
        gap_start, gap_end = 35.0, 87.0
        center_gap_camera[round(gap_start * cls.RATE):round(gap_end * cls.RATE)] = 0
        center_gap_master[round((20.24 + gap_start) * cls.RATE):round((20.24 + gap_end) * cls.RATE)] = 0
        cls.write_wave(cls.middle_silent_camera, center_gap_camera)
        cls.write_wave(cls.middle_silent_master, center_gap_master)
        cls.unrelated = cls.root / "unrelated long camera.wav"
        cls.write_wave(cls.unrelated, cls.signal(cls.CAMERA_SECONDS, 8862))
        cls.repeated_master = cls.root / "master contains two identical takes.wav"
        cls.repeated_camera = cls.root / "camera one identical take.wav"
        repeated = cls.signal(cls.CAMERA_SECONDS, 7712)
        cls.write_wave(cls.repeated_camera, repeated)
        cls.write_wave(cls.repeated_master, np.concatenate([
            cls.signal(8, 1), repeated, cls.signal(8, 2), repeated, cls.signal(8, 3),
        ]))

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    @classmethod
    def signal(cls, seconds, seed):
        rng = np.random.default_rng(seed)
        count = round(seconds * cls.RATE)
        block = round(0.08 * cls.RATE)
        level = np.repeat(rng.uniform(0.04, 1.0, math.ceil(count / block)) ** 1.3, block)[:count]
        time = np.arange(count, dtype=np.float64) / cls.RATE
        carrier = (np.sin(2 * np.pi * 211 * time)
                   + 0.37 * np.sin(2 * np.pi * 337 * time + 0.3)
                   + 0.19 * np.sin(2 * np.pi * 617 * time + 1.1))
        return np.rint(11500 * level * carrier + rng.normal(0, 40, count)).astype("<i2")

    @classmethod
    def write_wave(cls, path, samples):
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, cls.RATE, 0, "NONE", "not compressed"))
            stream.writeframes(np.asarray(samples, dtype="<i2").tobytes())

    def setUp(self):
        self.cache_temp = tempfile.TemporaryDirectory(prefix="middle_sync_cache_")
        self.addCleanup(self.cache_temp.cleanup)

    def align(self, camera, master=None, **kwargs):
        return audio_sync.align_audio_segments_fast(
            self.ffmpeg, [str(camera)], str(master or self.master),
            cache_dir=self.cache_temp.name, **kwargs,
        )[0]

    def assert_untrusted(self, camera, master=None, **kwargs):
        try:
            result = self.align(camera, master, **kwargs)
        except RuntimeError:
            return
        self.assertLess(result.confidence, 0.1, f"Unreliable recording was trusted: {result}")

    def test_real_mp4_uses_one_center_window_and_restores_whole_camera_offset(self):
        with patch("audio_sync._stream_energy", wraps=audio_sync._stream_energy) as decode:
            result = self.align(self.after)
        self.assertAlmostEqual(result.offset_seconds, -20.24, delta=0.08)
        self.assertGreater(result.confidence, 0.5)
        camera_reads = [call for call in decode.call_args_list if Path(call.args[1]) == self.after]
        self.assertEqual(len(camera_reads), 1, "Normal fast mode must decode one middle camera window")
        start = camera_reads[0].kwargs.get("start", 0.0)
        duration = camera_reads[0].kwargs.get("duration")
        self.assertIsNotNone(duration, "Fast camera alignment decoded the entire recording")
        self.assertLessEqual(duration, 60)
        self.assertAlmostEqual(start + duration / 2, self.CAMERA_SECONDS / 2, delta=0.08)

    def test_camera_before_master_keeps_positive_delay_and_negative_timeline_start(self):
        result = self.align(self.before)
        self.assertAlmostEqual(result.offset_seconds, 18.0, delta=0.06)
        self.assertGreater(result.confidence, 0.5)
        self.assertAlmostEqual(-result.offset_seconds, -18.0, delta=0.06)

    def test_maximum_applies_to_whole_recording_offset_not_probe_lag(self):
        for camera, maximum, expected in [(self.after, 21, -20.24), (self.before, 19, 18)]:
            with self.subTest(camera=camera.name):
                result = self.align(camera, max_offset_seconds=maximum)
                self.assertAlmostEqual(result.offset_seconds, expected, delta=0.08)
                self.assertGreater(result.confidence, 0.5)

    def test_actual_offset_outside_maximum_is_not_trusted(self):
        self.assert_untrusted(self.after, max_offset_seconds=10)

    def test_repeated_take_in_master_requires_manual_confirmation(self):
        self.assert_untrusted(self.repeated_camera, self.repeated_master)

    def test_unrelated_recording_is_not_trusted(self):
        self.assert_untrusted(self.unrelated)

    def test_silent_middle_reports_failure_without_full_camera_decode(self):
        with patch("audio_sync._stream_energy", wraps=audio_sync._stream_energy) as decode:
            result = self.align(self.silent, continue_on_error=True)
        self.assertEqual(result.confidence, 0)
        self.assertIn("待手动", result.engine)
        camera_reads = [call for call in decode.call_args_list if Path(call.args[1]) == self.silent]
        self.assertTrue(camera_reads)
        self.assertTrue(all(call.kwargs.get("duration") is not None for call in camera_reads),
                        "Silent middle window silently fell back to a full camera decode")

    def test_warm_cache_preserves_middle_result_without_decoder(self):
        cold = self.align(self.after)
        with patch("audio_sync._stream_energy", side_effect=AssertionError("Warm cache redecoded audio")):
            warm = self.align(self.after)
        self.assertAlmostEqual(warm.offset_seconds, cold.offset_seconds, places=6)
        self.assertAlmostEqual(warm.confidence, cold.confidence, places=6)
        self.assertIn("缓存", warm.engine)

    def test_silent_middle_can_choose_a_nearby_window_without_decoding_the_whole_camera(self):
        with patch("audio_sync._stream_energy", wraps=audio_sync._stream_energy) as decode:
            result = self.align(self.middle_silent_camera, self.middle_silent_master)
        self.assertAlmostEqual(result.offset_seconds, -20.24, delta=0.08)
        self.assertGreater(result.confidence, 0.5)
        camera_reads = [call for call in decode.call_args_list
                        if Path(call.args[1]) == self.middle_silent_camera]
        self.assertGreaterEqual(len(camera_reads), 2)
        self.assertLessEqual(len(camera_reads), 3)
        self.assertTrue(all(call.kwargs.get("duration") is not None for call in camera_reads))

    def test_both_fft_engines_honor_asymmetric_local_bounds(self):
        # The exact match is deliberately outside the allowed lag interval.
        # Selecting it and rejecting afterwards would discard an allowed match.
        rng = np.random.default_rng(741)
        master = rng.normal(0, 1, 400)
        camera = master[150:250].copy()
        options = dict(min_lag=-300, max_lag=-200, min_overlap_seconds=8,
                       return_second_peak=True)
        numpy_result = _normalized_fft_correlation(camera, master, 10, 0, **options)
        python_result = _normalized_fft_correlation_python(camera.tolist(), master.tolist(), 10, 0, **options)
        for lag, _score, _second in [numpy_result, python_result]:
            self.assertGreaterEqual(lag, -300)
            self.assertLessEqual(lag, -200)
        self.assertEqual(numpy_result[0], python_result[0])
        self.assertAlmostEqual(numpy_result[1], python_result[1], places=9)


if __name__ == "__main__":
    unittest.main()
