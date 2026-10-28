"""Short effect jobs retain programme frame count, phase, and bit depth."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creative_core import ColorGrade, Transition, grade_filter
from creative_render import (TransitionSegment, build_transition_command, has_pixel_effects,
                             normal_effect_filter, plan_creative_segments)
from exporter_core import ExportSettings
from multicam_core import CameraClip, Cut, MulticamProject
from subtitle_core import SubtitleCue, SubtitleStyle, SubtitleTrack, write_ass


def project(**changes):
    values = dict(clips=[CameraClip("a", 1, "red.mp4", 0, 4), CameraClip("b", 2, "blue.mp4", 0, 4)],
                  cuts=[Cut(0, 1), Cut(2, 2)], fps=10,
                  transitions=[Transition(2, "fade", 1)])
    values.update(changes)
    return MulticamProject(**values)


class CreativeRenderPlanTests(unittest.TestCase):
    def test_window_replaces_cut_without_adding_or_removing_frames(self):
        original = project()
        segments = original.segments()
        result = plan_creative_segments(original, segments)
        self.assertEqual([segment.frames for segment in result], [15, 10, 15])
        self.assertEqual([segment.start for segment in result], [0, 1.5, 2.5])
        self.assertEqual(sum(segment.frames for segment in result), 40)
        blend = result[1]
        self.assertIsInstance(blend, TransitionSegment)
        self.assertEqual((blend.camera, blend.other_camera), (1, 2))
        self.assertEqual((blend.source_start, blend.other_source_start, blend.full_start), (1.5, 1.5, 1.5))
        self.assertEqual((blend.full_frames, blend.trim_start_frames), (10, 0))
        self.assertEqual(result[-1].source_start, 2.5)

    def test_range_inside_window_keeps_full_phase_and_source_seeks(self):
        original = project(range_start=1.8, range_end=2.2)
        result = plan_creative_segments(original, original.segments())
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0].start, result[0].frames, result[0].duration), (1.8, 4, .4))
        self.assertEqual((result[0].trim_start_frames, result[0].full_frames, result[0].source_start), (3, 10, 1.5))

    def test_off_grid_odd_duration_uses_same_integer_frame_clock(self):
        original = project(fps=30, transitions=[Transition(2.001, "wipeleft", .51)])
        result = plan_creative_segments(original, original.segments())
        self.assertEqual(sum(segment.frames for segment in result), 120)
        blend = next(segment for segment in result if isinstance(segment, TransitionSegment))
        self.assertEqual(blend.full_frames, 15)
        self.assertEqual(round(blend.full_start * 30), 53)
        self.assertEqual(round(blend.end * 30), 68)

    def test_negative_camera_alignment_and_trim_seek_are_maintained(self):
        original = project(clips=[CameraClip("a", 1, "red.mp4", -2, 6), CameraClip("b", 2, "blue.mp4", -1, 5)])
        blend = plan_creative_segments(original, original.segments())[1]
        self.assertEqual((blend.source_start, blend.other_source_start), (3.5, 2.5))

    def test_material_must_be_one_continuous_selected_clip_per_camera(self):
        cases = [
            [CameraClip("a", 1, "red.mp4", 0, 2.1), CameraClip("b", 2, "blue.mp4", 0, 4)],
            [CameraClip("a", 1, "red.mp4", 0, 4), CameraClip("b", 2, "blue.mp4", 1.8, 2.2)],
            [CameraClip("a", 1, "red.mp4", 0, 4), CameraClip("b", 2, "blue.mp4", 0, 4),
             CameraClip("b2", 2, "blue2.mp4", 2.1, 1)],
        ]
        for clips in cases:
            original = project(clips=clips)
            with self.subTest(clips=clips), self.assertRaisesRegex(ValueError, "完整覆盖转场"):
                plan_creative_segments(original, original.segments())

    def test_transition_elsewhere_does_not_require_unexported_camera_handles(self):
        original = project(clips=[CameraClip("a", 1, "red.mp4", 0, 2), CameraClip("b", 2, "blue.mp4", 2, 2)],
                           range_end=1)
        result = plan_creative_segments(original, original.segments())
        self.assertEqual(sum(segment.frames for segment in result), 10)
        self.assertFalse(any(isinstance(segment, TransitionSegment) for segment in result))

    def test_transition_does_not_swallow_another_programme_cut(self):
        original = project(cuts=[Cut(0, 1), Cut(1.8, 2), Cut(2, 1)])
        with self.assertRaisesRegex(ValueError, "包含其他机位切点"):
            plan_creative_segments(original, original.segments())

    def test_camera_then_global_grade_and_single_absolute_time_subtitle(self):
        original = project(global_grade=ColorGrade(saturation=.5), camera_grades={1: ColorGrade(exposure=1)})
        self.assertTrue(has_pixel_effects(original, 1))
        self.assertTrue(has_pixel_effects(original, 2))
        chain = normal_effect_filter(original, 1, 12)
        self.assertEqual(chain, grade_filter(original.camera_grades[1]) + "," + grade_filter(original.global_grade))
        chain = normal_effect_filter(original, 1, 12, "subtitle.ass")
        self.assertIn("setpts=PTS+12/TB,subtitles=filename=", chain)
        self.assertTrue(chain.endswith("setpts=PTS-STARTPTS"))
        original = project()
        self.assertFalse(has_pixel_effects(original, 1))
        original.subtitles.cues = [SubtitleCue("s", 0, 1, "字幕")]
        self.assertTrue(has_pixel_effects(original, 2))
        original.subtitles.burn_in = False
        self.assertFalse(has_pixel_effects(original, 2))


class CreativeRenderPixelsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = Path(__file__).resolve().parents[1] / "bin" / "ffmpeg.exe"
        cls.ffprobe = cls.ffmpeg.with_name("ffprobe.exe")
        if not cls.ffmpeg.is_file():
            raise unittest.SkipTest("bundled FFmpeg unavailable")
        cls.folder = tempfile.TemporaryDirectory()
        cls.root = Path(cls.folder.name)
        for color in ("red", "blue"):
            command = [str(cls.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                       f"color=c={color}:s=128x72:r=10:d=4", "-c:v", "libx264", "-crf", "0", "-preset", "ultrafast",
                       "-pix_fmt", "yuv420p", str(cls.root / f"{color}.mp4")]
            result = subprocess.run(command, capture_output=True, timeout=20)
            if result.returncode:
                cls.folder.cleanup()
                raise RuntimeError(result.stderr.decode(errors="replace"))

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def make(self, **changes):
        return project(clips=[CameraClip("a", 1, str(self.root / "red.mp4"), 0, 4),
                              CameraClip("b", 2, str(self.root / "blue.mp4"), 0, 4)], **changes)

    def render(self, original, name, pixel_format="yuv420p", ass_path=None):
        job = next(segment for segment in plan_creative_segments(original, original.segments())
                   if isinstance(segment, TransitionSegment))
        path = self.root / name
        command = build_transition_command(job, original,
                    [str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-y"],
                    ExportSettings(codec="libx264"), 128, 72, pixel_format, "10/1",
                    ["-c:v", "libx264", "-crf", "0", "-preset", "ultrafast"], path, ass_path)
        result = subprocess.run(command, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        decoded = subprocess.run([str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(path),
                                  "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=15)
        self.assertEqual(decoded.returncode, 0, decoded.stderr.decode(errors="replace"))
        size = 128 * 72 * 3
        self.assertEqual(len(decoded.stdout), size * job.frames)
        frames = [decoded.stdout[index:index + size] for index in range(0, len(decoded.stdout), size)]
        return path, frames, command

    @staticmethod
    def pixel(frame, x=64, y=30):
        offset = (y * 128 + x) * 3
        return tuple(frame[offset:offset + 3])

    def test_fade_first_middle_last_are_source_mixture_destination(self):
        _, frames, _ = self.render(self.make(), "fade.mp4")
        first, middle, last = (self.pixel(frames[index]) for index in (0, 5, 9))
        self.assertGreater(first[0], 240)
        self.assertLess(first[2], 10)
        self.assertTrue(90 < middle[0] < 170 and 90 < middle[2] < 170, middle)
        self.assertLess(last[0], 40)
        self.assertGreater(last[2], 210)

    def test_wipe_middle_has_two_spatially_distinct_camera_regions(self):
        _, frames, _ = self.render(self.make(transitions=[Transition(2, "wipeleft", 1)]), "wipe.mp4")
        a, b = self.pixel(frames[5], 16), self.pixel(frames[5], 112)
        self.assertTrue((a[0] > 200 and b[2] > 200) or (a[2] > 200 and b[0] > 200), (a, b))

    def test_partial_range_pixels_equal_the_same_phase_of_full_transition(self):
        _, complete, _ = self.render(self.make(), "full.mp4")
        _, partial, _ = self.render(self.make(range_start=1.8, range_end=2.2), "partial.mp4")
        self.assertEqual(len(partial), 4)
        self.assertEqual(partial, complete[3:7])

    def test_ten_bit_output_remains_ten_bit_through_xfade(self):
        path, frames, _ = self.render(self.make(), "tenbit.mp4", "yuv420p10le")
        info = subprocess.run([str(self.ffprobe), "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=pix_fmt,nb_frames", "-of", "json", str(path)], capture_output=True, timeout=10)
        stream = json.loads(info.stdout)["streams"][0]
        self.assertEqual(stream["pix_fmt"], "yuv420p10le")
        self.assertEqual(stream["nb_frames"], "10")
        self.assertGreater(self.pixel(frames[5])[2], 90)

    def test_camera_grade_then_global_grade_are_applied_to_transition_inputs(self):
        original = self.make(global_grade=ColorGrade(exposure=-1),
                             camera_grades={1: ColorGrade(contrast=0)})
        _, frames, _ = self.render(original, "graded.mp4")
        first, last = self.pixel(frames[0]), self.pixel(frames[9])
        self.assertLess(max(first) - min(first), 5, first)
        self.assertTrue(50 < first[0] < 80, first)
        self.assertTrue(100 < last[2] < 150, last)
        self.assertLess(last[0], 15)

    def test_subtitle_applies_once_after_blend_and_uses_programme_time(self):
        original = self.make()
        track = SubtitleTrack([SubtitleCue("s", 1.5, 2.5, "中文")],
                              SubtitleStyle(font_size=300, color="#00FF00", outline_width=0, shadow=0))
        ass_path = write_ass(track, self.root / "字幕.ass", 128, 72)
        _, frames, command = self.render(original, "subtitle.mp4", ass_path=ass_path)
        graph = command[command.index("-filter_complex") + 1]
        self.assertEqual(graph.count("subtitles=filename="), 1)
        self.assertGreater(graph.index("subtitles="), graph.index("xfade="))
        self.assertIn("setpts=PTS+1.5/TB", graph)
        green = sum(1 for r, g, b in zip(frames[5][::3], frames[5][1::3], frames[5][2::3])
                    if g > 90 and g > r * 1.5 and g > b * 1.5)
        self.assertGreater(green, 30)


if __name__ == "__main__":
    unittest.main()
