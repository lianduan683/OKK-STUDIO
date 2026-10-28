"""Frame clock, source offsets and independent shot grades for live mixing."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cache_paths import cache_dir
from creative_core import ColorGrade, Transition, TRANSITION_KINDS
from gpu_live_player import compile_live_transitions, missing_gpu_features, side_grades, subtitle_payload
from multicam_core import CameraClip, Cut, MulticamProject
from program_edits import set_grade_on_range
from subtitle_core import SubtitleCue


class LiveTransitionPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='live_plan_', dir=cache_dir('tests'))
        self.folder = Path(self.temp.name)
        self.a = self.folder/'a.mp4'; self.a.write_bytes(b'original A')
        self.b = self.folder/'b.mp4'; self.b.write_bytes(b'original B')
        self.project = MulticamProject(
            [CameraClip('a', 1, str(self.a), -2., 12.), CameraClip('b', 2, str(self.b), 1., 12.)],
            [Cut(0., 1), Cut(4., 2)], fps=30., master_duration=13., transitions=[Transition(4., 'fade', 2.)])
    def tearDown(self): self.temp.cleanup()

    def test_all_nine_kinds_use_export_frame_windows_and_aligned_native_times(self):
        for kind in TRANSITION_KINDS:
            with self.subTest(kind=kind):
                self.project.transitions = [Transition(4., kind, 2.)]
                plan, = compile_live_transitions(self.project)
                self.assertEqual((plan.start_frame, plan.cut_frame, plan.end_frame), (90, 120, 150))
                self.assertEqual((plan.progress(3.), plan.progress(4.), plan.progress(5.)), (0., .5, 1.))
                self.assertTrue(plan.contains(149/30)); self.assertFalse(plan.contains(5.))
                self.assertEqual(plan.outgoing.native_time(3.), 5.)
                self.assertEqual(plan.incoming.native_time(3.), 2.)
                self.assertEqual(plan.incoming.native_time(5.), 4.)
        self.assertEqual(self.a.read_bytes(), b'original A'); self.assertEqual(self.b.read_bytes(), b'original B')

    def test_odd_frame_duration_matches_centred_export_rounding(self):
        self.project.transitions = [Transition(4., 'fade', .5)]
        plan, = compile_live_transitions(self.project)
        self.assertEqual((plan.start_frame, plan.cut_frame, plan.end_frame), (113, 120, 128))

    def test_same_file_cut_uses_distinct_side_local_grades_and_selected_cut_draft(self):
        self.project.clips = [CameraClip('a', 1, str(self.a), 0., 12.)]
        self.project.cuts = [Cut(0., 1)]
        self.project.master_duration = 12.
        self.project.edit_points = [4.]
        set_grade_on_range(self.project, 0., 4., ColorGrade(exposure=-.4))
        set_grade_on_range(self.project, 4., 12., ColorGrade(exposure=.6))
        plan, = compile_live_transitions(self.project)
        self.assertEqual(plan.outgoing.asset_key, plan.incoming.asset_key)
        self.assertEqual(side_grades(self.project, plan.outgoing)[1].exposure, -.4)
        self.assertEqual(side_grades(self.project, plan.incoming)[1].exposure, .6)
        draft = (('selection', 0., 4.), ColorGrade(exposure=1.2))
        self.assertEqual(side_grades(self.project, plan.outgoing, draft)[1].exposure, 1.2)
        self.assertEqual(side_grades(self.project, plan.incoming, draft)[1].exposure, .6)

    def test_bound_proxy_keeps_source_epoch_and_original_export_paths(self):
        proxy = self.folder/'proxy.mp4'; proxy.write_bytes(b'proxy')
        self.project.clips[1].proxy_path = str(proxy)
        preview, = compile_live_transitions(self.project, use_proxies=True)
        export, = compile_live_transitions(self.project, use_proxies=False)
        self.assertEqual(preview.incoming.path, str(proxy))
        self.assertEqual(export.incoming.path, str(self.b))
        self.assertEqual(preview.incoming.source_epoch, export.incoming.source_epoch)

    def test_grade_edit_does_not_change_asset_plan_but_alignment_does(self):
        original, = compile_live_transitions(self.project)
        self.project.global_grade = ColorGrade(exposure=.5)
        graded, = compile_live_transitions(self.project)
        self.assertEqual(original.token, graded.token)
        self.project.clips[1].start = 1.1
        moved, = compile_live_transitions(self.project)
        self.assertNotEqual(original.token, moved.token)

    def test_missing_source_handles_and_inner_cuts_are_rejected(self):
        self.project.clips[1].start = 3.5
        with self.assertRaises(ValueError): compile_live_transitions(self.project)
        self.project.clips[1].start = 1.
        self.project.edit_points = [3.5]
        with self.assertRaisesRegex(ValueError, '其他切点'): compile_live_transitions(self.project)

    def test_stabilized_side_is_resolved_at_its_own_anchor_and_missing_entry_is_rejected(self):
        seen = []
        def resolve(camera, time, use_proxies=False):
            seen.append((camera, time))
            if camera == 2: return str(self.b), time-3.8, ('stabilized', 'b')
            return str(self.a), time+2., ('original', 'a')
        with patch.object(MulticamProject, 'media_at', side_effect=resolve):
            with self.assertRaisesRegex(ValueError, '入口余量'): compile_live_transitions(self.project)
        self.assertEqual(seen[0], (1, 119/30))
        self.assertEqual(seen[1], (2, 4.))

    def test_subtitle_and_lut_require_explicit_capabilities_and_disabled_effects_do_not(self):
        caps = {'transitions': list(TRANSITION_KINDS), 'grade_uniforms': True, 'subtitles': False, 'lut': False}
        self.project.subtitles.cues = [SubtitleCue('s', 3., 5., '单一后置字幕')]
        self.project.global_grade = ColorGrade(lut_path='style.cube')
        self.assertEqual(missing_gpu_features(self.project, caps), ('LUT', '字幕'))
        payload = subtitle_payload(self.project, 4.)
        self.assertEqual(payload['cues'][0]['text'], '单一后置字幕')
        self.assertEqual(payload['programme_time'], 4.)
        self.project.subtitles.enabled = False
        self.project.global_grade.enabled = False
        self.assertEqual(missing_gpu_features(self.project, caps), ())

    def test_stabilized_side_requires_enough_cached_exit_frames(self):
        def resolve(camera, time, use_proxies=False):
            if camera == 2: return str(self.b), time-2., ('stabilized', 'b')
            return str(self.a), time+2., ('original', 'a')
        with patch.object(MulticamProject, 'media_at', side_effect=resolve), \
             patch.object(MulticamProject, 'stabilization_at', return_value=SimpleNamespace(source_end=3.)):
            with self.assertRaisesRegex(ValueError, '出口余量'): compile_live_transitions(self.project)


if __name__ == '__main__': unittest.main(verbosity=2)
