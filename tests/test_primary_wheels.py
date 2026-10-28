"""Primary-wheel persistence, validation and real high-depth image rendering."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creative_core import (ColorGrade, GRADE_RANGES, WHEEL_NAMES, WHEEL_RANGES,
                           grade_filter, grade_from_dict, grade_is_identity,
                           grade_to_dict, replace_wheel, validate_grade, wheel_values,
                           Transition, validate_transitions)
from creative_panel import CreativePanel
from exporter_core import CREATE_NO_WINDOW
from multicam_core import CameraClip, Cut, MulticamProject


class PrimaryWheelModelTests(unittest.TestCase):
    def test_neutral_defaults_old_projects_and_roundtrip(self):
        neutral = ColorGrade()
        self.assertEqual(len(WHEEL_RANGES), 16)
        self.assertEqual(set(GRADE_RANGES), {'exposure', 'contrast', 'saturation', 'temperature',
                                            'tint', 'gamma', 'shadows', 'highlights'})
        self.assertTrue(all(getattr(neutral, name) == 0 for name in WHEEL_RANGES))
        self.assertTrue(grade_is_identity(neutral))
        self.assertEqual(grade_filter(neutral), '')
        legacy = grade_from_dict({'exposure': 0.3, 'gamma': 1.2})
        self.assertEqual(legacy.exposure, .3)
        self.assertEqual(wheel_values(legacy, 'gamma'), dict(r=0., g=0., b=0., y=0.))
        grade = replace_wheel(legacy, 'lift', .12, -.1, .02, -.04)
        grade = replace_wheel(grade, 'gamma', .2, -.12, .04, .2)
        grade = replace_wheel(grade, 'gain', .08, .12, -.1, .15)
        grade = replace_wheel(grade, 'offset', -.02, .04, .02, .05)
        self.assertEqual(grade_from_dict(json.loads(json.dumps(grade_to_dict(grade)))), grade)
        self.assertFalse(grade_is_identity(grade))
        self.assertEqual(legacy.lift_r, 0)
        self.assertTrue(grade_is_identity(replace(grade, enabled=False)))
        self.assertEqual(grade_filter(replace(grade, enabled=False)), '')

    def test_numeric_bounds_and_combined_gamma_gain(self):
        for name, (minimum, maximum) in WHEEL_RANGES.items():
            for value in (minimum, maximum):
                with self.subTest(name=name, valid=value):
                    validate_grade(replace(ColorGrade(), **{name: value}))
            for value in (minimum-.001, maximum+.001, float('nan'), float('inf'), True, '0'):
                with self.subTest(name=name, invalid=value), self.assertRaises(ValueError):
                    validate_grade(replace(ColorGrade(), **{name: value}))
        validate_grade(ColorGrade(gamma_y=-.5, gamma_r=-.45))
        validate_grade(ColorGrade(gain_y=-.5, gain_r=-.5))
        for grade in (ColorGrade(gamma_y=-.5, gamma_r=-.451),
                      ColorGrade(gain_y=-.501, gain_r=-.5)):
            with self.subTest(grade=grade), self.assertRaises(ValueError):
                validate_grade(grade)
        for name in ('luma', '', None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                wheel_values(ColorGrade(), name)
            with self.subTest(name=name), self.assertRaises(ValueError):
                replace_wheel(ColorGrade(), name, 0, 0, 0, 0)
        with self.assertRaises(ValueError):
            replace_wheel(ColorGrade(), 'gamma', -.5, 0, 0, -.8)

    def test_manual_cut_accepts_transition_without_camera_change(self):
        project = SimpleNamespace(fps=30., duration=6.,
                                  cuts=[SimpleNamespace(time=0., camera=1)], edit_points=[2.])
        validate_transitions([Transition(2., duration=.5)], project)
        with self.assertRaisesRegex(ValueError, '切点'):
            validate_transitions([Transition(3., duration=.5)], project)


class SimpleVar:
    def __init__(self, value): self.value = value
    def get(self): return self.value
    def set(self, value): self.value = value


class LegacyPanelPreservationTests(unittest.TestCase):
    def panel(self, scope='全片'):
        panel = CreativePanel.__new__(CreativePanel)
        panel.loading = False
        grade = ColorGrade(lift_r=.2, gamma_y=.15, gain_b=-.1, offset_g=.03)
        self.original = copy.deepcopy(grade)
        panel.ws = SimpleNamespace(_available=lambda: True, project=MulticamProject(
            clips=[CameraClip('a', 1, 'a.mp4', 0., 6.), CameraClip('b', 2, 'b.mp4', 0., 6.)],
            cuts=[Cut(0., 1)], fps=30., global_grade=grade, camera_grades={2:copy.deepcopy(grade)}))
        panel.grade_scope_var = SimpleVar(scope)
        panel.grade_vars = {name: SimpleVar(str(getattr(grade, name))) for name in GRADE_RANGES}
        panel.grade_enabled_var = SimpleVar(True)
        panel.lut_var = SimpleVar('')
        self.commits, self.errors = [], []
        def commit(callback, status):
            callback()
            self.commits.append(status)
        panel._commit = commit
        panel._error = self.errors.append
        return panel

    def test_basic_panel_edits_preserve_all_wheels_and_reset_clears_them(self):
        for scope in ('全片', '机位 2'):
            with self.subTest(scope=scope):
                panel = self.panel(scope)
                panel.grade_vars['exposure'].set('.75')
                panel.apply_grade()
                grade = (panel.ws.project.global_grade if scope == '全片' else
                         panel.ws.project.camera_grades[2])
                self.assertEqual(grade.exposure, .75)
                self.assertEqual(self.errors, [])
                for name in WHEEL_RANGES:
                    self.assertEqual(getattr(grade, name), getattr(self.original, name))
                panel.reset_grade()
                grade = (panel.ws.project.global_grade if scope == '全片' else
                         panel.ws.project.camera_grades.get(2, ColorGrade()))
                self.assertEqual(grade, ColorGrade())

    def test_selection_scope_uses_workspace_transaction_without_second_undo(self):
        panel = self.panel('所选片段')
        panel.ws.selected_program_range = (2., 4.)
        panel.ws.get_selected_grade = lambda: copy.deepcopy(self.original)
        applied = []
        panel.ws.apply_selected_grade = applied.append
        panel._load_grade()
        panel.grade_vars['saturation'].set('.6')
        panel.apply_grade()
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0].saturation, .6)
        self.assertEqual(applied[0].lift_r, .2)
        self.assertEqual(self.commits, [])
        panel.reset_grade()
        self.assertEqual(applied[-1], ColorGrade())
        panel.ws.selected_program_range = None
        panel.apply_grade()
        self.assertEqual(len(self.errors), 1)


class PrimaryWheelFFmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = Path(__file__).resolve().parents[1] / 'bin' / 'ffmpeg.exe'
        if not cls.ffmpeg.is_file(): raise unittest.SkipTest('Bundled FFmpeg is unavailable')

    def render(self, grade, samples):
        source = b''.join(struct.pack('<HHH', *sample) for sample in samples)
        command = [str(self.ffmpeg), '-hide_banner', '-loglevel', 'error', '-f', 'rawvideo',
                   '-pix_fmt', 'rgb48le', '-s', '16x16', '-r', '25', '-i', 'pipe:0']
        chain = grade_filter(grade)
        if chain: command += ['-vf', chain]
        command += ['-frames:v', '1', '-pix_fmt', 'rgb48le', '-f', 'rawvideo', 'pipe:1']
        result = subprocess.run(command, input=source, capture_output=True, timeout=20,
                                creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(len(result.stdout), 16*16*6)
        values = struct.unpack('<'+'H'*(len(result.stdout)//2), result.stdout)
        return list(zip(values[::3], values[1::3], values[2::3]))

    def test_all_four_wheel_masters_and_rgb_actually_change_image(self):
        samples = [(20000, 24000, 28000)]*256
        for wheel in WHEEL_NAMES:
            with self.subTest(wheel=wheel, control='Y'):
                neutral = self.render(ColorGrade(), samples)
                result = self.render(replace_wheel(ColorGrade(), wheel, 0, 0, 0, .15), samples)
                self.assertTrue(all(result[0][channel] > neutral[0][channel] for channel in range(3)))
            for index, channel in enumerate('rgb'):
                with self.subTest(wheel=wheel, control=channel):
                    controls = {key: (.15 if key == channel else 0) for key in 'rgby'}
                    result = self.render(replace_wheel(ColorGrade(), wheel, **controls), samples)
                    self.assertGreater(result[0][index], samples[0][index])
                    for other in set(range(3))-{index}:
                        self.assertLessEqual(abs(result[0][other]-samples[0][other]), 1)

    def test_each_wheel_retains_sub_eight_bit_precision(self):
        samples = [(12000+index, 16000+index, 18000+index) for index in range(256)]
        for wheel in WHEEL_NAMES:
            with self.subTest(wheel=wheel):
                result = self.render(replace_wheel(ColorGrade(), wheel, .05, -.02, .04, .02), samples)
                self.assertGreater(len({sample[0] for sample in result}), 200)
                self.assertGreater(len({sample[1] for sample in result}), 200)
                self.assertGreater(len({sample[2] for sample in result}), 200)

    def test_ten_bit_yuv_pipeline_preserves_adjacent_input_values(self):
        luma = list(range(240, 496))
        source = struct.pack('<'+'H'*384, *(luma+[512]*128))
        for wheel in WHEEL_NAMES:
            with self.subTest(wheel=wheel):
                grade = replace_wheel(ColorGrade(), wheel, .02, .02, .02, .02)
                command = [str(self.ffmpeg), '-hide_banner', '-loglevel', 'error', '-f', 'rawvideo',
                           '-pix_fmt', 'yuv420p10le', '-s', '16x16', '-r', '25', '-i', 'pipe:0',
                           '-vf', grade_filter(grade), '-frames:v', '1', '-pix_fmt', 'yuv420p10le',
                           '-f', 'rawvideo', 'pipe:1']
                result = subprocess.run(command, input=source, capture_output=True, timeout=20,
                                        creationflags=CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
                self.assertEqual(len(result.stdout), 768)
                result_y = struct.unpack('<'+'H'*384, result.stdout)[:256]
                self.assertGreater(len(set(result_y)), 200)

    def test_wheel_limits_and_combined_basic_controls_are_valid_filters(self):
        samples = [(20000, 24000, 28000)]*256
        for name, bounds in WHEEL_RANGES.items():
            for value in bounds:
                with self.subTest(name=name, value=value):
                    self.render(replace(ColorGrade(), **{name:value}), samples)
        grade = ColorGrade(exposure=.4, contrast=1.1, saturation=1.2, gamma=1.1,
                           temperature=.2, tint=.1, shadows=.1, highlights=-.1,
                           lift_r=.02, gamma_g=.1, gain_b=-.1, offset_y=.03)
        self.assertNotEqual(self.render(grade, samples)[0], samples[0])


if __name__ == '__main__':
    unittest.main()
