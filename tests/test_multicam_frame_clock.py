"""Canonical frame clocks and conservative metadata eligibility checks."""
from __future__ import annotations

from fractions import Fraction
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exporter_core import CREATE_NO_WINDOW, ExportCancelled, find_binary
from multicam_frame_clock import canonical_frame_rate, probe_partition_rates


class CanonicalFrameRateTests(unittest.TestCase):
    def test_rounded_ui_ntsc_rates_recover_the_exact_clock(self):
        for value, expected in ((23.976024, Fraction(24000, 1001)),
                                (29.97003, Fraction(30000, 1001)),
                                (47.952048, Fraction(48000, 1001)),
                                (59.94006, Fraction(60000, 1001)),
                                (119.88012, Fraction(120000, 1001))):
            with self.subTest(value=value):
                self.assertEqual(canonical_frame_rate(value), expected)

    def test_custom_rates_outside_the_tolerance_remain_custom(self):
        for value in (29.97, 59.94, 24, 25, 30, 60, 47.99, Fraction(12345, 271)):
            with self.subTest(value=value):
                self.assertEqual(canonical_frame_rate(value), Fraction(value).limit_denominator(100000))
        ntsc = float(Fraction(60000, 1001))
        self.assertEqual(canonical_frame_rate(ntsc + .999e-6), Fraction(60000, 1001))
        self.assertNotEqual(canonical_frame_rate(ntsc + 1.001e-6), Fraction(60000, 1001))

    def test_invalid_clocks_are_rejected(self):
        for value in (0, -30, float("nan"), float("inf"), True, "30", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_frame_rate(value)


class PartitionRateProbeTests(unittest.TestCase):
    @staticmethod
    def response(average="30/1", nominal="30/1", time_base="1/15360", **extra):
        stream = dict(avg_frame_rate=average, r_frame_rate=nominal, time_base=time_base)
        stream.update(extra)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": [stream]}), stderr="")

    def test_exact_cfr_metadata_returns_source_fractions_and_probes_each_path_once(self):
        results = [self.response(), self.response("60000/1001", "120000/2002", "1/60000")]
        with patch("multicam_frame_clock.subprocess.run", side_effect=results) as run:
            actual = probe_partition_rates("ffprobe.exe", ["a.mov", "b.mp4", "a.mov"])
        self.assertEqual(actual, {"a.mov": Fraction(30), "b.mp4": Fraction(60000, 1001)})
        self.assertEqual(run.call_count, 2)
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[command.index("-select_streams") + 1], "v:0")
        self.assertIn("avg_frame_rate,r_frame_rate,time_base", command[command.index("-show_entries") + 1])
        self.assertEqual(command[-1], "a.mov")
        self.assertNotIn("-show_frames", command)
        self.assertNotIn("-show_packets", command)

    def test_unknown_or_nonintegral_clocks_and_attached_picture_are_skipped(self):
        responses = [self.response("29/1", "30/1"),
                     self.response("30/1", "30/1", "1/1000"),
                     self.response("30000/1001", "30000/1001", "1/1000"),
                     self.response("0/0"), self.response(time_base="0/1"),
                     self.response(time_base="1/1"), self.response(disposition={"attached_pic": 1}),
                     SimpleNamespace(returncode=0, stdout='{"streams": []}'),
                     self.response()]
        paths = [f"camera_{index}" for index in range(len(responses))]
        with patch("multicam_frame_clock.subprocess.run", side_effect=responses):
            self.assertEqual(probe_partition_rates("ffprobe", paths), {paths[-1]: Fraction(30)})

    def test_malformed_metadata_and_failed_probes_do_not_block_other_sources(self):
        responses = [SimpleNamespace(returncode=1, stdout=""),
                     SimpleNamespace(returncode=0, stdout="invalid json"),
                     SimpleNamespace(returncode=0, stdout="[]"),
                     SimpleNamespace(returncode=0, stdout='{"streams": [null]}'),
                     self.response(average="N/A"),
                     FileNotFoundError("missing ffprobe"),
                     subprocess.TimeoutExpired("ffprobe", 15),
                     self.response()]
        paths = [f"camera_{index}" for index in range(len(responses))]
        with patch("multicam_frame_clock.subprocess.run", side_effect=responses):
            self.assertEqual(probe_partition_rates("ffprobe", paths), {paths[-1]: Fraction(30)})

    def test_cancel_before_metadata_work_raises_even_for_empty_paths(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch("multicam_frame_clock.subprocess.run") as run:
            for paths in ([], ["camera.mov"]):
                with self.subTest(paths=paths), self.assertRaises(ExportCancelled):
                    probe_partition_rates("ffprobe", paths, cancelled)
        run.assert_not_called()

    def test_cancel_during_or_after_a_failed_probe_is_not_swallowed(self):
        for failure in (False, True):
            cancelled = threading.Event()

            def cancel(*args, **kwargs):
                cancelled.set()
                if failure:
                    raise subprocess.TimeoutExpired("ffprobe", 15)
                return self.response()

            with self.subTest(failure=failure), patch("multicam_frame_clock.subprocess.run", side_effect=cancel):
                with self.assertRaises(ExportCancelled):
                    probe_partition_rates("ffprobe", ["camera.mov", "never.mov"], cancelled)


class ActualPartitionMetadataTests(unittest.TestCase):
    def test_short_cpu_encoded_camera_clock_is_eligible(self):
        app = Path(__file__).resolve().parents[1]
        ffmpeg, ffprobe = find_binary("ffmpeg", app), find_binary("ffprobe", app)
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg unavailable")
        with tempfile.TemporaryDirectory(prefix="multicam_clock_") as folder:
            camera = Path(folder) / "two_frames.mov"
            result = subprocess.run(
                [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=64x64:r=30",
                 "-frames:v", "2", "-c:v", "libx264", "-preset", "ultrafast", str(camera)],
                capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            self.assertEqual(probe_partition_rates(ffprobe, [str(camera)]), {str(camera): Fraction(30)})


if __name__ == "__main__":
    unittest.main()
