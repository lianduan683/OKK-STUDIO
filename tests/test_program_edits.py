"""Local programme edits preserve independent ranges on the master clock."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creative_core import ColorGrade
from multicam_core import CameraClip, Cut, MulticamProject
from program_edits import (ClipGrade, grade_at, program_edits_from_dict, program_spans,
                           set_camera_on_range, set_grade_on_range, split_at)


def project(**changes):
    values = dict(clips=[CameraClip("a", 1, "a.mp4", 0, 6), CameraClip("b", 2, "b.mp4", 0, 6)],
                  cuts=[Cut(0, 1), Cut(4, 2)], fps=30)
    values.update(changes)
    return MulticamProject(**values)


class ProgramEditTests(unittest.TestCase):
    def test_manual_split_keeps_same_camera_and_is_idempotent(self):
        original = project()
        self.assertTrue(split_at(original, 2.008))
        self.assertFalse(split_at(original, 2.001))
        self.assertFalse(split_at(original, 0))
        self.assertFalse(split_at(original, 6))
        self.assertEqual([(s.start, s.end, s.camera) for s in program_spans(original)],
                         [(0, 2, 1), (2, 4, 1), (4, 6, 2)])
        segments = original.segments()
        self.assertEqual([s.frames for s in segments], [60, 60, 60])
        self.assertEqual([s.phase_origin for s in segments], [0, 0, 4])
        self.assertEqual([s.source_start for s in segments], [0, 2, 4])
        self.assertEqual(sum(s.frames for s in segments), 180)

    def test_replace_middle_camera_preserves_neighbours_and_manual_bounds(self):
        original = project()
        split_at(original, 2)
        set_camera_on_range(original, 2, 4, 2)
        self.assertEqual([original.camera_at(time) for time in (0, 1.96, 2, 3.96, 4, 5.96)],
                         [1, 1, 2, 2, 2, 2])
        self.assertEqual([(s.start, s.end, s.camera) for s in program_spans(original)],
                         [(0, 2, 1), (2, 4, 2), (4, 6, 2)])
        set_camera_on_range(original, 2, 4, 1)
        self.assertEqual([original.camera_at(time) for time in (1, 3, 5)], [1, 1, 2])
        self.assertEqual(original.edit_points, [2, 4])
        original.validate()

    def test_switch_arbitrary_range_keeps_all_external_camera_cuts(self):
        original = project(cuts=[Cut(0, 2), Cut(1, 1), Cut(3, 2), Cut(5, 1)])
        expected = {time: original.camera_at(time) for time in (.5, 1.5, 4.5, 5.5)}
        set_camera_on_range(original, 2, 4, 1)
        self.assertEqual({time: original.camera_at(time) for time in expected}, expected)
        self.assertEqual(original.camera_at(3.5), 1)
        self.assertIn(3, original.edit_points)
        self.assertEqual(original.camera_at(4), 2)

    def test_overwrite_grade_range_splits_old_range_and_copies_grade(self):
        original = project()
        warm = ColorGrade(temperature=.4)
        cool = ColorGrade(temperature=-.2)
        set_grade_on_range(original, 1, 5, warm)
        set_grade_on_range(original, 2, 3, cool)
        cool.temperature = .9
        self.assertEqual([(s.start, s.end, s.grade.temperature) for s in original.clip_grades],
                         [(1, 2, .4), (2, 3, -.2), (3, 5, .4)])
        self.assertIsNone(grade_at(original, .5))
        self.assertEqual(grade_at(original, 2).temperature, -.2)
        self.assertEqual(grade_at(original, 3).temperature, .4)
        self.assertIsNone(grade_at(original, 5))
        self.assertIsNone(grade_at(original, -.01))
        original.validate()

    def test_loaded_grade_edges_also_prevent_segment_merging(self):
        original = project(cuts=[], clip_grades=[ClipGrade(1, 2, ColorGrade(exposure=1))])
        self.assertEqual([s.start for s in original.segments()], [0, 1, 2])
        self.assertEqual([s.end for s in original.segments()], [1, 2, 6])
        self.assertEqual([(s.start, s.end) for s in program_spans(original)], [(0, 1), (1, 2), (2, 6)])

    def test_version_four_round_trip_and_relative_local_lut(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = project()
            original.clips[0].path = str(root / "a.mp4")
            original.clips[1].path = str(root / "b.mp4")
            set_grade_on_range(original, 1, 2, ColorGrade(lut_path=str(root / "looks" / "grade.cube")))
            original.save(root / "scene.okkmulti")
            raw = json.loads((root / "scene.okkmulti").read_text(encoding="utf-8"))
            self.assertEqual(raw["version"], 8)
            self.assertFalse(Path(raw["clip_grades"][0]["grade"]["lut_path"]).is_absolute())
            loaded = MulticamProject.load(root / "scene.okkmulti")
            self.assertEqual(loaded.edit_points, [1, 2])
            self.assertEqual(loaded.clip_grades[0].grade.lut_path, str(root / "looks" / "grade.cube"))
            for version in (1, 2):
                raw["version"] = version
                raw.pop("edit_points", None)
                raw.pop("clip_grades", None)
                (root / "old.okkmulti").write_text(json.dumps(raw), encoding="utf-8")
                old = MulticamProject.load(root / "old.okkmulti")
                self.assertEqual((old.edit_points, old.clip_grades), ([], []))

    def test_bad_ranges_edits_and_unknown_fields_are_rejected(self):
        for points, grades in (([True], []), (["invalid"], []), ([2, 2.001], []), ([0], []), ([6], []),
                               ([float("nan")], []), ([], [ClipGrade(1, 2, ColorGrade()), ClipGrade(1.5, 3, ColorGrade())]),
                               ([], [ClipGrade(1, 1.001, ColorGrade())]), ([], [{"start": 1}])):
            with self.subTest(points=points, grades=grades), self.assertRaises(ValueError):
                project(edit_points=points, clip_grades=grades).validate()
        for value in ({"unknown": []}, {"clip_grades": [{"start": 1, "end": 2, "grade": {}, "unknown": 1}]},
                      {"edit_points": [True]}, {"clip_grades": [{"start": 1, "end": 2, "grade": None}]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                program_edits_from_dict(value)
        for start, end in ((-1, 2), (1, 7), (2, 1), (2, 2.001)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                set_camera_on_range(project(), start, end, 1)
        with self.assertRaises(ValueError):
            set_camera_on_range(project(), 1, 2, 9)


if __name__ == "__main__":
    unittest.main()
