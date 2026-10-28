"""Real lossless-video regression for additional render-job boundaries."""
from __future__ import annotations

import hashlib
import math
from fractions import Fraction
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exporter_core import CREATE_NO_WINDOW, Capabilities, ExportSettings, find_binary
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam
from multicam_render_plan import partition_render_segments


class PartitionMediaTests(unittest.TestCase):
    width, height = 128, 72

    @classmethod
    def setUpClass(cls):
        app_dir = Path(__file__).resolve().parents[1]
        cls.ffmpeg = find_binary("ffmpeg", app_dir)
        cls.ffprobe = find_binary("ffprobe", app_dir)
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest("FFmpeg and ffprobe required")
        cls.temp = tempfile.TemporaryDirectory(prefix="multicam_partition_media_")
        cls.root = Path(cls.temp.name)
        cls.sources = {}
        for label, rate in (("30", 30.0), ("2997", 30000/1001),
                            ("5994", 60000/1001), ("25", 25.0)):
            frames = math.ceil((75 if label == "30" else 4)*rate)
            # Every frame has a unique moving luminance pattern and chroma.
            # Planar YUV avoids any RGB conversion or color-range tolerance.
            raw = bytearray()
            for index in range(frames):
                raw.extend((16+(x*3+y*5+index*17) % 220
                            for y in range(cls.height) for x in range(cls.width)))
                raw.extend(bytes([80+index*3 % 80]) * (cls.width*cls.height//4))
                raw.extend(bytes([90+index*7 % 70]) * (cls.width*cls.height//4))
            path = cls.root / ("source_"+label+".mp4")
            rate_text = {"2997": "30000/1001", "5994": "60000/1001"}.get(label, label)
            cls.command([cls.ffmpeg, "-v", "error", "-y", "-f", "rawvideo",
                         "-pixel_format", "yuv420p", "-video_size", "128x72",
                         "-framerate", rate_text, "-i", "pipe:0", "-an", "-c:v", "libx264",
                         "-preset", "ultrafast", "-crf", "0", "-g", "30", "-bf", "0",
                         "-threads", "1", str(path)], input=bytes(raw))
            cls.sources[label] = (path, frames/rate)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    @staticmethod
    def command(args, **kwargs):
        process = subprocess.run(args, capture_output=True, timeout=30,
                                 creationflags=CREATE_NO_WINDOW, **kwargs)
        if process.returncode:
            raise AssertionError(process.stderr.decode("utf-8", "replace")[-4000:])
        return process.stdout

    @staticmethod
    def runner(command, report):
        process = subprocess.run(command, capture_output=True, timeout=30,
                                 creationflags=CREATE_NO_WINDOW)
        packet = {}
        for line in process.stdout.decode("utf-8", "replace").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                packet[key] = value
                if key == "progress":
                    report(packet)
                    packet = {}
        return process.returncode, process.stderr.decode("utf-8", "replace")

    def render_frames(self, project, segments, name):
        output = self.root / (name+".mp4")
        settings = ExportSettings(codec="libx264", preset="ultrafast", quality=0,
                                  audio="none", decode_mode="cpu", parallel_workers=2,
                                  bframes=0, extra_args=["-threads", "1"])
        # Exercise precisely the existing renderer with only its job list
        # replaced. No dependence on whether the root has integrated planning.
        with patch.object(MulticamProject, "segments", return_value=segments), \
                patch("multicam_core.partition_render_segments",
                      side_effect=lambda values, *args, **kwargs: list(values), create=True), \
                patch("multicam_core.probe_partition_rates", return_value={}, create=True):
            render_multicam(project, output, self.ffmpeg, self.ffprobe,
                            settings, Capabilities(), self.runner)
        raw = self.command([self.ffmpeg, "-v", "error", "-i", str(output),
                            "-map", "0:v:0", "-an", "-pix_fmt", "yuv420p",
                            "-f", "rawvideo", "pipe:1"])
        size = self.width*self.height*3//2
        self.assertEqual(len(raw) % size, 0)
        return [hashlib.sha256(raw[offset:offset+size]).hexdigest()
                for offset in range(0, len(raw), size)]

    def test_added_job_boundaries_do_not_change_decoded_programme_frames(self):
        variants = (("30", 60000/1001), ("2997", 60000/1001),
                    ("25", 30.0), ("30", 30.0),
                    ("5994", 60000/1001), ("2997", 30000/1001))
        for source_rate, output_rate in variants:
            path, duration = self.sources[source_rate]
            for source_start in (0.0, 0.017):
                project = MulticamProject(
                    [CameraClip("camera", 1, str(path), -source_start, duration)],
                    [Cut(0, 1)], fps=output_rate, range_end=2.6)
                originals = project.segments()
                label = f"src{source_rate}_out{output_rate:.6f}_start{source_start:.3f}"
                expected = self.render_frames(project, originals, label+"_continuous")
                for chunk_seconds in (1.0, 1.03):
                    with self.subTest(source_rate=source_rate, output_rate=output_rate,
                                      source_start=source_start, chunk_seconds=chunk_seconds):
                        source_fps = {"30": Fraction(30), "25": Fraction(25),
                                      "2997": Fraction(30000, 1001),
                                      "5994": Fraction(60000, 1001)}[source_rate]
                        jobs = partition_render_segments(originals, output_rate, 2, chunk_seconds,
                                                         source_frame_rates={str(path): source_fps})
                        actual = self.render_frames(project, jobs, label+f"_chunk{chunk_seconds}")
                        self.assertEqual(len(actual), len(expected), "Total frame count changed")
                        mismatches = [i for i, (a, b) in enumerate(zip(actual, expected)) if a != b]
                        seams, cumulative = [], 0
                        for job in jobs[:-1]:
                            cumulative += job.frames
                            seams.append(cumulative)
                        self.assertFalse(mismatches,
                            f"Added seams {seams} changed {len(mismatches)}/{len(expected)} frames; "
                            f"first differing output indices={mismatches[:24]}")

    def test_30_to_5994_long_conversion_period_preserves_every_frame(self):
        path, duration = self.sources["30"]
        fps = Fraction(60000, 1001)
        for source_start in (0.0, 0.017):
            with self.subTest(source_start=source_start):
                project = MulticamProject(
                    [CameraClip("camera", 1, str(path), -source_start, duration)],
                    [Cut(0, 1)], fps=float(fps), range_end=71)
                originals = project.segments()
                jobs = partition_render_segments(originals, fps, 2, 60,
                                                 source_frame_rates={str(path): Fraction(30)})
                self.assertEqual([job.frames for job in jobs], [2000, 2000, 256])
                label = f"long30_to5994_{source_start}"
                expected = self.render_frames(project, originals, label+"_continuous")
                actual = self.render_frames(project, jobs, label+"_partitioned")
                self.assertEqual(len(actual), len(expected))
                mismatches = [i for i, (a, b) in enumerate(zip(actual, expected)) if a != b]
                self.assertFalse(mismatches,
                                 f"Conversion-period seams at 2000/4000 changed "
                                 f"{len(mismatches)} frames; first indices={mismatches[:20]}")

    def test_unverified_source_keeps_continuous_pixels(self):
        path, duration = self.sources["2997"]
        project = MulticamProject(
            [CameraClip("camera", 1, str(path), -0.017, duration)],
            [Cut(0, 1)], fps=60000/1001, range_end=2.6)
        originals = project.segments()
        jobs = partition_render_segments(originals, project.fps, 2, 1,
                                         source_frame_rates={})
        self.assertIs(jobs[0], originals[0])
        expected = self.render_frames(project, originals, "unknown_continuous")
        actual = self.render_frames(project, jobs, "unknown_planned")
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
