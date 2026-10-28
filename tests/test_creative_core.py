"""Portable creative settings, safe LUT paths and real FFmpeg image checks."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creative_core import (
    ColorGrade, GRADE_RANGES, TRANSITION_KINDS, Transition, effects_from_dict,
    effects_to_dict, escape_filter_path, grade_filter, grade_from_dict,
    grade_is_identity, grade_to_dict, transition_filter, transition_from_dict,
    transition_to_dict, transition_window, validate_grade, validate_transitions,
)
from exporter_core import CREATE_NO_WINDOW


class CreativeSettingsTests(unittest.TestCase):
    def test_default_and_disabled_grades_bypass_filters(self):
        self.assertTrue(grade_is_identity(ColorGrade()))
        self.assertEqual(grade_filter(ColorGrade()), "")
        disabled = ColorGrade(exposure=2, lut_path="missing.cube", enabled=False)
        self.assertTrue(grade_is_identity(disabled))
        self.assertEqual(grade_filter(disabled), "")

    def test_each_control_has_finite_numeric_bounds(self):
        for name, (minimum, maximum) in GRADE_RANGES.items():
            for value in (minimum, maximum):
                with self.subTest(name=name, value=value):
                    validate_grade(replace(ColorGrade(), **{name: value}))
            for value in (minimum - 0.001, maximum + 0.001, float("nan"),
                          float("inf"), True, "1"):
                with self.subTest(name=name, invalid=value):
                    with self.assertRaises(ValueError):
                        validate_grade(replace(ColorGrade(), **{name: value}))

    def test_grading_roundtrip_and_missing_lut_relink(self):
        grade = ColorGrade(exposure=0.5, contrast=1.4, saturation=0.8, temperature=-0.3,
                           tint=0.2, gamma=1.2, shadows=0.2, highlights=-0.4,
                           lut_path="D:/camera look.cube")
        restored = grade_from_dict(json.loads(json.dumps(grade_to_dict(grade))))
        self.assertEqual(restored, grade)
        with self.assertRaisesRegex(ValueError, "找不到 LUT"):
            grade_filter(restored)
        self.assertEqual(grade_from_dict(None), ColorGrade())
        for bad in ([], {"exposure": "1"}, {"enabled": 1}, {"unexpected": 1},
                    {"lut_path": "a\n.cube"}, {"lut_path": "bad.exe"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                grade_from_dict(bad)

    def test_effects_json_roundtrip_camera_keys_and_old_projects(self):
        global_grade = ColorGrade(gamma=1.1)
        cameras = {1: ColorGrade(temperature=0.2), 3: ColorGrade(saturation=0.5)}
        transitions = [Transition(1.5, "wipeleft", 0.6), Transition(3, "fadeblack", 0.2)]
        payload = json.loads(json.dumps(effects_to_dict(global_grade, cameras, transitions)))
        self.assertEqual(effects_from_dict(payload), (global_grade, cameras, transitions))
        self.assertEqual(effects_from_dict(None), (ColorGrade(), {}, []))
        self.assertEqual(effects_from_dict({}), (ColorGrade(), {}, []))
        for bad in ([], {"unknown": 0}, {"camera_grades": []},
                    {"camera_grades": {"1.0": {}}}, {"camera_grades": {"0": {}}},
                    {"camera_grades": {"True": {}}}, {"transitions": {}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                effects_from_dict(bad)

    def test_transition_windows_preserve_cut_clock_and_model_validation(self):
        project = SimpleNamespace(fps=30, duration=6,
                                  cuts=[SimpleNamespace(time=0), SimpleNamespace(time=2),
                                        SimpleNamespace(time=4)])
        transitions = [Transition(2, "fade", 1), Transition(4, "dissolve", 0.5)]
        validate_transitions(transitions, project)
        self.assertEqual(transition_window(transitions[0]), (1.5, 2.5))
        self.assertEqual(transitions[0].time, 2)
        self.assertEqual(transition_from_dict(transition_to_dict(transitions[0])), transitions[0])
        for transition in (Transition(2, "none"), Transition(2, "fade", 0),
                           Transition(2, "fade", 5.1), Transition(float("nan")),
                           Transition(-1), Transition(0.1, duration=0.5)):
            with self.subTest(transition=transition), self.assertRaises(ValueError):
                validate_transitions([transition])
        with self.assertRaisesRegex(ValueError, "重叠"):
            validate_transitions([Transition(2, duration=2), Transition(3, duration=2)])
        with self.assertRaisesRegex(ValueError, "切点"):
            validate_transitions([Transition(3)], project)
        with self.assertRaisesRegex(ValueError, "结尾"):
            validate_transitions([Transition(5.9)], project)
        self.assertEqual(transition_filter(Transition(2, "wipeleft")), "wipeleft")
        self.assertEqual(set(TRANSITION_KINDS), {"fade", "dissolve", "wipeleft", "wiperight",
                                              "slideleft", "slideright", "circleopen",
                                              "fadeblack", "fadewhite"})

    def test_filter_path_rejects_control_characters(self):
        for bad in ("a\x00b", "a\nb", "a\rb"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                escape_filter_path(bad)
        self.assertIn("\\\\:", escape_filter_path("C:\\look.cube"))


class CreativeFFmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = Path(__file__).resolve().parents[1] / "bin" / "ffmpeg.exe"
        if not cls.ffmpeg.is_file():
            raise unittest.SkipTest("Bundled FFmpeg is unavailable")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="creative_filters_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def render_rgb(self, chain="", source="color=c=0x606060:s=16x16:r=25:d=0.04"):
        command = [str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", source]
        if chain:
            command += ["-vf", chain]
        command += ["-frames:v", "1", "-pix_fmt", "rgb48le", "-f", "rawvideo", "pipe:1"]
        result = subprocess.run(command, capture_output=True, timeout=20,
                                creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(len(result.stdout), 16 * 16 * 6)
        return struct.unpack("<" + "H" * (len(result.stdout) // 2), result.stdout)

    def test_actual_exposure_saturation_and_white_balance_change_pixels(self):
        base = self.render_rgb()
        lighter = self.render_rgb(grade_filter(ColorGrade(exposure=1)))
        darker = self.render_rgb(grade_filter(ColorGrade(exposure=-1)))
        self.assertGreater(sum(lighter), sum(base) * 1.8)
        self.assertLess(sum(darker), sum(base) * 0.6)
        warm = self.render_rgb(grade_filter(ColorGrade(temperature=0.8)))
        cool = self.render_rgb(grade_filter(ColorGrade(temperature=-0.8)))
        self.assertGreater(warm[0], warm[2])
        self.assertLess(cool[0], cool[2])
        gray = self.render_rgb(grade_filter(ColorGrade(saturation=0)), "color=c=red:s=16x16:r=25:d=0.04")
        self.assertLess(max(gray[:3]) - min(gray[:3]), 16)

    def test_all_control_limits_are_legal_ffmpeg_filters(self):
        for name, (minimum, maximum) in GRADE_RANGES.items():
            for value in (minimum, maximum):
                with self.subTest(name=name, value=value):
                    self.render_rgb(grade_filter(replace(ColorGrade(), **{name: value})))
        self.render_rgb(grade_filter(ColorGrade(exposure=0.3, contrast=1.2, saturation=1.4,
                                               temperature=0.3, tint=-0.4, gamma=1.1,
                                               shadows=0.2, highlights=-0.3)))

    def _lut(self, suffix=".cube"):
        directory = self.root / "调色 [A],;"
        directory.mkdir(exist_ok=True)
        path = directory / ("look's file" + suffix)
        if suffix == ".cube":
            lines = ["TITLE \"Invert\"", "LUT_3D_SIZE 2", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
            lines += [f"{1-r} {1-g} {1-b}" for b in range(2) for g in range(2) for r in range(2)]
        else:
            # FFmpeg's .3dl reader accepts the 17 x 17 x 17 / 12-bit variant.
            lines = ["# Invert 17-point mesh", " ".join(str(index * 64) for index in range(17))]
            lines += [f"{4096-r*256} {4096-g*256} {4096-b*256}"
                      for r in range(17) for g in range(17) for b in range(17)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_cube_and_3dl_with_drive_unicode_apostrophe_and_filter_delimiters(self):
        base = self.render_rgb()
        for suffix in (".cube", ".3dl"):
            with self.subTest(suffix=suffix):
                path = self._lut(suffix)
                result = self.render_rgb(grade_filter(ColorGrade(lut_path=str(path))))
                self.assertGreater(sum(result), sum(base) * 1.4)
                self.assertLess(sum(result), sum(base) * 2.0)

    def test_ten_bit_grade_retains_more_than_8_bit_precision(self):
        # RGB48 ramp input includes values that differ only below the eighth
        # bit.  An implicit conversion through an 8-bit filter would collapse
        # these adjacent samples; the grade must retain their differences.
        samples = [12000 + index for index in range(256)]
        source = b"".join(struct.pack("<HHH", value, value, value) for value in samples)
        grades = [ColorGrade(exposure=0.5, contrast=1.1, gamma=1.2),
                  ColorGrade(saturation=1.4), ColorGrade(temperature=0.2, tint=0.1),
                  ColorGrade(shadows=0.2, highlights=-0.1),
                  ColorGrade(lut_path=str(self._lut()))]
        for grade in grades:
            with self.subTest(grade=grade):
                command = [str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
                           "-pix_fmt", "rgb48le", "-s", "16x16", "-r", "25", "-i", "pipe:0",
                           "-vf", grade_filter(grade), "-frames:v", "1", "-pix_fmt", "rgb48le",
                           "-f", "rawvideo", "pipe:1"]
                result = subprocess.run(command, input=source, capture_output=True, timeout=20,
                                        creationflags=CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
                red = struct.unpack("<" + "H" * (len(result.stdout) // 2), result.stdout)[::3]
                self.assertGreater(len(set(red)), 200)


if __name__ == "__main__":
    unittest.main()
