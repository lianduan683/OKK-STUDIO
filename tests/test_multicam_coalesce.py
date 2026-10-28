"""Remove redundant export seeks without changing edits or frame selection."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
import math
from pathlib import Path
import subprocess
import tempfile
import unittest

from cache_paths import cache_dir
from creative_core import ColorGrade, Transition
from creative_render import TransitionSegment, normal_effect_filter, plan_creative_segments
from gyroflow_core import StabilizedRange
from multicam_core import CameraClip, Cut, MulticamProject, MulticamSegment
from multicam_render_plan import coalesce_render_segments, partition_render_segments
from program_edits import set_grade_on_range
from subtitle_core import SubtitleCue, write_ass


class CoalescePlanTests(unittest.TestCase):
    def setUp(self):
        self.project = MulticamProject(clips=[CameraClip('a', 1, '8k-2997.mp4', 0., 12.)],
            cuts=[Cut(0., 1)], fps=30., edit_points=[211/30],
            camera_grades={1: ColorGrade(saturation=.9)}, global_grade=ColorGrade(exposure=.25))
        self.left = MulticamSegment(1, '8k-2997.mp4', 0., 0., 211/30, 211, 0.)
        self.right = MulticamSegment(1, '8k-2997.mp4', 211/30, 211/30, .1, 3, 0.)

    def test_three_frame_edit_reuses_same_8k_source_phase_without_mutating_project(self):
        before = deepcopy(self.project)
        merged = coalesce_render_segments(self.project, [self.left, self.right])
        self.assertEqual(merged, [replace(self.left, frames=214, duration=214/30)])
        self.assertEqual(self.project, before)
        self.assertEqual(self.right.frames, 3)
        self.assertEqual(merged[0].phase_origin, 0.)
        self.assertEqual(merged[0].source_start, 0.)

    def test_three_adjacent_jobs_merge_without_accumulating_float_durations(self):
        last = replace(self.right, source_start=214/30, start=214/30, frames=7, duration=7/30)
        merged, = coalesce_render_segments(self.project, [self.left, self.right, last])
        self.assertEqual(merged.frames, 221)
        self.assertEqual(merged.duration, 221/30)

    def test_source_camera_native_discontinuity_gap_or_overlap_are_kept(self):
        for changes in ({'path': 'other.mp4'}, {'camera': 2}, {'source_start': 211/30+.01},
                        {'source_start': 211/30+1e-8},
                        {'start': 212/30}, {'start': 210/30}):
            with self.subTest(changes=changes):
                right = replace(self.right, **changes)
                self.assertEqual(coalesce_render_segments(self.project, [self.left, right]), [self.left, right])

    def test_different_resampling_origins_and_none_origins_do_not_reset_frame_phase(self):
        for left, right in ((self.left, replace(self.right, phase_origin=211/30)),
                            (replace(self.left, phase_origin=None), replace(self.right, phase_origin=None))):
            with self.subTest(left=left.phase_origin, right=right.phase_origin):
                self.assertEqual(coalesce_render_segments(self.project, [left, right]), [left, right])
        # None means the first job's own start, compatible with an explicit
        # origin at that very same frame in the following job.
        self.assertEqual(len(coalesce_render_segments(self.project,
            [replace(self.left, phase_origin=None), self.right])), 1)

    def test_local_grade_change_stays_a_separate_export_job(self):
        set_grade_on_range(self.project, 0., 211/30, ColorGrade(exposure=-.2))
        set_grade_on_range(self.project, 211/30, 12., ColorGrade(exposure=.4))
        self.assertEqual(coalesce_render_segments(self.project, [self.left, self.right]), [self.left, self.right])

    def test_equal_local_grades_and_identity_disabled_layers_are_pixel_equivalent(self):
        set_grade_on_range(self.project, 0., 211/30, ColorGrade(exposure=.4))
        set_grade_on_range(self.project, 211/30, 12., ColorGrade(exposure=.4))
        self.assertEqual(len(coalesce_render_segments(self.project, [self.left, self.right])), 1)
        self.project.clip_grades[0].grade = ColorGrade(enabled=False, exposure=2.)
        self.project.clip_grades[1].grade = ColorGrade()
        self.assertEqual(len(coalesce_render_segments(self.project, [self.left, self.right])), 1)

    def test_transition_jobs_and_normal_material_across_a_transition_never_merge(self):
        effect = TransitionSegment(1, self.left.path, 0., 211/30, .1, 3, 1, self.right.path, 0., 'fade', 3, 0, 211/30)
        last = replace(self.right, source_start=214/30, start=214/30)
        self.assertEqual(coalesce_render_segments(self.project, [self.left, effect, last]), [self.left, effect, last])
        self.assertEqual(coalesce_render_segments(self.project, [effect, replace(effect, start=214/30)]),
                         [effect, replace(effect, start=214/30)])

    def test_stabilized_cache_changes_and_record_identity_remain_separate(self):
        self.assertEqual(len(coalesce_render_segments(self.project,
            [self.left, replace(self.right, path='stabilized.mp4')])), 2)
        self.project.stabilizations = [
            StabilizedRange(0., 211/30, 1, 'a', cache_path='shared.mp4', signature='first', source_end=12.),
            StabilizedRange(211/30, 12., 1, 'a', cache_path='shared.mp4', signature='second', source_end=12.)]
        left, right = replace(self.left, path='shared.mp4'), replace(self.right, path='shared.mp4')
        self.assertEqual(coalesce_render_segments(self.project, [left, right]), [left, right])

    def test_partial_range_retains_its_first_seek_phase_and_frame_count(self):
        left = replace(self.left, start=2., source_start=2., frames=151, duration=151/30)
        merged, = coalesce_render_segments(self.project, [left, self.right])
        self.assertEqual((merged.start, merged.source_start, merged.frames, merged.phase_origin), (2., 2., 154, 0.))

    def test_partition_after_coalescing_still_obeys_source_conversion_quantum(self):
        left = replace(self.left, frames=1800, duration=60.)
        right = replace(self.right, start=60., source_start=60., frames=500, duration=500/30)
        merged = coalesce_render_segments(self.project, [left, right])
        jobs = partition_render_segments(merged, 30., 2, source_frame_rates={left.path: Fraction(30000, 1001)})
        self.assertEqual([item.frames for item in jobs], [1001, 1001, 298])
        self.assertEqual(sum(item.frames for item in jobs), 2300)
        self.assertTrue(all(item.phase_origin == 0. for item in jobs))

    def test_burned_subtitles_preserve_original_decimal_timestamp_boundaries(self):
        self.project.subtitles.cues = [SubtitleCue('a', 6.9, 7.1, 'A')]
        self.assertEqual(coalesce_render_segments(self.project, [self.left, self.right]), [self.left, self.right])
        self.project.subtitles.burn_in = False
        self.assertEqual(len(coalesce_render_segments(self.project, [self.left, self.right])), 1)
        self.project.subtitles.burn_in = True
        self.project.subtitles.enabled = False
        self.assertEqual(len(coalesce_render_segments(self.project, [self.left, self.right])), 1)


class SoftwareFrameEquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = Path(__file__).resolve().parents[1]/'bin/ffmpeg.exe'
        if not cls.ffmpeg.is_file(): raise unittest.SkipTest('bundled software FFmpeg unavailable')
        cls.temp = tempfile.TemporaryDirectory(prefix='coalesce_frames_', dir=cache_dir('tests'))
        cls.folder = Path(cls.temp.name); cls.source = cls.folder/'2997.mp4'
        result = subprocess.run([str(cls.ffmpeg), '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'lavfi', '-i', 'testsrc2=size=96x54:rate=30000/1001:duration=8.2',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '0', '-bf', '0', '-g', '30',
            '-pix_fmt', 'yuv420p', '-video_track_timescale', '30000', str(cls.source)],
            capture_output=True, timeout=20, creationflags=0x08000000)
        if result.returncode: raise RuntimeError(result.stderr.decode('utf-8','replace')[-3000:])
    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def render(self, project, segments, ass_path=None):
        output = bytearray()
        for segment in segments:
            origin = segment.start if segment.phase_origin is None else segment.phase_origin
            prefix = math.floor((segment.start-origin)*30+.5)%1001
            seek = segment.source_start-prefix/30
            chain = ('setpts=PTS-STARTPTS,fps=fps=30/1:start_time=0,scale=96:54,setsar=1,'
                f'format=yuv420p,tpad=stop_mode=clone:stop_duration=10,trim=start_frame={prefix}:end_frame={prefix+segment.frames},'
                'settb=AVTB,setpts=N/(30/1*TB)')
            effects = normal_effect_filter(project, segment.camera, segment.start, ass_path)
            if effects: chain += ','+effects
            result = subprocess.run([str(self.ffmpeg), '-hide_banner', '-loglevel', 'error', '-filter_threads', '1',
                '-ss', f'{seek:.9f}', '-i', segment.path, '-an', '-vf', chain,
                '-frames:v', str(segment.frames), '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'],
                capture_output=True, timeout=20, creationflags=0x08000000)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8','replace')[-3000:])
            self.assertEqual(len(result.stdout), segment.frames*96*54*3)
            output.extend(result.stdout)
        return bytes(output)

    def fixture(self):
        project = MulticamProject(clips=[CameraClip('a', 1, str(self.source), 0., 8.2)],
            cuts=[Cut(0., 1)], fps=30., edit_points=[211/30], range_end=214/30,
            camera_grades={1: ColorGrade(saturation=.9)}, global_grade=ColorGrade(exposure=.2))
        return project, project.segments()

    def test_211_plus_three_frame_2997_to_30_output_is_byte_identical(self):
        project, original = self.fixture()
        self.assertEqual([item.frames for item in original], [211, 3])
        merged = coalesce_render_segments(project, original)
        self.assertEqual(len(merged), 1)
        self.assertEqual(self.render(project, original), self.render(project, merged))

    def test_partial_range_pixels_and_sampling_phase_are_byte_identical(self):
        project, _ = self.fixture()
        project.range_start = 2.
        original = project.segments()
        merged = coalesce_render_segments(project, original)
        self.assertEqual(sum(item.frames for item in merged), 154)
        self.assertEqual(len(merged), 1)
        self.assertEqual(self.render(project, original), self.render(project, merged))

    def test_subtitle_timestamps_are_byte_identical_by_retaining_separate_jobs(self):
        project, _ = self.fixture()
        project.range_start = 2.
        project.subtitles.cues = [SubtitleCue('a', 6.9, 7.1, 'A')]
        project.subtitles.style.font_size = 12
        project.subtitles.style.margin_y = 2
        ass = write_ass(project.subtitles, self.folder/'captions.ass', 96, 54)
        original = project.segments()
        merged = coalesce_render_segments(project, original)
        self.assertEqual(sum(item.frames for item in merged), 154)
        self.assertEqual(len(merged), 2)
        self.assertEqual(self.render(project, original, ass), self.render(project, merged, ass))


if __name__ == '__main__': unittest.main(verbosity=2)
