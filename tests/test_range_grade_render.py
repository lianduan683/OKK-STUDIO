"""Actual renders: cuts retain fps phase/audio and local grades reach pixels."""
from __future__ import annotations

from copy import deepcopy
from array import array
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creative_core import ColorGrade, Transition, grade_filter
from creative_render import (TransitionSegment, build_transition_command, has_pixel_effects,
                             normal_effect_filter, plan_creative_segments)
from exporter_core import CREATE_NO_WINDOW, Capabilities, ExportSettings, detect_capabilities
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam
from program_edits import ClipGrade, set_grade_on_range, split_at


class RangeGradeFilterTests(unittest.TestCase):
    def make(self):
        original = MulticamProject(clips=[CameraClip("a", 1, "a.mp4", 0, 4)], fps=10,
                                  camera_grades={1: ColorGrade(exposure=.3)},
                                  global_grade=ColorGrade(saturation=.5))
        set_grade_on_range(original, 1, 2, ColorGrade(contrast=1.2))
        return original

    def test_camera_then_local_then_global_and_fast_path_outside_range(self):
        original = self.make()
        self.assertEqual(normal_effect_filter(original, 1, 1.5), ",".join((
            grade_filter(original.camera_grades[1]), grade_filter(original.clip_grades[0].grade),
            grade_filter(original.global_grade))))
        original.camera_grades.clear()
        original.global_grade = ColorGrade()
        self.assertFalse(has_pixel_effects(original, 1, .5))
        self.assertTrue(has_pixel_effects(original, 1, 1.5))
        self.assertFalse(has_pixel_effects(original, 1, 2))
        self.assertTrue(has_pixel_effects(original, 1))

    def test_transition_uses_distinct_grade_each_side_of_manual_cut(self):
        original = self.make()
        original.camera_grades.clear()
        original.global_grade = ColorGrade()
        set_grade_on_range(original, 0, 2, ColorGrade(exposure=-1))
        set_grade_on_range(original, 2, 4, ColorGrade(exposure=1))
        original.transitions = [Transition(2, "fade", 1)]
        jobs = plan_creative_segments(original, original.segments())
        job = next(value for value in jobs if isinstance(value, TransitionSegment))
        command = build_transition_command(job, original, ["ffmpeg"], ExportSettings(),
                                           128, 72, "yuv420p", "10/1", [], "out.mp4")
        graph = command[command.index("-filter_complex") + 1]
        outgoing, incoming, _ = graph.split(";")
        self.assertIn(grade_filter(original.clip_grades[0].grade), outgoing)
        self.assertIn(grade_filter(original.clip_grades[1].grade), incoming)

    def test_transition_cannot_swallow_another_manual_cut(self):
        original = MulticamProject(clips=[CameraClip("a", 1, "a.mp4", 0, 4)], fps=10)
        split_at(original, 1.8)
        split_at(original, 2)
        original.transitions = [Transition(2, "fade", 1)]
        with self.assertRaisesRegex(ValueError, "包含其他机位切点"):
            plan_creative_segments(original, original.segments())


