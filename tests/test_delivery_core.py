"""Delivery settings, recent throughput, parallel cleanup, and real exports."""
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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exporter_core import CREATE_NO_WINDOW, Capabilities, ExportCancelled, ExportSettings, MediaInfo, find_binary
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam


class DeliveryMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delivery_metrics_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"original media")
        self.project = MulticamProject(
            [CameraClip("a", 1, str(self.source), 0, 4), CameraClip("b", 2, str(self.source), 0, 4)],
            [Cut(0, 1), Cut(2, 2)], fps=30)
        self.reference = MediaInfo(str(self.source), 4, 320, 180, 30, "h264", "yuv420p", 8)

    def render(self, runner, **kwargs):
        settings = kwargs.pop("settings", ExportSettings(codec="libx264", audio="none", decode_mode="cpu"))
        with patch("multicam_core.probe_media", return_value=self.reference):
            return render_multicam(self.project, self.root / "out.mp4", "ffmpeg", "ffprobe",
                                   settings, Capabilities(), runner, **kwargs)

    def test_recent_rates_ignore_startup_segment_resets_and_mux_packet_frames(self):
        now = [0.0]
        snapshots = []
        legacy = []

        def runner(command, report):
            if "concat" in command:
                now[0] += 10
                report({"frame": "9000", "out_time_us": "1000000", "fps": "9999", "speed": "999x"})
                now[0] += 1
                report({"frame": "10000", "out_time_us": "3000000", "fps": "9999", "speed": "999x"})
            else:
                report({"frame": "0", "out_time_us": "0"})
                now[0] += 10  # Decoder/encoder startup is not an FPS sample.
                report({"frame": "15", "out_time_us": "500000", "fps": "9999", "speed": "999x"})
                now[0] += 1
                report({"frame": "45", "out_time_us": "1500000", "fps": "9999", "speed": "999x"})
                report({"frame": "60", "out_time_us": "2000000", "progress": "end"})
            Path(command[-1]).write_bytes(b"encoded")
            return 0, ""

        with patch("multicam_core.time.monotonic", side_effect=lambda: now[0]):
            self.render(runner, metrics=snapshots.append, progress=lambda *values: legacy.append(values))
        encode = [item for item in snapshots if item["stage"] == "encoding"]
        self.assertEqual(sum(item["fps"] == 30 for item in encode), 2)
        self.assertTrue(all(item["fps"] <= 45 for item in encode))
        self.assertTrue(all(item["fps"] == 0 for item in encode if item["encoded_frames"] in {15, 75}))
        mux = [item for item in snapshots if item["stage"] == "muxing"]
        self.assertTrue(all(item["fps"] == 0 for item in mux))
        self.assertTrue(any(item["speed"] == 2.0 for item in mux))
        self.assertEqual(snapshots[-1]["encoded_frames"], 120)
        self.assertEqual(snapshots[-1]["progress_percent"], 100)
        self.assertEqual(snapshots[-1]["stage"], "done")
        self.assertEqual(snapshots[-1]["active_workers"], 0)
        self.assertTrue(all(item["progress_percent"] < 100 for item in snapshots[:-1]))
        percents = [item["progress_percent"] for item in snapshots]
        self.assertEqual(percents, sorted(percents))
        self.assertTrue(any(item["eta"] is not None and item["eta"] > 0 for item in encode))
        self.assertEqual(legacy[-1][:2], (4.0, 4.0))

    def test_parallel_runners_really_overlap_and_report_active_workers(self):
        barrier = threading.Barrier(2)
        snapshots = []
        calls = []

        def runner(command, report):
            calls.append(command)
            if "concat" not in command:
                barrier.wait(timeout=5)
                report({"frame": "30", "out_time_us": "1000000"})
            Path(command[-1]).write_bytes(b"encoded")
            return 0, ""

        self.render(runner, settings=ExportSettings(codec="libx264", audio="none", parallel_workers=2), metrics=snapshots.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(max(item["active_workers"] for item in snapshots), 2)
        self.assertEqual(snapshots[-1]["encoded_frames"], 120)

    def test_failed_parallel_job_waits_for_other_runner_before_cleanup(self):
        self.project.cuts = [Cut(0, 1), Cut(1, 2), Cut(2, 1), Cut(3, 2)]
        destination = self.root / "out.mp4"
        destination.write_bytes(b"previous result")
        barrier = threading.Barrier(2)
        failure_returned = threading.Event()
        calls = []

        def runner(command, report):
            calls.append(command)
            chunk = Path(command[-1])
            chunk.write_bytes(b"partial")
            barrier.wait(timeout=5)
            if chunk.name == "camera_000000.mp4":
                failure_returned.set()
                return 1, "intentional failure"
            failure_returned.wait(timeout=5)
            self.assertTrue(chunk.parent.is_dir())
            return 0, ""

        before = set(self.root.iterdir())
        with self.assertRaisesRegex(RuntimeError, "intentional failure"):
            self.render(runner, settings=ExportSettings(codec="libx264", audio="none", parallel_workers=2))
        self.assertEqual(len(calls), 2)
        self.assertEqual(destination.read_bytes(), b"previous result")
        self.assertEqual(set(self.root.iterdir()), before)

    def test_parallel_cancel_waits_and_never_replaces_destination(self):
        cancelled = threading.Event()
        barrier = threading.Barrier(2)
        calls = []
        destination = self.root / "out.mp4"
        destination.write_bytes(b"previous result")

        def runner(command, report):
            calls.append(command)
            barrier.wait(timeout=5)
            cancelled.set()
            Path(command[-1]).write_bytes(b"partial")
            return 1, "cancelled"

        before = set(self.root.iterdir())
        with self.assertRaises(ExportCancelled):
            self.render(runner, settings=ExportSettings(codec="libx264", audio="none", parallel_workers=2), cancel_event=cancelled)
        self.assertEqual(len(calls), 2)
        self.assertEqual(destination.read_bytes(), b"previous result")
        self.assertEqual(set(self.root.iterdir()), before)

    def test_copy_rejects_padding_and_incompatible_container_before_encoding(self):
        audio = self.root / "master.wav"
        audio.write_bytes(b"master")
        self.project.master_audio = str(audio)
        for source, duration in [({"codec_name": "pcm_s16le"}, 5), ({"codec_name": "aac"}, 2), ({}, 0)]:
            with self.subTest(source=source, duration=duration), patch("multicam_core._audio_stream_info", return_value=(source, duration)):
                with self.assertRaises(ValueError):
                    self.render(lambda *_: self.fail("invalid audio launched encoding"),
                                settings=ExportSettings(codec="libx264", audio="copy"))


class DeliveryRealFFmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = Path(__file__).resolve().parents[1]
        cls.ffmpeg, cls.ffprobe = find_binary("ffmpeg", app), find_binary("ffprobe", app)
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest("FFmpeg unavailable")
        cls.temp = tempfile.TemporaryDirectory(prefix="delivery_real_")
        cls.root = Path(cls.temp.name)
        cls.sources = [cls.root / "red.mp4", cls.root / "blue.mp4"]
        for source, color, size in zip(cls.sources, ["red", "blue"], ["160x90", "192x128"]):
            cls.run_command([cls.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                             f"color={color}:s={size}:r=30:d=4", "-c:v", "libx264", "-preset", "ultrafast", str(source)])
        cls.master = cls.root / "master.wav"
        cls.samples = array.array("h", (int(10000 * math.sin(2 * math.pi * (230 * n / 48000 + 13 * (n / 48000) ** 2)))
                                        for n in range(5 * 48000)))
        with wave.open(str(cls.master), "wb") as stream:
            stream.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            stream.writeframes(cls.samples.tobytes())
        cls.aac = cls.root / "master.m4a"
        cls.run_command([cls.ffmpeg, "-v", "error", "-y", "-i", str(cls.master), "-c:a", "aac", "-b:a", "192k", str(cls.aac)])
        cls.master44100 = cls.root / "master44100.wav"
        cls.run_command([cls.ffmpeg, "-v", "error", "-y", "-i", str(cls.master), "-ar", "44100", str(cls.master44100)])
        cls.camera_audio = cls.root / "camera_audio.mp4"
        cls.run_command([cls.ffmpeg, "-v", "error", "-y", "-i", str(cls.sources[0]), "-i", str(cls.master44100),
                         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-t", "4", str(cls.camera_audio)])

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    @staticmethod
    def run_command(command):
        result = subprocess.run(command, capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", "replace")[-5000:])
        return result.stdout

    @classmethod
    def runner(cls, command, callback):
        result = subprocess.run(command, capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
        snapshot = {}
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                snapshot[key] = value
                if key == "progress":
                    callback(snapshot)
                    snapshot = {}
        return result.returncode, result.stderr.decode("utf-8", "replace")

    def project(self, master=None):
        return MulticamProject([CameraClip("a", 1, str(self.sources[0]), 0, 2.5),
                                CameraClip("b", 2, str(self.sources[1]), 0, 4)],
                               [Cut(0, 1), Cut(1, 2), Cut(2, 1), Cut(3, 2)],
                               str(master or self.master), 4, 30, 0, 4)

    def test_all_audio_codecs_sample_rates_and_parallel_video_keep_exact_frames(self):
        variants = [("aac", ".mp4", 44100), ("pcm_s16le", ".mov", 48000),
                    ("pcm_s24le", ".mov", 96000), ("pcm_s32le", ".mkv", 48000),
                    ("pcm_f32le", ".mov", 48000), ("flac", ".mkv", 48000),
                    ("none", ".mp4", 48000), ("copy", ".mp4", 48000)]
        for codec, suffix, sample_rate in variants:
            with self.subTest(codec=codec):
                output = self.root / (codec + suffix)
                snapshots = []
                render_multicam(self.project(self.aac if codec == "copy" else None), output,
                                self.ffmpeg, self.ffprobe,
                                ExportSettings(codec="libx264", preset="ultrafast", audio=codec,
                                               audio_sample_rate=sample_rate, audio_bitrate="128k",
                                               parallel_workers=2, decode_mode="cpu"),
                                Capabilities(), self.runner, metrics=snapshots.append)
                streams = json.loads(self.run_command([self.ffprobe, "-v", "error", "-count_frames",
                    "-show_entries", "stream=codec_type,codec_name,sample_rate,nb_read_frames,bits_per_raw_sample,bit_rate", "-of", "json", str(output)]))["streams"]
                video = next(stream for stream in streams if stream["codec_type"] == "video")
                self.assertEqual(int(video["nb_read_frames"]), 120)
                audio = next((stream for stream in streams if stream["codec_type"] == "audio"), None)
                if codec == "none":
                    self.assertIsNone(audio)
                else:
                    self.assertEqual(audio["codec_name"], "aac" if codec == "copy" else codec)
                    self.assertEqual(int(audio["sample_rate"]), sample_rate)
                    if codec == "aac":
                        self.assertTrue(110000 < int(audio["bit_rate"]) < 145000)
                self.assertEqual(snapshots[-1]["encoded_frames"], 120)
                self.assertEqual(snapshots[-1]["progress_percent"], 100)
                self.assertTrue(all(s["fps"] == 0 for s in snapshots if s["stage"] == "muxing"))

    def test_parallel_export_keeps_black_gap_and_unbroken_sample_accurate_master(self):
        output = self.root / "continuous.mov"
        render_multicam(self.project(), output, self.ffmpeg, self.ffprobe,
                        ExportSettings(codec="libx264", preset="ultrafast", audio="pcm_s16le", parallel_workers=3,
                                       decode_mode="cpu", resolution="original"), Capabilities(), self.runner)
        raw = self.run_command([self.ffmpeg, "-v", "error", "-i", str(output), "-map", "0:a:0", "-f", "s16le", "-"])
        actual = array.array("h")
        actual.frombytes(raw)
        self.assertEqual(actual.tolist(), self.samples[:4 * 48000].tolist())
        for timestamp, expected in [(0.5, (255, 0, 0)), (1.5, (0, 0, 255)), (2.2, (255, 0, 0)),
                                    (2.7, (0, 0, 0)), (3.5, (0, 0, 255))]:
            rgb = self.run_command([self.ffmpeg, "-v", "error", "-ss", str(timestamp), "-i", str(output),
                                   "-frames:v", "1", "-vf", "crop=iw/2:ih/2,scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
            self.assertEqual(len(rgb), 3)
            self.assertTrue(all(abs(a - b) < 14 for a, b in zip(rgb, expected)), (timestamp, list(rgb)))

    def preserved_audio(self, project, name):
        output = self.root / name
        settings = ExportSettings(codec="libx264", preset="ultrafast", audio="pcm_s16le", audio_sample_rate=None,
                                  parallel_workers=2, decode_mode="cpu")
        render_multicam(project, output, self.ffmpeg, self.ffprobe, settings, Capabilities(), self.runner)
        self.assertIsNone(settings.audio_sample_rate, "renderer must not modify shared delivery settings")
        stream = json.loads(self.run_command([self.ffprobe, "-v", "error", "-select_streams", "a:0",
                                             "-show_entries", "stream=sample_rate,channels", "-of", "json", str(output)]))["streams"][0]
        raw = self.run_command([self.ffmpeg, "-v", "error", "-i", str(output), "-map", "0:a:0", "-f", "s16le", "-"])
        return stream, raw

    def test_preserve_sample_rate_keeps_44100_master_without_mutating_settings(self):
        stream, raw = self.preserved_audio(self.project(self.master44100), "preserved.mov")
        self.assertEqual(stream["sample_rate"], "44100")
        with wave.open(str(self.master44100), "rb") as source:
            self.assertEqual(raw, source.readframes(4 * 44100))

    def test_preserve_camera_audio_sample_rate_pads_leading_silence_on_timeline(self):
        project = MulticamProject([CameraClip("a", 1, str(self.camera_audio), 1, 3)],
                                  [Cut(0, 1)], master_duration=4, fps=30, range_end=4)
        stream, raw = self.preserved_audio(project, "leading_silence.mov")
        self.assertEqual(stream["sample_rate"], "44100")
        expected = self.run_command([self.ffmpeg, "-v", "error", "-i", str(self.camera_audio),
                                    "-map", "0:a:0", "-t", "3", "-f", "s16le", "-"])
        self.assertEqual(raw[:44100 * 2], b"\0" * (44100 * 2))
        self.assertEqual(raw[44100 * 2:], expected)

    def test_preserve_rate_without_master_or_camera_audio_uses_48000_silence(self):
        project = self.project()
        project.master_audio = ""
        stream, raw = self.preserved_audio(project, "silent_fallback.mov")
        self.assertEqual(stream["sample_rate"], "48000")
        self.assertEqual(stream["channels"], 2)
        self.assertEqual(raw, b"\0" * (4 * 48000 * 2 * 2))


if __name__ == "__main__":
    unittest.main()
