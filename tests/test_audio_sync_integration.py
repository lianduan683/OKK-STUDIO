"""Independent real-audio checks for fast synchronization and its cache.

The fixtures have random, reproducible changes in energy. They are not constant
tones, and FFmpeg actually decodes FLAC, AAC and PCM recordings in these tests.
No user media is opened by this test module.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import numpy as np
except ImportError:
    np = None

from audio_sync import align_audio_segments_fast
from exporter_core import ExportCancelled, find_binary


@unittest.skipIf(np is None, "NumPy unavailable")
class RealAudioSyncTests(unittest.TestCase):
    RATE = 8000

    @classmethod
    def setUpClass(cls):
        app_dir = Path(__file__).resolve().parents[1]
        cls.ffmpeg = find_binary("ffmpeg", app_dir)
        if not cls.ffmpeg:
            raise unittest.SkipTest("FFmpeg unavailable")
        cls.temp = tempfile.TemporaryDirectory(prefix="audio_sync_real_")
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "underlying recording.wav"
        cls.source_pcm = cls.signal(96, 7331)
        cls.write_wave(cls.source, cls.source_pcm)
        cls.master = cls.root / "master.flac"
        cls.trim(cls.source, cls.master, 12, 72, ["-c:a", "flac"])
        cls.after = cls.root / "camera after master.m4a"
        cls.trim(cls.source, cls.after, 32.24, 28.64, ["-c:a", "aac", "-ar", "16000", "-b:a", "96k"])
        cls.before = cls.root / "camera before master.wav"
        cls.trim(cls.source, cls.before, 6, 36, ["-c:a", "pcm_s16le"])
        cls.late = cls.root / "camera late in master.wav"
        cls.trim(cls.source, cls.late, 67.16, 16, ["-c:a", "pcm_s16le"])
        cls.multiwindow = cls.root / "long camera with consistent windows.wav"
        cls.trim(cls.source, cls.multiwindow, 20, 60, ["-c:a", "pcm_s16le"])
        cls.silent = cls.root / "silence.wav"
        cls.write_wave(cls.silent, np.zeros(24 * cls.RATE, dtype="<i2"))
        cls.quiet = cls.root / "nearly silent unrelated.wav"
        rng = np.random.default_rng(999)
        cls.write_wave(cls.quiet, rng.integers(-1, 2, 24 * cls.RATE, dtype=np.int16))
        cls.unrelated = cls.root / "different performance.wav"
        cls.write_wave(cls.unrelated, cls.signal(28, 9207))
        cls.repeated_master = cls.root / "repeated takes master.wav"
        cls.repeated_camera = cls.root / "one repeated take.wav"
        repeated = cls.signal(20, 443)
        cls.write_wave(cls.repeated_camera, repeated)
        cls.write_wave(cls.repeated_master, np.concatenate([
            cls.signal(8, 11), repeated, cls.signal(8, 12), repeated, cls.signal(8, 13),
        ]))

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    @classmethod
    def signal(cls, seconds, seed):
        rng = np.random.default_rng(seed)
        size = round(seconds * cls.RATE)
        block = round(0.08 * cls.RATE)
        # Random amplitude, carrier mixture and low-level room noise produce a
        # nonperiodic envelope with enough structure for an independent match.
        levels = rng.uniform(0.05, 1.0, math.ceil(size / block)) ** 1.3
        amplitude = np.repeat(levels, block)[:size]
        time = np.arange(size, dtype=np.float64) / cls.RATE
        carrier = (np.sin(2 * np.pi * 211 * time)
                   + 0.37 * np.sin(2 * np.pi * 337 * time + 0.3)
                   + 0.19 * np.sin(2 * np.pi * 617 * time + 1.1))
        return np.rint(11500 * amplitude * carrier + rng.normal(0, 45, size)).astype("<i2")

    @classmethod
    def write_wave(cls, path, samples):
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, cls.RATE, 0, "NONE", "not compressed"))
            stream.writeframes(np.asarray(samples, dtype="<i2").tobytes())

    @classmethod
    def trim(cls, source, output, start, duration, codec):
        command = [cls.ffmpeg, "-v", "error", "-y", "-ss", str(start), "-t", str(duration),
                   "-i", str(source), "-map", "0:a:0", "-vn", *codec, str(output)]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", "replace")[-3000:])

    def align(self, videos, master=None, mode="fast", cache_dir=None, **options):
        return align_audio_segments_fast(
            self.ffmpeg, [str(path) for path in videos], str(master or self.master),
            mode=mode, cache_dir=str(cache_dir) if cache_dir else None, **options,
        )

    def assert_rejected_or_untrusted(self, camera, master=None):
        try:
            result = self.align([camera], master=master)[0]
        except RuntimeError:
            return
        self.assertLess(result.confidence, 0.1,
                        f"ambiguous/low-signal audio was trusted: {result}")

    def test_fast_mode_finds_both_offset_signs_after_real_codec_decode(self):
        results = self.align([self.after, self.before])
        self.assertEqual(len(results), 2)
        for result, expected in zip(results, [-20.24, 6.0]):
            with self.subTest(offset=expected):
                self.assertAlmostEqual(result.offset_seconds, expected, delta=0.08)
                self.assertGreater(result.confidence, 0.5)

    def test_short_camera_clip_can_match_the_end_of_a_longer_master(self):
        result = self.align([self.late])[0]
        self.assertAlmostEqual(result.offset_seconds, -55.16, delta=0.08)
        self.assertGreater(result.confidence, 0.5)

    def test_multiple_sample_windows_agree_on_one_global_offset(self):
        result = self.align([self.multiwindow])[0]
        self.assertAlmostEqual(result.offset_seconds, -8, delta=0.08)
        self.assertGreater(result.confidence, 0.5)

    def test_same_path_uses_zero_offset_but_silent_source_is_not_trusted(self):
        result = self.align([self.master])[0]
        self.assertAlmostEqual(result.offset_seconds, 0, places=6)
        self.assertGreater(result.confidence, 0.9)
        self.assert_rejected_or_untrusted(self.silent, master=self.silent)

    def test_silent_and_nearly_silent_audio_are_not_reported_as_reliable(self):
        for path in [self.silent, self.quiet]:
            with self.subTest(path=path.name):
                self.assert_rejected_or_untrusted(path)

    def test_two_identical_takes_in_master_are_not_assigned_high_confidence(self):
        self.assert_rejected_or_untrusted(self.repeated_camera, master=self.repeated_master)

    def test_an_unrelated_performance_is_not_assigned_high_confidence(self):
        self.assert_rejected_or_untrusted(self.unrelated)

    def test_full_mode_agrees_with_fast_mode_for_a_unique_overlap(self):
        fast = self.align([self.after])[0]
        full = self.align([self.after], mode="full")[0]
        self.assertAlmostEqual(fast.offset_seconds, full.offset_seconds, delta=0.06)
        self.assertAlmostEqual(full.offset_seconds, -20.24, delta=0.08)
        self.assertGreater(full.confidence, 0.5)

    def test_second_run_reuses_cached_features_and_preserves_the_result(self):
        with tempfile.TemporaryDirectory(prefix="audio_sync_cache_") as folder:
            cache = Path(folder)
            cold = self.align([self.after], cache_dir=cache)[0]
            files = {path: path.stat().st_mtime_ns for path in cache.rglob("*.npz")}
            self.assertTrue(files, "synchronization did not write its feature cache")
            warm = self.align([self.after], cache_dir=cache)[0]
            self.assertAlmostEqual(warm.offset_seconds, cold.offset_seconds, places=6)
            self.assertAlmostEqual(warm.confidence, cold.confidence, places=6)
            self.assertEqual({path: path.stat().st_mtime_ns for path in files}, files)
            self.assertIn("缓存", warm.engine)

    def test_changed_audio_invalidates_cached_features(self):
        with tempfile.TemporaryDirectory(prefix="audio_sync_replace_") as folder:
            root = Path(folder)
            camera = root / "camera.wav"
            cache = root / "cache"
            self.trim(self.source, camera, 32.24, 28.64, ["-c:a", "pcm_s16le"])
            first = self.align([camera], cache_dir=cache)[0]
            self.assertAlmostEqual(first.offset_seconds, -20.24, delta=0.08)
            stamp = camera.stat().st_mtime_ns
            self.trim(self.source, camera, 37.24, 28.64, ["-c:a", "pcm_s16le"])
            os.utime(camera, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))
            changed = self.align([camera], cache_dir=cache)[0]
            self.assertAlmostEqual(changed.offset_seconds, -25.24, delta=0.08)

    def test_pre_cancelled_work_stops_without_returning_a_false_match(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(ExportCancelled):
            self.align([self.after], cancel_event=cancelled)


if __name__ == "__main__":
    unittest.main()
