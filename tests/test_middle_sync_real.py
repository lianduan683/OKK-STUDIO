"""Independent codec-level regressions for middle-only waveform alignment.

Only reproducible synthetic PCM fixtures are opened. The separate work/
profile_middle_sync_readonly.py script profiles the selected desktop media.
"""
from __future__ import annotations

import math
from pathlib import Path
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
from exporter_core import find_binary


@unittest.skipIf(np is None, "NumPy unavailable")
class MiddleWaveformCodecTests(unittest.TestCase):
    RATE = 4000

    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = find_binary("ffmpeg", Path(__file__).resolve().parents[1])
        if not cls.ffmpeg:
            raise unittest.SkipTest("FFmpeg unavailable")
        cls.temp = tempfile.TemporaryDirectory(prefix="middle_waveform_codec_")
        cls.root = Path(cls.temp.name)
        rng = np.random.default_rng(29171)
        count = 480 * cls.RATE
        block = cls.RATE // 10
        levels = rng.uniform(0.04, 1.0, math.ceil(count / block)) ** 1.4
        amplitude = np.repeat(levels, block)[:count]
        t = np.arange(count, dtype=np.float64) / cls.RATE
        carrier = np.sin(2 * np.pi * 211 * t) + 0.37 * np.sin(2 * np.pi * 337 * t + 0.3)
        signal = np.rint(12000 * amplitude * carrier + rng.normal(0, 35, count)).astype("<i2")
        cls.master = cls.root / "continuous master.wav"
        cls.write(cls.master, signal[40 * cls.RATE:460 * cls.RATE])
        cls.after = cls.root / "camera after master.wav"
        cls.write(cls.after, signal[round(137.24 * cls.RATE):round(337.24 * cls.RATE)])
        cls.before = cls.root / "camera before master.wav"
        cls.write(cls.before, signal[:250 * cls.RATE])
        cls.middle_silent = cls.root / "silent centre with useful adjacent audio.wav"
        local = signal[100 * cls.RATE:300 * cls.RATE].copy()
        local[round(77.5 * cls.RATE):round(122.5 * cls.RATE)] = 0
        cls.write(cls.middle_silent, local)
        cls.middle_area_silent = cls.root / "all middle candidates silent but active edges.wav"
        local = signal[100 * cls.RATE:400 * cls.RATE].copy()
        local[round(82.5 * cls.RATE):round(217.5 * cls.RATE)] = 0
        cls.write(cls.middle_area_silent, local)
        cls.repeated_master = cls.root / "two identical takes.wav"
        cls.repeated_camera = cls.root / "one take.wav"
        pattern = signal[:120 * cls.RATE]
        cls.write(cls.repeated_camera, pattern)
        cls.write(cls.repeated_master, np.concatenate([
            pattern, signal[120 * cls.RATE:180 * cls.RATE], pattern,
            signal[300 * cls.RATE:420 * cls.RATE],
        ]))

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    @classmethod
    def write(cls, path, samples):
        with wave.open(str(path), "wb") as output:
            output.setparams((1, 2, cls.RATE, 0, "NONE", "not compressed"))
            output.writeframes(np.asarray(samples, dtype="<i2").tobytes())

    def align_traced(self, camera, master=None, **options):
        camera = camera.resolve()
        original = audio_sync._EnergyCache.energy
        camera_requests = []

        def trace(cache, path, *, start=0.0, duration=None, **decode_options):
            if Path(path).resolve() == camera:
                self.assertIsNotNone(duration, "middle mode must never decode the complete camera")
                self.assertLessEqual(duration, 45.001)
                camera_requests.append((start, duration))
            return original(cache, path, start=start, duration=duration, **decode_options)

        cache = self.root / ("cache_" + self._testMethodName)
        with patch.object(audio_sync._EnergyCache, "energy", trace):
            results = audio_sync.align_audio_segments_fast(
                self.ffmpeg, [str(camera)], str(master or self.master),
                cache_dir=cache, **options)
        return results[0], camera_requests

    def test_real_pcm_single_middle_sample_preserves_both_offset_signs(self):
        for camera, expected, start in [(self.after, -97.24, 77.5), (self.before, 40.0, 102.5)]:
            with self.subTest(camera=camera.name):
                result, calls = self.align_traced(camera)
                self.assertAlmostEqual(result.offset_seconds, expected, delta=0.04)
                self.assertGreater(result.confidence, 0.8)
                self.assertEqual(calls, [(start, 45.0)])

    def test_offset_limit_is_applied_after_adding_middle_start(self):
        # The true global delay is -97.24 s, but the local middle-window lag
        # is -174.74 s. Applying the 100 s bound to local lag would reject it.
        result, calls = self.align_traced(self.after, max_offset_seconds=100)
        self.assertAlmostEqual(result.offset_seconds, -97.24, delta=0.04)
        self.assertGreater(result.confidence, 0.8)
        self.assertEqual(calls, [(77.5, 45.0)])

    def test_silent_centre_uses_only_bounded_adjacent_rescue(self):
        result, calls = self.align_traced(self.middle_silent)
        self.assertAlmostEqual(result.offset_seconds, -60.0, delta=0.04)
        self.assertGreater(result.confidence, 0.8)
        self.assertGreaterEqual(len(calls), 2)
        self.assertLessEqual(len(calls), 3)
        self.assertEqual(calls[0], (77.5, 45.0))
        self.assertTrue(all(abs(start - 77.5) <= 45.001 for start, _duration in calls))

    def test_silent_middle_area_returns_manual_instead_of_full_track_fallback(self):
        result, calls = self.align_traced(self.middle_area_silent, continue_on_error=True)
        self.assertEqual(result.confidence, 0.0)
        self.assertIn("待手动", result.engine)
        self.assertGreaterEqual(len(calls), 1)
        self.assertLessEqual(len(calls), 3)
        self.assertTrue(all(abs(start - 127.5) <= 45.001 for start, _duration in calls))

    def test_repeated_middle_phrase_does_not_receive_high_confidence(self):
        try:
            result, calls = self.align_traced(self.repeated_camera, master=self.repeated_master)
        except RuntimeError:
            return
        self.assertLess(result.confidence, 0.1)
        self.assertEqual(calls[0], (37.5, 45.0))
        self.assertLessEqual(len(calls), 3)
        self.assertTrue(all(abs(start - 37.5) <= 45.001 for start, _duration in calls))


if __name__ == "__main__":
    unittest.main()