class RangeGradeMediaTests(unittest.TestCase):
    width, height = 128, 72

    @classmethod
    def command(cls, args, **kwargs):
        result = subprocess.run(args, capture_output=True, timeout=45,
                                creationflags=CREATE_NO_WINDOW, **kwargs)
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", "replace")[-4000:])
        return result.stdout

    @classmethod
    def setUpClass(cls):
        folder = Path(__file__).resolve().parents[1]
        cls.ffmpeg = str(folder / "bin" / "ffmpeg.exe")
        cls.ffprobe = str(folder / "bin" / "ffprobe.exe")
        if not Path(cls.ffmpeg).is_file():
            raise unittest.SkipTest("bundled FFmpeg unavailable")
        cls.temp = tempfile.TemporaryDirectory(prefix="range_grade_")
        cls.root = Path(cls.temp.name)
        cls.sources = {}
        for rate in (25, 30):
            raw = bytearray()
            for index in range(rate * 4):
                raw.extend((16 + (x * 3 + y * 5 + index * 17) % 220
                            for y in range(cls.height) for x in range(cls.width)))
                raw.extend(bytes([80 + index * 3 % 80]) * (cls.width * cls.height // 4))
                raw.extend(bytes([90 + index * 7 % 70]) * (cls.width * cls.height // 4))
            path = cls.root / f"source_{rate}.mp4"
            cls.command([cls.ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pixel_format", "yuv420p",
                         "-video_size", "128x72", "-framerate", str(rate), "-i", "pipe:0", "-an",
                         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-g", "30", "-bf", "0",
                         "-threads", "1", str(path)], input=bytes(raw))
            cls.sources[rate] = path
        cls.gray = cls.root / "gray.mp4"
        cls.command([cls.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                     "color=c=gray:s=128x72:r=10:d=4", "-an", "-c:v", "libx264", "-preset", "ultrafast",
                     "-crf", "0", "-threads", "1", str(cls.gray)])
        cls.audio = cls.root / "master.wav"
        cls.command([cls.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                     "sine=frequency=619:sample_rate=48000:duration=4", "-c:a", "pcm_s16le", str(cls.audio)])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def runner(command, report):
        result = subprocess.run(command, capture_output=True, timeout=45, creationflags=CREATE_NO_WINDOW)
        packet = {}
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                packet[key] = value
                if key == "progress":
                    report(packet)
                    packet = {}
        return result.returncode, result.stderr.decode("utf-8", "replace")

    def render(self, original, name, *, workers=1):
        output = self.root / f"{name}.mov"
        settings = ExportSettings(codec="libx264", preset="ultrafast", quality=0, audio="pcm_s16le",
                                  audio_sample_rate=48000, parallel_workers=workers,
                                  decode_mode="cpu", bframes=0, extra_args=["-threads", "1"])
        render_multicam(original, output, self.ffmpeg, self.ffprobe, settings, Capabilities(), self.runner)
        picture = self.command([self.ffmpeg, "-v", "error", "-i", str(output), "-map", "0:v:0", "-an",
                                "-pix_fmt", "yuv420p", "-f", "rawvideo", "pipe:1"])
        audio = self.command([self.ffmpeg, "-v", "error", "-i", str(output), "-map", "0:a:0", "-vn",
                              "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"])
        return picture, audio

    def test_same_camera_cuts_preserve_mixed_rate_pixels_and_continuous_audio(self):
        for source_fps, out_fps, start, workers in ((25, 30, 0, 1), (25, 30, .017, 2),
                                                    (30, 60000 / 1001, .017, 2)):
            with self.subTest(source_fps=source_fps, out_fps=out_fps, start=start, workers=workers):
                original = MulticamProject(clips=[CameraClip("a", 1, str(self.sources[source_fps]), -start, 4)],
                                          master_audio=str(self.audio), master_duration=4,
                                          fps=out_fps, range_end=3.2)
                before = self.render(original, f"continuous{source_fps}_{workers}", workers=workers)
                for time in (.23333, 1.0333, 2.1667):
                    split_at(original, time)
                after = self.render(original, f"cuts{source_fps}_{workers}", workers=workers)
                self.assertEqual(after[0], before[0], "manual cuts changed source frame selection")
                self.assertEqual(after[1], before[1], "manual cuts interrupted the master audio")

    def test_unverified_timing_uses_original_phase_instead_of_resetting_at_edit(self):
        original = MulticamProject(clips=[CameraClip("a", 1, str(self.sources[25]), -.017, 4)],
                                  master_audio=str(self.audio), master_duration=4, fps=30, range_end=3.2)
        before = self.render(original, "unknown_before")
        split_at(original, .233)
        split_at(original, 1.033)
        with patch("multicam_core.probe_partition_rates", return_value={}):
            after = self.render(original, "unknown_after")
        self.assertEqual(after, before)

    def test_same_camera_identity_fade_preserves_mixed_rate_frame_phase(self):
        original = MulticamProject(clips=[CameraClip("a", 1, str(self.sources[25]), 0, 4)],
                                  master_audio=str(self.audio), master_duration=4, fps=30)
        before = self.render(original, "before_identity_fade")
        split_at(original, 2)
        original.transitions = [Transition(2, "fade", 1)]
        after = self.render(original, "same_camera_identity_fade")
        self.assertEqual(after, before)
        original.range_start, original.range_end = 1.8, 2.2
        partial, _ = self.render(original, "identity_fade_partial")
        size = self.width * self.height * 3 // 2
        self.assertEqual(partial, before[0][54 * size:66 * size])

    def test_same_camera_transition_uses_each_shot_grade_and_partial_phase(self):
        original = MulticamProject(clips=[CameraClip("a", 1, str(self.gray), 0, 4)],
                                  master_audio=str(self.audio), master_duration=4, fps=10)
        set_grade_on_range(original, 0, 2, ColorGrade(exposure=-1))
        set_grade_on_range(original, 2, 4, ColorGrade(exposure=1))
        original.transitions = [Transition(2, "fade", 1)]
        picture, audio = self.render(original, "same_camera_transition")
        size = self.width * self.height * 3 // 2
        frames = [picture[offset:offset + size] for offset in range(0, len(picture), size)]
        self.assertEqual(len(frames), 40)
        # Luma at a central flat pixel follows dark -> mixture -> light.
        outgoing, middle, incoming = (frames[index][30 * self.width + 64] for index in (15, 20, 24))
        self.assertLess(outgoing, middle)
        self.assertLess(middle, incoming)
        self.assertGreater(incoming - outgoing, 100)
        partial = deepcopy(original)
        partial.range_start, partial.range_end = 1.8, 2.2
        partial_picture, _ = self.render(partial, "same_camera_partial")
        self.assertEqual(partial_picture, b"".join(frames[18:22]))
        self.assertEqual(len(audio), 4 * 48000 * 2)


@unittest.skipUnless(os.environ.get("OKK_TEST_NVENC") == "1", "set OKK_TEST_NVENC=1 for a short GPU integration")
class RangeGradeNvencTests(unittest.TestCase):
    """Synthetic local media only; each test encodes at most 3.2 seconds."""
    width, height = 320, 180
    command = RangeGradeMediaTests.command

    @classmethod
    def setUpClass(cls):
        app = Path(__file__).resolve().parents[1]
        cls.caps = detect_capabilities(app)
        cls.ffmpeg, cls.ffprobe = cls.caps.ffmpeg_path, cls.caps.ffprobe_path
        if not cls.ffmpeg or not cls.ffprobe or "hevc_nvenc" not in cls.caps.nvenc_codecs or not cls.caps.cuda_decode:
            raise unittest.SkipTest("HEVC NVENC/CUDA unavailable")
        cls.temp = tempfile.TemporaryDirectory(prefix="range_grade_nvenc_")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.source, cls.audio = cls.root / "frame_ids.mp4", cls.root / "master.wav"
        # Flat quadrants identify every source frame independently of scene
        # motion; native 10-bit lossless NVENC lets us compare decoded bytes.
        raw = array("H")
        for index in range(102):
            for y in range(cls.height):
                left = 96 + (index * 29 + (180 if y >= cls.height // 2 else 0)) % 680
                right = 96 + (index * 17 + (220 if y >= cls.height // 2 else 100)) % 680
                raw.extend([left] * (cls.width // 2) + [right] * (cls.width // 2))
            raw.extend([400 + index % 120] * (cls.width * cls.height // 4))
            raw.extend([470 + index % 100] * (cls.width * cls.height // 4))
        cls.command([cls.ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pixel_format", "yuv420p10le",
                     "-video_size", "320x180", "-framerate", "30", "-i", "pipe:0", "-an",
                     "-c:v", "hevc_nvenc", "-preset", "p2", "-tune", "lossless", "-rc", "constqp",
                     "-qp", "0", "-bf", "0", "-profile:v", "main10", "-pix_fmt", "p010le", str(cls.source)],
                    input=raw.tobytes())
        cls.command([cls.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                     "sine=frequency=619:sample_rate=48000:duration=4", "-c:a", "pcm_s16le", str(cls.audio)])

    def render(self, original, name):
        destination = self.root / f"{name}.mov"
        commands = []

        def runner(command, report):
            commands.append(command)
            return RangeGradeMediaTests.runner(command, report)

        settings = ExportSettings(codec="hevc_nvenc", preset="p2", tune="lossless", quality=0,
                                  audio="pcm_s16le", audio_sample_rate=48000, parallel_workers=2,
                                  bit_depth="10-bit", decode_mode="cuda", bframes=0,
                                  extra_args=["-rc", "constqp", "-qp", "0"])
        render_multicam(original, destination, self.ffmpeg, self.ffprobe,
                        settings, self.caps, runner)
        picture = self.command([self.ffmpeg, "-v", "error", "-i", str(destination), "-map", "0:v:0", "-an",
                                "-pix_fmt", "yuv420p10le", "-f", "rawvideo", "pipe:1"])
        audio = self.command([self.ffmpeg, "-v", "error", "-i", str(destination), "-map", "0:a:0", "-vn",
                              "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"])
        return destination, picture, audio, commands

    def make(self):
        return MulticamProject(clips=[CameraClip("a", 1, str(self.source), 0, 3.4)],
                               master_audio=str(self.audio), master_duration=4,
                               fps=59.94006, range_end=3.2)

    def test_same_camera_cuts_keep_cuda_surfaces_and_exact_30_to_5994_pixels(self):
        original = self.make()
        _, before_picture, before_audio, _ = self.render(original, "gpu_continuous")
        for position in (.23333, 1.0333, 2.1667):
            split_at(original, position)
        _, after_picture, after_audio, commands = self.render(original, "gpu_cut")
        chunks = [command for command in commands if "-frames:v" in command]
        self.assertEqual(len(chunks), 4)
        for command in chunks:
            self.assertIn("-hwaccel_output_format", command)
            self.assertEqual(command[command.index("-hwaccel_output_format") + 1], "cuda")
            self.assertNotIn("hwdownload", command[command.index("-vf") + 1])
        self.assertEqual(after_picture, before_picture, "CUDA cut changed source frame selection")
        self.assertEqual(after_audio, before_audio, "CUDA cut interrupted the continuous master audio")
        self.assertEqual(len(after_picture), 192 * self.width * self.height * 3)

    def test_primary_wheels_and_same_camera_transition_export_ten_bit_and_correct_audio(self):
        original = self.make()
        set_grade_on_range(original, 0, 1.6, ColorGrade(lift_r=.06, gamma_y=.1,
                                                      gain_b=.08, offset_g=.025))
        set_grade_on_range(original, 1.6, 3.2, ColorGrade(lift_b=.03, gamma_r=-.05,
                                                       gain_r=.12, offset_y=-.02))
        original.transitions = [Transition(1.6, "fade", .5)]
        destination, picture, audio, commands = self.render(original, "gpu_wheels_transition")
        payload = json.loads(self.command([self.ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
                                           "-show_entries", "stream=pix_fmt,nb_read_frames,r_frame_rate", "-of", "json",
                                           str(destination)]))
        stream = payload["streams"][0]
        self.assertEqual((stream["pix_fmt"], stream["nb_read_frames"], stream["r_frame_rate"]),
                         ("yuv420p10le", "192", "60000/1001"))
        self.assertEqual(len(picture), 192 * self.width * self.height * 3)
        self.assertEqual(len(audio), round(192 / (60000 / 1001) * 48000) * 2)
        self.assertTrue(any("-filter_complex" in command and "xfade=" in command[command.index("-filter_complex") + 1]
                            for command in commands))
        # Verify actual pixel effects, not just the filter string: compare
        # one unblended frame from each graded range with the neutral render.
        _, neutral, neutral_audio, _ = self.render(self.make(), "gpu_neutral_for_wheels")
        size = self.width * self.height * 3
        for index in (20, 150):
            self.assertNotEqual(picture[index * size:(index + 1) * size],
                                neutral[index * size:(index + 1) * size])
        self.assertEqual(audio, neutral_audio)


if __name__ == "__main__":
    unittest.main()
