"""Group timeline edits preserve the master clock and remove stale references."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from creative_core import ColorGrade, Transition
from gyroflow_core import StabilizedRange
from library_overlay import LibraryOverlayRange
from multicam_core import CameraClip, Cut, MulticamProject
from program_edits import ClipGrade, grade_at, program_spans
from subtitle_core import SubtitleCue
from timeline_edits import (delete_source_clips, finalize_source_edit, move_source_clips,
                            program_boundaries_for_ranges, remove_program_boundaries,
                            source_move_delta)


def project():
    result = MulticamProject(clips=[CameraClip('a', 1, 'a.mov', 0, 10),
                                   CameraClip('b', 2, 'b.mov', 0, 6),
                                   CameraClip('c', 2, 'c.mov', 6, 4)],
        cuts=[Cut(0, 1), Cut(4, 2), Cut(8, 1)], master_audio='master.wav', master_duration=10,
        video_stack_enabled=True, video_stack_order=[2, 1], video_stack_hidden=[1],
        alignment_reference_camera=1, color_reference_camera=2)
    result.subtitles.cues = [SubtitleCue('s', 1, 3, '字幕')]
    return result


def overlay(source='a', camera=1, start=1, end=3):
    return LibraryOverlayRange(start, end, camera, source, 'overlay.mov', 'alpha', 1,
        0, 10, 'cache.mov', 'a' * 64, 1920, 1080, 30)


class TimelineSourceEditTests(unittest.TestCase):
    def test_delete_instances_keeps_other_camera_recordings_and_master_audio(self):
        value = project()
        value.stabilizations = [StabilizedRange(1, 3, 1, 'a', source_end=10, cache_path='cache.mov', signature='b' * 64),
                                StabilizedRange(1, 3, 2, 'b', source_end=6, cache_path='cache.mov', signature='c' * 64)]
        value.library_overlays = [overlay()]
        self.assertEqual(delete_source_clips(value, ['a']), 1)
        self.assertEqual([c.id for c in value.clips], ['b', 'c'])
        self.assertEqual([(c.time, c.camera) for c in value.cuts], [(4, 2)])
        self.assertEqual([s.source_id for s in value.stabilizations], ['b'])
        self.assertEqual(value.library_overlays, [])
        self.assertEqual(value.video_stack_order, [2])
        self.assertEqual(value.video_stack_hidden, [])
        self.assertIsNone(value.alignment_reference_camera)
        self.assertEqual(value.color_reference_camera, 2)
        self.assertEqual((value.master_audio, value.master_duration), ('master.wav', 10))
        self.assertEqual(value.subtitles.cues[0].start, 1)
        value.validate()

    def test_delete_one_of_two_source_instances_keeps_camera_cuts(self):
        value = project()
        delete_source_clips(value, ['b'])
        self.assertEqual(value.cameras, [1, 2])
        self.assertEqual([(c.time, c.camera) for c in value.cuts], [(0, 1), (4, 2), (8, 1)])
        self.assertEqual(value.video_stack_order, [2, 1])

    def test_delete_all_video_keeps_master_and_subtitles(self):
        value = project()
        self.assertEqual(delete_source_clips(value, ['a', 'b', 'c']), 3)
        self.assertEqual(value.clips, [])
        self.assertEqual(value.cuts, [])
        self.assertEqual(value.video_stack_order, [])
        self.assertEqual(value.duration, 10)
        self.assertEqual(value.master_audio, 'master.wav')
        self.assertEqual(len(value.subtitles.cues), 1)
        value.validate()

    def test_deleting_final_tail_trims_range_grades_and_transitions(self):
        value = project()
        value.master_audio, value.master_duration = '', 0
        value.range_start, value.range_end = 7, 10
        value.edit_points = [2, 7]
        value.clip_grades = [ClipGrade(2, 9, ColorGrade(exposure=1))]
        value.transitions = [Transition(2), Transition(8)]
        delete_source_clips(value, ['a', 'c'])
        self.assertEqual(value.duration, 6)
        self.assertEqual((value.range_start, value.range_end), (0, 6))
        self.assertEqual(value.edit_points, [2])
        self.assertEqual([(g.start, g.end) for g in value.clip_grades], [(2, 6)])
        self.assertEqual([t.time for t in value.transitions], [2])
        value.validate()

    def test_missing_id_does_not_partially_delete_group(self):
        value = project()
        before = deepcopy(value)
        with self.assertRaises(ValueError): delete_source_clips(value, ['a', 'missing'])
        self.assertEqual(value, before)

    def test_group_move_preserves_relative_offsets_and_other_clocks(self):
        value = project()
        value.stabilizations = [StabilizedRange(1, 3, 1, 'a', source_end=10, cache_path='cache.mov', signature='b' * 64)]
        before_cuts = deepcopy(value.cuts)
        self.assertAlmostEqual(move_source_clips(value, ['a', 'c'], 1.012), 1)
        self.assertEqual([(c.id, c.start) for c in value.clips], [('a', 1), ('b', 0), ('c', 7)])
        self.assertEqual(value.cuts, before_cuts)
        self.assertEqual((value.master_audio, value.master_duration), ('master.wav', 10))
        self.assertEqual((value.stabilizations[0].start, value.stabilizations[0].end), (2, 4))
        self.assertEqual(value.stabilizations[0].source_start, 0)
        self.assertEqual(value.subtitles.cues[0].start, 1)
        value.validate()

    def test_negative_source_start_clips_stabilization_without_changing_cache_offset(self):
        value = project()
        value.stabilizations = [StabilizedRange(1, 4, 1, 'a', source_end=10, cache_path='cache.mov', signature='b' * 64)]
        self.assertEqual(move_source_clips(value, ['a'], -2), -2)
        self.assertEqual(value.clips[0].start, -2)
        self.assertEqual((value.stabilizations[0].start, value.stabilizations[0].end), (0, 2))
        self.assertEqual((value.stabilizations[0].source_start, value.stabilizations[0].source_end), (0, 10))
        value.validate()

    def test_overlay_dependent_group_rejection_is_atomic(self):
        value = project()
        value.library_overlays = [overlay()]
        before = deepcopy(value)
        with self.assertRaisesRegex(ValueError, '叠加'):
            move_source_clips(value, ['a', 'b'], 2)
        self.assertEqual(value, before)

    def test_move_cannot_cover_another_instances_overlay(self):
        value = project()
        value.library_overlays = [overlay('b', 2, 1, 3)]
        before = deepcopy(value)
        with self.assertRaisesRegex(ValueError, '叠加'):
            move_source_clips(value, ['c'], -5)
        self.assertEqual(value, before)

    def test_noop_and_invalid_deltas_leave_project_unchanged(self):
        value = project()
        before = deepcopy(value)
        self.assertEqual(delete_source_clips(value, []), 0)
        self.assertEqual(move_source_clips(value, ['a'], .001), 0)
        self.assertEqual(move_source_clips(value, [], 2), 0)
        self.assertEqual(source_move_delta(value, ['a'], -.02), -1 / 30)
        for delta in (True, float('nan'), float('inf'), '1'):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                move_source_clips(value, ['a'], delta)
        self.assertEqual(value, before)

    def test_snapshot_restores_group_and_deleted_metadata(self):
        value = project()
        value.library_overlays = [overlay()]
        before = deepcopy(value)
        delete_source_clips(value, ['a', 'c'])
        value = before  # The UI's one-gesture undo restores this same snapshot.
        self.assertEqual([c.id for c in value.clips], ['a', 'b', 'c'])
        self.assertEqual(len(value.library_overlays), 1)
        self.assertEqual(value.video_stack_hidden, [1])
        value.validate()

    def test_changed_project_roundtrips_without_new_format(self):
        value = project()
        move_source_clips(value, ['a', 'b'], .5)
        delete_source_clips(value, ['c'])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'timeline.multicam.json'
            value.save(path)
            restored = MulticamProject.load(path)
        self.assertEqual([(c.id, c.start) for c in restored.clips], [('a', .5), ('b', .5)])
        self.assertEqual(restored.master_duration, 10)
        self.assertEqual(restored.subtitles.cues[0].text, '字幕')

    def test_finalizing_shortened_source_tail_is_separate_from_motion(self):
        value = project()
        value.master_audio, value.master_duration = '', 0
        value.range_start, value.range_end = 7, 10
        value.edit_points = [2, 7]
        move_source_clips(value, ['a', 'c'], -4)
        self.assertEqual(value.duration, 6)
        self.assertEqual(value.edit_points, [2, 7])  # Motion must not lose data on a return drag.
        finalize_source_edit(value)
        self.assertEqual(value.edit_points, [2])
        self.assertEqual((value.range_start, value.range_end), (0, 6))
        value.validate()


class TimelineProgramMergeTests(unittest.TestCase):
    def test_delete_middle_cut_extends_left_and_retains_later_shots_and_audio(self):
        value = project()
        value.cuts = [Cut(0, 1), Cut(2, 2), Cut(6, 1), Cut(8, 2)]
        original_clips = deepcopy(value.clips)
        original_cues = deepcopy(value.subtitles.cues)
        self.assertEqual(remove_program_boundaries(value, [2]), 1)
        self.assertEqual([value.camera_at(t) for t in (1, 3, 7, 9)], [1, 1, 1, 2])
        self.assertEqual([(s.start, s.end) for s in program_spans(value)], [(0, 6), (6, 8), (8, 10)])
        self.assertEqual(value.clips, original_clips)
        self.assertEqual(value.subtitles.cues, original_cues)
        self.assertEqual((value.master_audio, value.master_duration), ('master.wav', 10))
        value.validate()

    def test_local_grade_inherits_left_across_merge_and_restores_at_retained_edge(self):
        value = project()
        value.cuts = [Cut(0, 1), Cut(2, 2)]
        value.edit_points = [4, 6]
        value.clip_grades = [ClipGrade(0, 2, ColorGrade(exposure=.5)),
                             ClipGrade(2, 4, ColorGrade(exposure=1.5)),
                             ClipGrade(4, 6, ColorGrade(exposure=-.5))]
        remove_program_boundaries(value, [2])
        self.assertEqual(value.camera_at(3), 1)
        self.assertEqual(value.camera_at(5), 2)  # Grade-only retained boundary restores original camera.
        self.assertEqual(grade_at(value, 3).exposure, .5)
        self.assertEqual(grade_at(value, 5).exposure, -.5)
        self.assertIsNone(grade_at(value, 7))
        value.validate()

    def test_connected_multi_cut_deletion_uses_leftmost_shot(self):
        value = project()
        value.cuts = [Cut(0, 2), Cut(2, 1), Cut(4, 2), Cut(8, 1)]
        value.clip_grades = [ClipGrade(2, 4, ColorGrade(exposure=1))]
        value.transitions = [Transition(2), Transition(4), Transition(8)]
        self.assertEqual(remove_program_boundaries(value, [2.001, 4, 4]), 2)
        self.assertEqual([value.camera_at(t) for t in (1, 3, 5, 9)], [2, 2, 2, 1])
        self.assertIsNone(grade_at(value, 3))
        self.assertEqual([t.time for t in value.transitions], [8])
        self.assertEqual(value.duration, 10)
        value.validate()

    def test_invalid_or_missing_boundary_does_not_partially_merge(self):
        value = project()
        before = deepcopy(value)
        self.assertEqual(remove_program_boundaries(value, []), 0)
        for points in ([0], [10], [4, 3], [float('nan')], [True], '4'):
            with self.subTest(points=points), self.assertRaises(ValueError):
                remove_program_boundaries(value, points)
            self.assertEqual(value, before)

    def test_range_mapping_chooses_start_or_first_shots_end(self):
        value = project()
        self.assertEqual(program_boundaries_for_ranges(value, [(0, 4)]), [4])
        self.assertEqual(program_boundaries_for_ranges(value, [(4, 8), (8, 10)]), [4, 8])
        self.assertEqual(program_boundaries_for_ranges(value, [(0, 10)]), [])
        for ranges in ([(-1, 4)], [(2, 1)], [(2, 2.001)], [(True, 4)]):
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                program_boundaries_for_ranges(value, ranges)


if __name__ == '__main__':
    unittest.main()
