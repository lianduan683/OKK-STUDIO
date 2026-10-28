"""Multicamera editing regressions, including real CPU FFmpeg export.

Run: py -3 -m unittest discover -s tests -p test_multicam.py -v
The export fixtures use different camera frame rates, sizes, and native audio;
the rendered program must retain the selected pictures and continuous master.
"""
from __future__ import annotations

import array
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exporter_core import Capabilities, ExportCancelled, ExportSettings, find_binary
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam


def make_project(**overrides):
    values = dict(
        clips=[
            CameraClip("a", 1, "camera-a.mp4", -1.0, 3.0),
            CameraClip("b", 2, "camera-b.mp4", 0.0, 4.0),
        ],
        cuts=[Cut(0.0, 1), Cut(1.0, 2), Cut(2.0, 1)],
        master_audio="master.wav", master_duration=4.0, fps=30.0,
        range_start=0.0, range_end=4.0,
    )
    values.update(overrides)
    return MulticamProject(**values)


class MulticamTimelineTests(unittest.TestCase):
    def test_switch_begins_exactly_at_cut_and_clips_are_half_open(self):
        project = make_project()
        project.validate()
        self.assertEqual(project.camera_at(29 / 30), 1)
        self.assertEqual(project.camera_at(1.0), 2)
        self.assertEqual(project.camera_at(2.0), 1)
        self.assertEqual(project.clip_at(1, 0.0).id, "a")
        self.assertIsNone(project.clip_at(1, 2.0))

    def test_negative_alignment_offset_seeks_into_source(self):
        segments = make_project().segments()
        self.assertEqual(len(segments), 3)
        self.assertEqual([segment.path for segment in segments], ["camera-a.mp4", "camera-b.mp4", None])
        self.assertEqual([segment.frames for segment in segments], [30, 30, 60])
        self.assertAlmostEqual(segments[0].source_start, 1.0)
        self.assertAlmostEqual(segments[1].source_start, 1.0)
        self.assertAlmostEqual(segments[-1].end, 4.0)

    def test_selected_camera_gap_stays_black_even_when_another_camera_exists(self):
        project = make_project(
            clips=[CameraClip("a1", 1, "a1.mp4", 0, 1),
                   CameraClip("a2", 1, "a2.mp4", 2, 1),
                   CameraClip("b", 2, "b.mp4", 0, 3)],
            cuts=[Cut(0, 1)], master_duration=3, range_end=3,
        )
        segments = project.segments()
        self.assertEqual([(segment.start, segment.duration) for segment in segments], [(0, 1), (1, 1), (2, 1)])
        self.assertEqual([segment.path for segment in segments], ["a1.mp4", None, "a2.mp4"])
        self.assertIsNone(project.clip_at(1, 1.5))
        self.assertEqual(project.clip_at(2, 1.5).id, "b")

    def test_set_cut_replaces_same_time_and_keeps_chronological_behavior(self):
        project = make_project()
        project.set_cut(0.5, 2)
        project.set_cut(1.0, 1)
        self.assertEqual(project.camera_at(14 / 30), 1)
        self.assertEqual(project.camera_at(0.5), 2)
        self.assertEqual(project.camera_at(1.0), 1)
        times = [cut.time for cut in project.cuts]
        self.assertEqual(times, sorted(set(times)))
        with self.assertRaises(ValueError):
            project.set_cut(1.5, 99)

    def test_many_fractional_cuts_do_not_accumulate_frame_rounding(self):
        project = make_project(
            clips=[CameraClip("a", 1, "a.mp4", 0, 4), CameraClip("b", 2, "b.mp4", 0, 4)],
            cuts=[Cut(index * 0.1013, 1 + index % 2) for index in range(32)],
            fps=30000 / 1001, range_start=0.137, range_end=3.14159,
        )
        segments = project.segments()
        expected = math.floor((project.range_end - project.range_start) * project.fps + 0.5)
        self.assertEqual(sum(segment.frames for segment in segments), expected)
        self.assertTrue(all(segment.frames > 0 for segment in segments))

    def test_project_round_trip_resolves_media_relative_to_project(self):
        with tempfile.TemporaryDirectory(prefix="multicam_project_") as folder:
            root = Path(folder)
            (root / "projects").mkdir()
            project = make_project(
                clips=[CameraClip("a", 1, str(root / "素材 红.mp4"), -0.5, 3,
                                  str(root / "proxy" / "a.mp4")),
                       CameraClip("b", 2, str(root / "blue.mp4"), 0, 4)],
                master_audio=str(root / "录音.wav"),
            )
            path = root / "projects" / "演出.multicam.json"
            project.save(path)
            loaded = MulticamProject.load(path)
            self.assertEqual([(item.id, item.camera, item.start, item.duration) for item in loaded.clips],
                             [(item.id, item.camera, item.start, item.duration) for item in project.clips])
            self.assertEqual([Path(item.path).resolve() for item in loaded.clips],
                             [Path(item.path).resolve() for item in project.clips])
            self.assertEqual(Path(loaded.clips[0].proxy_path).resolve(), Path(project.clips[0].proxy_path).resolve())
            self.assertEqual(Path(loaded.master_audio).resolve(), Path(project.master_audio).resolve())
            self.assertEqual([(cut.time, cut.camera) for cut in loaded.cuts], [(0, 1), (1, 2), (2, 1)])
            self.assertEqual((loaded.fps, loaded.range_start, loaded.range_end), (30, 0, 4))

    def test_invalid_timeline_values_are_rejected(self):
        for field, value in [("fps", 0), ("fps", -24), ("fps", float("nan")),
                             ("fps", float("inf")), ("master_duration", -1),
                             ("range_start", float("nan")), ("range_end", -1)]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                make_project(**{field: value}).validate()
        for clips in [
            [CameraClip("same", 1, "a.mp4", 0, 1), CameraClip("same", 2, "b.mp4", 0, 1)],
            [CameraClip("bad", 1, "a.mp4", 0, -1)],
            [CameraClip("bad", 1, "a.mp4", float("inf"), 1)],
        ]:
            with self.subTest(clips=clips), self.assertRaises(ValueError):
                make_project(clips=clips).validate()
        with self.assertRaises(ValueError):
            make_project(cuts=[Cut(0, 99)]).validate()

    def test_malformed_project_is_rejected_without_materializing_bad_state(self):
        with tempfile.TemporaryDirectory(prefix="multicam_bad_project_") as folder:
            path = Path(folder) / "bad.json"
            for text in ["{not json", "[]", '{"version":999,"clips":[]}']:
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaises(ValueError):
                    MulticamProject.load(path)
            make_project().save(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["fps"] = 0
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                MulticamProject.load(path)


class FFmpegMulticamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_dir = Path(__file__).resolve().parents[1]
        cls.ffmpeg = find_binary("ffmpeg", app_dir)
        cls.ffprobe = find_binary("ffprobe", app_dir)
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest("FFmpeg/ffprobe unavailable")
        cls.temp = tempfile.TemporaryDirectory(prefix="multicam_export_")
        cls.root = Path(cls.temp.name)
        cls.red = cls.root / "camera red.mp4"
        cls.blue = cls.root / "camera blue.mp4"
        for path, color, size, rate, tone in [
            (cls.red, "red", "160x90", 25, 880),
            (cls.blue, "blue", "192x128", 30, 1350),
        ]:
            cls.run_command([
                cls.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                f"color={color}:s={size}:r={rate}:d=3", "-f", "lavfi", "-i",
                f"sine=frequency={tone}:sample_rate=48000:duration=3",
                "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "12", "-threads", "1", "-c:a", "aac", "-shortest", str(path),
            ])
        cls.master = cls.root / "continuous master.wav"
        cls.samples = array.array("h", (int(11000 * math.sin(2 * math.pi * (230 * n / 48000 + 17 * (n / 48000) ** 2)))
                                        for n in range(4 * 48000)))
        with wave.open(str(cls.master), "wb") as stream:
            stream.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            stream.writeframes(cls.samples.tobytes())

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    @staticmethod
    def run_command(command):
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", "replace")[-7000:])
        return result.stdout

    @classmethod
    def process(cls, command, callback=None):
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        return result.returncode, result.stdout.decode("utf-8", "replace")

    def project(self, **overrides):
        return make_project(
            clips=[CameraClip("red", 1, str(self.red), -0.5, 3),
                   CameraClip("blue", 2, str(self.blue), 1, 3)],
            cuts=[Cut(0, 1), Cut(1, 2), Cut(2, 1), Cut(3, 2)],
            master_audio=str(self.master), fps=24,
            **overrides,
        )

    def render(self, name, project=None, run_process=None, **options):
        output = self.root / name
        result = render_multicam(
            project or self.project(), str(output), self.ffmpeg, self.ffprobe,
            ExportSettings(codec="libx264", preset="veryfast", decode_mode="cpu", audio_bitrate="192k"),
            Capabilities(), run_process or self.process, **options,
        )
        self.assertEqual(Path(result).resolve(), output.resolve())
        return output

    def assert_color(self, output, timestamp, expected):
        raw = self.run_command([
            self.ffmpeg, "-v", "error", "-ss", str(timestamp), "-i", str(output),
            "-frames:v", "1", "-vf", "crop=iw/2:ih/2,scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
        ])
        self.assertEqual(len(raw), 3)
        self.assertTrue(all(abs(actual - wanted) < 14 for actual, wanted in zip(raw, expected)),
                        (timestamp, list(raw), expected))

    def assert_frames(self, output, count):
        payload = json.loads(self.run_command([
            self.ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=nb_read_frames,r_frame_rate,width,height", "-of", "json", str(output),
        ]))
        stream = payload["streams"][0]
        self.assertEqual(int(stream["nb_read_frames"]), count)
        numerator, denominator = map(float, stream["r_frame_rate"].split("/"))
        self.assertAlmostEqual(numerator / denominator, 24)

    def assert_continuous_master(self, output, master_start, duration):
        pcm = self.run_command([
            self.ffmpeg, "-v", "error", "-i", str(output), "-map", "0:a:0",
            "-ac", "1", "-ar", "48000", "-f", "s16le", "-",
        ])
        actual = array.array("h")
        actual.frombytes(pcm)
        self.assertGreaterEqual(len(actual), round(duration * 48000))
        # Check every side of every cut and the camera-off gap, excluding only
        # AAC's boundary transient. Independent camera tones fail this check.
        for start in [0.08 + index * 0.25 for index in range(int((duration - 0.2) / 0.25))]:
            begin = round(start * 48000)
            count = 4800
            observed = actual[begin:begin + count]
            expected_begin = round(master_start * 48000) + begin
            expected = self.samples[expected_begin:expected_begin + count]
            dot = sum(a * b for a, b in zip(observed, expected))
            norm = math.sqrt(sum(a * a for a in observed) * sum(b * b for b in expected))
            self.assertGreater(dot / max(norm, 1), 0.985, f"master audio discontinuity at {start:.3f}s")

    def test_export_switches_mixed_sources_keeps_black_gap_and_continuous_master(self):
        project = self.project()
        # Deliberately swap the preview proxies: final pictures must come from
        # original recordings, never from the low-resolution preview paths.
        project.clips[0].proxy_path = str(self.blue)
        project.clips[1].proxy_path = str(self.red)
        output = self.render("program.mp4", project)
        self.assert_frames(output, 96)
        for timestamp, expected in [(0.4, (255, 0, 0)), (1.4, (0, 0, 255)),
                                    (2.25, (255, 0, 0)), (2.75, (0, 0, 0)), (3.4, (0, 0, 255))]:
            self.assert_color(output, timestamp, expected)
        self.assert_continuous_master(output, 0, 4)

    def test_selected_range_preserves_master_time_and_frame_count(self):
        output = self.render("range.mp4", self.project(range_start=0.5, range_end=3.5))
        self.assert_frames(output, 72)
        for timestamp, expected in [(0.2, (255, 0, 0)), (0.7, (0, 0, 255)),
                                    (1.7, (255, 0, 0)), (2.2, (0, 0, 0)), (2.7, (0, 0, 255))]:
            self.assert_color(output, timestamp, expected)
        self.assert_continuous_master(output, 0.5, 3)

    def test_export_cannot_overwrite_source_or_master_audio(self):
        for source in [self.red, self.blue, self.master]:
            before = source.read_bytes()
            with self.subTest(source=source.name), self.assertRaises((ValueError, RuntimeError)):
                self.render(source.name)
            self.assertEqual(source.read_bytes(), before)

    def test_export_cannot_overwrite_preview_proxy(self):
        proxy = self.root / "preview proxy.mp4"
        proxy.write_bytes(b"preview proxy must survive")
        project = self.project()
        project.clips[0].proxy_path = str(proxy)
        with self.assertRaises((ValueError, RuntimeError)):
            self.render(proxy.name, project)
        self.assertEqual(proxy.read_bytes(), b"preview proxy must survive")

    def test_failed_export_preserves_existing_destination_and_cleans_workspace(self):
        output = self.root / "failed.mp4"
        output.write_bytes(b"previous completed export")
        before = set(self.root.iterdir())
        with self.assertRaises(RuntimeError):
            self.render(output.name, run_process=lambda command, callback=None: (1, "intentional encoder failure"))
        self.assertEqual(output.read_bytes(), b"previous completed export")
        self.assertEqual(set(self.root.iterdir()), before)

    def test_cancel_after_first_segment_preserves_destination_and_cleans_workspace(self):
        output = self.root / "cancelled.mp4"
        output.write_bytes(b"keep completed result")
        before = set(self.root.iterdir())
        cancelled = threading.Event()
        calls = []

        def cancel_after_segment(command, callback=None):
            calls.append(command)
            result = self.process(command, callback)
            cancelled.set()
            return result

        with self.assertRaises(ExportCancelled):
            self.render(output.name, run_process=cancel_after_segment, cancel_event=cancelled)
        self.assertEqual(len(calls), 1)
        self.assertEqual(output.read_bytes(), b"keep completed result")
        self.assertEqual(set(self.root.iterdir()), before)

    def test_pre_cancelled_export_does_not_launch_encoder(self):
        cancelled = threading.Event()
        cancelled.set()

        def must_not_run(command, callback=None):
            self.fail("a pre-cancelled export launched FFmpeg")

        output = self.root / "pre-cancelled.mp4"
        with self.assertRaises(ExportCancelled):
            self.render(output.name, run_process=must_not_run, cancel_event=cancelled)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
