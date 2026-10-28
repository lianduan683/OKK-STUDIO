"""Shared natural-spline maths, project compatibility and real CPU pixels."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from color_curves import (DEFAULT_CURVE, MIN_CURVE_SPACING, canonical_curve,
                          curve_coefficients, evaluate_curve)
from creative_core import (ColorGrade, curve_points, grade_filter, grade_from_dict,
                           grade_is_identity, grade_to_dict, replace_curve, validate_grade)
from exporter_core import CREATE_NO_WINDOW
from multicam_core import CameraClip, Cut, MulticamProject
from program_edits import grade_at, set_grade_on_range


class NaturalCurveMathTests(unittest.TestCase):
    def test_identity_and_two_point_linear_coefficients(self):
        self.assertEqual(curve_coefficients(DEFAULT_CURVE), ((0., 1., 0., 1., 0., 0.),))
        curve = ((0., .2), (1., .8))
        for x in (0., .125, .5, .75, 1.):
            self.assertAlmostEqual(evaluate_curve(DEFAULT_CURVE, x), x)
            self.assertAlmostEqual(evaluate_curve(curve, x), .2+.6*x)
        self.assertEqual(evaluate_curve(curve, -1.), .2)
        self.assertAlmostEqual(evaluate_curve(curve, 2.), .8)

    def test_known_three_point_natural_spline_and_output_clipping(self):
        curve = ((0., 0.), (.5, 1.), (1., 0.))
        # Natural endpoint conditions give y=3x-4x^3 in the first interval.
        self.assertAlmostEqual(evaluate_curve(curve, .25), .6875)
        self.assertAlmostEqual(evaluate_curve(curve, .75), .6875)
        self.assertEqual(evaluate_curve(curve, .5), 1.)
        overshoot = ((0., 0.), (.1, 1.), (.9, 1.), (1., 0.))
        self.assertEqual(evaluate_curve(overshoot, .5), 1.)

    def test_spline_interpolates_eight_points_and_derivatives_are_continuous(self):
        curve = ((0., .05), (.1, .13), (.2, .1), (.35, .45),
                 (.5, .6), (.7, .54), (.9, .95), (1., .9))
        coefficients = curve_coefficients(curve)
        self.assertEqual(len(coefficients), 7)
        self.assertAlmostEqual(coefficients[0][4], 0.)
        for index, current in enumerate(coefficients):
            x0, x1, a, b, c, d = current
            width = x1-x0
            self.assertAlmostEqual(a, curve[index][1], places=12)
            self.assertAlmostEqual(a+width*(b+width*(c+width*d)), curve[index+1][1], places=12)
            if index+1 < len(coefficients):
                following = coefficients[index+1]
                self.assertAlmostEqual(b+2*c*width+3*d*width*width, following[3], places=11)
                self.assertAlmostEqual(2*c+6*d*width, 2*following[4], places=11)
            else:
                self.assertAlmostEqual(2*c+6*d*width, 0., places=11)

    def test_validation_rejects_bad_knots_and_keeps_immutable_canonical_values(self):
        self.assertEqual(canonical_curve([[0, 0], [.5, .6], [1, 1]]),
                         ((0., 0.), (.5, .6), (1., 1.)))
        invalid = [None, '', [], [(0, 0)], [(i/8, i/8) for i in range(9)],
                   [(0, 0), (.5, .5), (.4, .6), (1, 1)], [(0, 0), (0, .2), (1, 1)],
                   [(.1, 0), (1, 1)], [(0, 0), (.9, 1)], [(0, 0), (1, 1, 0)],
                   [(0, 0), (1, True)], [(0, 0), (1, '1')], [(0, 0), (1, float('nan'))],
                   [(0, 0), (1, float('inf'))], [(0, -.1), (1, 1)],
                   [(0, 0), (MIN_CURVE_SPACING/2, .2), (1, 1)]]
        for points in invalid:
            with self.subTest(points=points), self.assertRaises(ValueError):
                canonical_curve(points)
        for bad in (True, '0.5', float('inf'), float('nan')):
            with self.subTest(input=bad), self.assertRaises(ValueError):
                evaluate_curve(DEFAULT_CURVE, bad)


class CurveModelTests(unittest.TestCase):
    def test_old_grades_identity_disabled_and_roundtrip(self):
        grade = ColorGrade(lift_r=.1, exposure=.3)
        for channel in ('master', 'r', 'g', 'b'):
            self.assertEqual(curve_points(grade, channel), DEFAULT_CURVE)
        self.assertTrue(grade_is_identity(ColorGrade()))
        self.assertEqual(grade_filter(ColorGrade()), '')
        changed = replace_curve(grade, 'r', [(0, 0), (.5, .7), (1, 1)])
        changed = replace_curve(changed, 'master', ((0, .03), (.5, .6), (1, .96)))
        self.assertEqual(grade.lift_r, .1)
        self.assertEqual(grade.curve_r, DEFAULT_CURVE)
        self.assertFalse(grade_is_identity(changed))
        restored = grade_from_dict(json.loads(json.dumps(grade_to_dict(changed))))
        self.assertEqual(restored, changed)
        self.assertIsInstance(restored.curve_r, tuple)
        self.assertIsInstance(restored.curve_r[0], tuple)
        self.assertEqual(grade_from_dict({'exposure':.3}), ColorGrade(exposure=.3))
        self.assertEqual(grade_filter(replace(changed, enabled=False)), '')
        for channel in ('invalid', '', None):
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                replace_curve(grade, channel, DEFAULT_CURVE)
        with self.assertRaises(ValueError):
            grade_from_dict({'curve_master':[[0, 0], [.5, 2], [1, 1]]})

    def test_project_roundtrip_keeps_camera_and_only_selected_cut_curves(self):
        with tempfile.TemporaryDirectory(prefix='curve_project_') as folder:
            path = Path(folder)/'曲线.multicam.json'
            curve = ((0., 0.), (.3, .18), (.7, .85), (1., 1.))
            project = MulticamProject(clips=[CameraClip('a', 1, 'a.mp4', 0., 9.)],
                                      cuts=[Cut(0., 1)], fps=30., edit_points=[3., 6.])
            project.camera_grades[1] = replace_curve(ColorGrade(), 'g', curve)
            local_grade = replace_curve(ColorGrade(gain_y=.1), 'master', curve)
            set_grade_on_range(project, 3., 6., local_grade)
            project.save(path)
            loaded = MulticamProject.load(path)
            self.assertEqual(loaded.camera_grades[1], project.camera_grades[1])
            self.assertEqual(grade_at(loaded, 4.), local_grade)
            self.assertIsNone(grade_at(loaded, 1.))
            self.assertIsNone(grade_at(loaded, 7.))
            self.assertIsInstance(grade_at(loaded, 4.).curve_master, tuple)
            loaded_payload = json.loads(path.read_text(encoding='utf-8'))
            loaded_payload['version'] = 2
            loaded_payload.pop('edit_points')
            loaded_payload.pop('clip_grades')
            for record in [loaded_payload['global_grade'], *loaded_payload['camera_grades'].values()]:
                for channel in ('master','r','g','b'): record.pop('curve_'+channel)
            path.write_text(json.dumps(loaded_payload), encoding='utf-8')
            legacy = MulticamProject.load(path)
            self.assertEqual(legacy.camera_grades[1].curve_g, DEFAULT_CURVE)


class CurveCpuPixelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = Path(__file__).resolve().parents[1]/'bin'/'ffmpeg.exe'
        if not cls.ffmpeg.is_file(): raise unittest.SkipTest('Bundled FFmpeg is unavailable')

    def render(self, grade, samples):
        source = b''.join(struct.pack('<HHH', *sample) for sample in samples)
        command = [str(self.ffmpeg), '-hide_banner', '-loglevel', 'error', '-f', 'rawvideo',
                   '-pix_fmt', 'rgb48le', '-s', '32x32', '-r', '25', '-i', 'pipe:0']
        chain = grade_filter(grade)
        if chain: command += ['-vf', chain]
        command += ['-frames:v', '1', '-pix_fmt', 'rgb48le', '-f', 'rawvideo', 'pipe:1']
        result = subprocess.run(command, input=source, capture_output=True, timeout=20,
                                creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(len(result.stdout), 32*32*6)
        values = struct.unpack('<'+'H'*(len(result.stdout)//2), result.stdout)
        return list(zip(values[::3], values[1::3], values[2::3]))

    def test_each_curve_changes_actual_pixels_and_only_its_channel(self):
        curve = ((0., 0.), (.5, .7), (1., 1.))
        samples = [(24000, 28000, 32000)]*1024
        for index, channel in enumerate(('r','g','b','master')):
            with self.subTest(channel=channel):
                rendered = self.render(replace_curve(ColorGrade(), channel, curve), samples)[0]
                if channel == 'master':
                    self.assertTrue(all(output > original for output, original in zip(rendered, samples[0])))
                else:
                    self.assertGreater(rendered[index], samples[0][index])
                    for other in {0,1,2}-{index}:
                        self.assertLessEqual(abs(rendered[other]-samples[0][other]), 1)

    def test_cpu_pixels_match_shared_cubic_rgb_then_master_order(self):
        rgb_curve = ((0., 0.), (.25, .13), (.6, .76), (1., .95))
        master_curve = ((0., .03), (.3, .4), (.8, .73), (1., 1.))
        grade = replace_curve(ColorGrade(), 'r', rgb_curve)
        grade = replace_curve(grade, 'master', master_curve)
        samples = [(value, value, value) for value in range(0, 65536, 64)]
        rendered = self.render(grade, samples)
        for before, after in zip(samples, rendered):
            expected_r = evaluate_curve(master_curve, evaluate_curve(rgb_curve, before[0]/65535))
            expected_other = evaluate_curve(master_curve, before[1]/65535)
            self.assertLessEqual(abs(after[0]-round(expected_r*65535)), 6)
            self.assertLessEqual(abs(after[1]-round(expected_other*65535)), 4)

    def test_curve_keeps_more_than_eight_bit_precision_and_follows_basic_grade(self):
        grade = replace_curve(ColorGrade(exposure=.1, shadows=.1), 'master',
                              ((0.,0.), (.5,.62), (1.,1.)))
        chain = grade_filter(grade)
        self.assertLess(chain.index('lutrgb='), chain.index('format=gbrp16le'))
        self.assertLess(chain.index('curves=master='), chain.index('format=gbrp16le'))
        samples = [(12000+index, 16000+index, 20000+index) for index in range(1024)]
        rendered = self.render(grade, samples)
        self.assertGreater(len({sample[0] for sample in rendered}), 700)


if __name__ == '__main__': unittest.main()
