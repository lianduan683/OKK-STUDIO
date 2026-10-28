"""End-to-end short programme exports with grading, transitions and subtitles."""
from __future__ import annotations

import array
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creative_core import ColorGrade, Transition
from exporter_core import Capabilities, ExportCancelled, ExportSettings, MediaInfo, detect_capabilities
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam
from subtitle_core import SubtitleCue, SubtitleStyle, SubtitleTrack, parse_srt


class CreativeProgrammeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = Path(__file__).resolve().parents[1] / "bin" / "ffmpeg.exe"
        cls.ffprobe = cls.ffmpeg.with_name("ffprobe.exe")
        if not cls.ffmpeg.is_file() or not cls.ffprobe.is_file():
            raise unittest.SkipTest("bundled FFmpeg unavailable")
        cls.folder = tempfile.TemporaryDirectory(prefix="creative_export_")
        cls.root = Path(cls.folder.name)
        cls.sources = []
        for color in ("red", "blue"):
            path = cls.root / f"dynamic_{color}.mp4"
            marker = "if(lt(mod(X+N*3,160),8),255,0)"
            expression = (f"geq=r=255:g='{marker}':b='{marker}'" if color == "red" else
                          f"geq=r='{marker}':g='{marker}':b=255")
            command = [str(cls.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-filter_threads", "1",
                       "-f", "lavfi", "-i", "color=black:s=160x90:r=12:d=4", "-vf", expression,
                       "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv420p", str(path)]
            result = subprocess.run(command, capture_output=True, timeout=30)
            if result.returncode:
                cls.folder.cleanup()
                raise RuntimeError(result.stderr.decode(errors="replace"))
            cls.sources.append(path)
        cls.master = cls.root / "连续主音频.wav"
        cls.samples = array.array("h", (round(10000 * math.sin(2 * math.pi * (220 * (index / 48000)
                        + 45 * (index / 48000) ** 2))) for index in range(4 * 48000)))
        with wave.open(str(cls.master), "wb") as file:
            file.setnchannels(1)
            file.setsampwidth(2)
            file.setframerate(48000)
            file.writeframes(cls.samples.tobytes())
        srt = cls.root / "中文源字幕.srt"
        srt.write_text("1\n00:00:00,750 --> 00:00:01,167\n中文样式\n\n"
                       "2\n00:00:01,500 --> 00:00:02,667\n跨越转场\n第二行\n\n"
                       "3\n00:00:02,000 --> 00:00:02,333\n重叠字幕\n", encoding="utf-8-sig")
        cls.cues = parse_srt(srt)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def project(self, effects=True, **changes):
        values = dict(clips=[CameraClip("a", 1, str(self.sources[0]), 0, 4),
                             CameraClip("b", 2, str(self.sources[1]), 0, 4)],
                      cuts=[Cut(0, 1), Cut(2, 2)], master_audio=str(self.master), fps=12)
        if effects:
            values.update(global_grade=ColorGrade(exposure=-.5),
                          camera_grades={1: ColorGrade(saturation=0)},
                          transitions=[Transition(2, "fade", 1)],
                          subtitles=SubtitleTrack(list(self.cues), SubtitleStyle(font_size=220,
                              color="#00FF00", outline_width=1, shadow=0, bold=True, margin_y=60)))
        values.update(changes)
        return MulticamProject(**values)

    def process(self, command, callback=None):
        result = subprocess.run(command, capture_output=True, timeout=40)
        if callback is not None:
            callback({"frame": "1", "out_time_us": "83333"})
        return result.returncode, result.stderr.decode("utf-8", "replace")

    def export(self, name, original=None, settings=None, runner=None, **kwargs):
        output = self.root / name
        result = render_multicam(original or self.project(), output, str(self.ffmpeg), str(self.ffprobe),
                    settings or ExportSettings(codec="libx264", preset="ultrafast", quality=0, audio="pcm_s16le",
                                                decode_mode="cpu", bframes=0, parallel_workers=2),
                    Capabilities(), runner or self.process, **kwargs)
        self.assertEqual(Path(result), output)
        return output

    def frames(self, output):
        result = subprocess.run([str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(output),
                                 "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        size = 160 * 90 * 3
        self.assertEqual(len(result.stdout) % size, 0)
        return [result.stdout[index:index + size] for index in range(0, len(result.stdout), size)]

    def master_samples(self, output):
        result = subprocess.run([str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(output),
                                 "-map", "0:a:0", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        samples = array.array("h")
        samples.frombytes(result.stdout)
        return samples

    @staticmethod
    def green_pixels(frame):
        return sum(1 for r, g, b in zip(frame[::3], frame[1::3], frame[2::3])
                   if g > 150 and g > r * 1.5 and g > b * 1.5)

    @staticmethod
    def pixel(frame, x=80, y=25):
        position = (y * 160 + x) * 3
        return tuple(frame[position:position + 3])

    def test_real_export_applies_grades_fade_chinese_styles_and_continuous_master(self):
        metrics, commands = [], []
        def runner(command, callback=None):
            commands.append(command)
            return self.process(command, callback)
        output = self.export("complete.mov", runner=runner, metrics=metrics.append)
        frames = self.frames(output)
        self.assertEqual(len(frames), 48)
        # Camera 1's desaturation precedes the shared half-stop exposure.
        first = self.pixel(frames[2])
        self.assertLess(max(first) - min(first), 5, first)
        # The common grade also affects camera 2, while green captions are
        # burned after grading and retain their chosen vivid appearance.
        tail = self.pixel(frames[43])
        self.assertGreater(tail[2], tail[0] + 100, tail)
        self.assertLess(tail[2], 210)
        self.assertEqual(self.green_pixels(frames[2]), 0)
        self.assertGreater(self.green_pixels(frames[11]), 40)
        self.assertEqual(self.green_pixels(frames[15]), 0)
        self.assertGreater(self.green_pixels(frames[24]), 50)
        self.assertEqual(self.green_pixels(frames[40]), 0)
        self.assertEqual(self.master_samples(output), self.samples)
        transition_commands = [command for command in commands if "-filter_complex" in command]
        self.assertEqual(len(transition_commands), 1)
        graph = transition_commands[0][transition_commands[0].index("-filter_complex") + 1]
        self.assertEqual(graph.count("subtitles=filename="), 1)
        self.assertIn("xfade=transition=fade", graph)
        # Final picture mux copies the encoded chunks rather than encoding
        # the complete timeline a second time.
        final = commands[-1]
        self.assertEqual(final[final.index("-c:v") + 1], "copy")
        self.assertEqual(metrics[-1]["stage"], "done")
        self.assertEqual(metrics[-1]["encoded_frames"], 48)
        self.assertEqual(metrics[-1]["progress_percent"], 100)

    def test_subrange_inside_fade_keeps_same_pixels_subtitle_phase_and_master_samples(self):
        full = self.export("full_for_range.mov")
        selected = self.export("range.mov", self.project(range_start=1.75, range_end=2.25))
        full_frames, selected_frames = self.frames(full), self.frames(selected)
        self.assertEqual(len(selected_frames), 6)
        self.assertEqual(selected_frames, full_frames[21:27])
        self.assertGreater(self.green_pixels(selected_frames[4]), 50)
        self.assertEqual(self.master_samples(selected), self.samples[84000:108000])

    def test_effects_disabled_keep_original_frame_count_dynamic_source_and_master(self):
        original = self.project(effects=False)
        output = self.export("unchanged.mov", original)
        frames = self.frames(output)
        self.assertEqual(len(frames), 48)
        self.assertNotEqual(frames[0], frames[6], "moving marker froze during export")
        self.assertGreater(self.pixel(frames[5])[0], 240)
        self.assertGreater(self.pixel(frames[40])[2], 240)
        self.assertEqual(self.master_samples(output), self.samples)

    def test_encoder_failure_and_cancel_keep_existing_file_and_clean_effect_workspace(self):
        for cancel in (False, True):
            output = self.root / f"protected_{cancel}.mov"
            output.write_bytes(b"existing completed export")
            before = set(self.root.iterdir())
            flag = threading.Event()
            settings = ExportSettings(codec="libx264", preset="ultrafast", quality=0,
                                      decode_mode="cpu", audio="pcm_s16le", parallel_workers=1)
            def fail(command, callback=None):
                if cancel:
                    flag.set()
                return 1, "intentional effect encoder failure"
            with self.subTest(cancel=cancel), self.assertRaises(ExportCancelled if cancel else RuntimeError):
                self.export(output.name, settings=settings, runner=fail, cancel_event=flag)
            self.assertEqual(output.read_bytes(), b"existing completed export")
            self.assertEqual(set(self.root.iterdir()), before)

    def test_identity_and_disabled_effects_preserve_cuda_resident_command_path(self):
        for disabled in (False, True):
            original = self.project(effects=False)
            if disabled:
                original.global_grade = ColorGrade(exposure=1, enabled=False)
                original.camera_grades[1] = ColorGrade(saturation=0, enabled=False)
                original.subtitles = SubtitleTrack(list(self.cues), enabled=False)
            commands = []
            def fake_run(command, callback=None):
                commands.append(command)
                Path(command[-1]).write_bytes(b"rendered")
                return 0, ""
            media = {str(path): MediaInfo(str(path), 4, 160, 90, 12, "h264", "yuv420p", 8)
                     for path in self.sources}
            with self.subTest(disabled=disabled), patch("multicam_core.probe_media", side_effect=lambda _, path: media[path]):
                render_multicam(original, self.root / f"mock_cuda_{disabled}.mp4", "ffmpeg", "ffprobe",
                    ExportSettings(codec="hevc_nvenc", decode_mode="cuda", audio="none"),
                    Capabilities(nvenc_codecs={"hevc_nvenc"}, cuda_decode=True, scale_cuda=True), fake_run)
            self.assertEqual(len(commands), 3)
            for command in commands[:-1]:
                self.assertEqual(command[command.index("-hwaccel_output_format") + 1], "cuda")
                self.assertNotIn("-pix_fmt", command)
                graph = command[command.index("-vf") + 1]
                self.assertNotIn("hwdownload", graph)
                self.assertNotIn("subtitles=", graph)
                self.assertNotIn("scale=", graph)
                self.assertNotIn("format=", graph)
                self.assertNotIn("-filter_complex", command)


@unittest.skipUnless(os.environ.get("OKK_TEST_NVENC") == "1", "set OKK_TEST_NVENC=1 for real short GPU exports")
class CreativeNvencProgrammeTests(unittest.TestCase):
    """Opt-in real hardware encode with the same grade/xfade/libass chain."""
    project = CreativeProgrammeIntegrationTests.project
    process = CreativeProgrammeIntegrationTests.process
    master_samples = CreativeProgrammeIntegrationTests.master_samples

    @classmethod
    def setUpClass(cls):
        CreativeProgrammeIntegrationTests.setUpClass.__func__(cls)
        cls.caps = detect_capabilities(Path(__file__).resolve().parents[1])
        if not cls.caps.cuda_decode:
            cls.folder.cleanup()
            raise unittest.SkipTest("CUDA decoder unavailable")
        # Keep the hardware smoke deliberately small: two synthetic moving
        # sources, 320 x 180, and three seconds of programme per encoder.
        enlarged = []
        for index, original in enumerate(cls.sources):
            source = cls.root / f"gpu_source_{index}.mp4"
            result = subprocess.run([str(cls.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(original), "-vf", "scale=320:180", "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "0", "-pix_fmt", "yuv420p", str(source)], capture_output=True, timeout=30)
            if result.returncode:
                cls.folder.cleanup()
                raise RuntimeError(result.stderr.decode(errors="replace"))
            enlarged.append(source)
        cls.sources = enlarged

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def smoke(self, codec, depth, expected_pixel_format):
        if codec not in self.caps.nvenc_codecs:
            self.skipTest(f"{codec} unavailable")
        original = self.project(range_end=3)
        output = self.root / f"actual_{codec}_{depth}.mov"
        commands, metrics = [], []
        def runner(command, callback=None):
            commands.append(command)
            return self.process(command, callback)
        render_multicam(original, output, str(self.ffmpeg), str(self.ffprobe),
            ExportSettings(codec=codec, preset="p2", quality=22, audio="pcm_s16le", decode_mode="cuda",
                           bit_depth=f"{depth}-bit", bframes=0, parallel_workers=2, split_encode="disabled"),
            self.caps, runner, metrics=metrics.append)
        result = subprocess.run([str(self.ffprobe), "-v", "error", "-select_streams", "v:0", "-count_frames",
                "-show_entries", "stream=codec_name,pix_fmt,width,height,r_frame_rate,nb_read_frames,duration",
                "-of", "json", str(output)], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        stream = json.loads(result.stdout)["streams"][0]
        self.assertEqual(stream["codec_name"], "h264" if codec == "h264_nvenc" else "hevc")
        self.assertEqual(stream["pix_fmt"], expected_pixel_format)
        self.assertEqual((stream["width"], stream["height"]), (320, 180))
        self.assertEqual(stream["r_frame_rate"], "12/1")
        self.assertEqual(int(stream["nb_read_frames"]), 36)
        self.assertAlmostEqual(float(stream["duration"]), 3, places=3)
        self.assertEqual(self.master_samples(output), self.samples[:144000])
        decoded = subprocess.run([str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", "2",
                "-i", str(output), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                capture_output=True, timeout=15)
        self.assertEqual(decoded.returncode, 0, decoded.stderr.decode(errors="replace"))
        self.assertEqual(len(decoded.stdout), 320 * 180 * 3)
        self.assertGreater(CreativeProgrammeIntegrationTests.green_pixels(decoded.stdout), 150)
        transition = [command for command in commands if "-filter_complex" in command]
        self.assertEqual(len(transition), 1)
        self.assertEqual(transition[0][transition[0].index("-c:v") + 1], codec)
        graph = transition[0][transition[0].index("-filter_complex") + 1]
        self.assertIn("xfade=transition=fade", graph)
        self.assertEqual(graph.count("subtitles=filename="), 1)
        self.assertIn(f"format={expected_pixel_format}", graph)
        self.assertEqual(commands[-1][commands[-1].index("-c:v") + 1], "copy")
        self.assertEqual((metrics[-1]["encoded_frames"], metrics[-1]["stage"]), (36, "done"))

    def test_h264_eight_bit_grade_transition_subtitles_and_audio(self):
        self.smoke("h264_nvenc", 8, "yuv420p")

    def test_hevc_ten_bit_grade_transition_subtitles_and_audio(self):
        self.smoke("hevc_nvenc", 10, "yuv420p10le")


if __name__ == "__main__":
    unittest.main()
