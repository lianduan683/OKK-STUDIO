"""Render partitioning preserves every frame and exact source positions."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from multicam_render_plan import partition_render_segments


@dataclass(frozen=True, slots=True)
class Segment:
    camera: int
    path: str | None
    source_start: float
    start: float
    duration: float
    frames: int
    marker: str = "retained metadata"


class RenderPartitionTests(unittest.TestCase):
    def segment(self, frames, fps=60, **kwargs):
        fields = dict(camera=2, path="camera.mp4", source_start=13.25,
                      start=7.5, duration=frames/fps, frames=frames)
        fields.update(kwargs)
        return Segment(**fields)

    def test_three_hour_segment_is_bounded_for_parallel_dispatch(self):
        source = self.segment(650357)
        jobs = partition_render_segments([source], 60, 3)
        self.assertEqual(len(jobs), math.ceil(source.frames/3600))
        self.assertEqual(sum(job.frames for job in jobs), source.frames)
        self.assertEqual(jobs[-1].frames, source.frames % 3600)
        self.assertTrue(all(0 < job.frames <= 3600 for job in jobs))
        self.assertAlmostEqual(jobs[-1].start+jobs[-1].duration,
                               source.start+source.duration)
        self.assertEqual(source.frames, 650357)  # Original is immutable/unmodified.

    def test_fractional_rate_offsets_use_original_integer_frame_clock(self):
        fps = 60000/1001
        source = self.segment(650357, fps)
        jobs = partition_render_segments([source], fps, 4)
        offset = 0
        for job in jobs:
            self.assertIsInstance(job, Segment)
            self.assertEqual(job.camera, source.camera)
            self.assertEqual(job.path, source.path)
            self.assertEqual(job.marker, source.marker)
            self.assertEqual(job.start, source.start+offset/fps)
            self.assertEqual(job.source_start, source.source_start+offset/fps)
            self.assertEqual(job.duration, job.frames/fps)
            self.assertLessEqual(job.duration, 60)
            offset += job.frames
        self.assertEqual(offset, source.frames)

    def test_black_frames_are_partitioned_without_losing_their_identity(self):
        source = self.segment(7501, path=None, source_start=0)
        jobs = partition_render_segments([source], 60, 2)
        self.assertEqual([job.frames for job in jobs], [3600, 3600, 301])
        self.assertEqual([job.source_start for job in jobs], [0, 60, 120])
        self.assertTrue(all(job.path is None and job.camera == 2 for job in jobs))

    def test_short_and_exact_limit_segments_keep_original_objects(self):
        sources = [self.segment(1), self.segment(3599), self.segment(3600)]
        jobs = partition_render_segments(sources, 60, 3)
        self.assertEqual(len(jobs), 3)
        self.assertTrue(all(job is source for job, source in zip(jobs, sources)))

    def test_single_worker_retains_whole_segment(self):
        source = self.segment(650357)
        jobs = partition_render_segments(iter([source]), 60, 1)
        self.assertEqual(len(jobs), 1)
        self.assertIs(jobs[0], source)

    def test_cut_order_is_retained_without_merging_neighboring_sources(self):
        first = self.segment(3601, start=0)
        second = self.segment(1, start=first.duration, camera=3,
                              path="different.mp4", source_start=97)
        third = self.segment(3602, start=first.duration+second.duration)
        jobs = partition_render_segments([first, second, third], 60, 4)
        self.assertEqual([job.frames for job in jobs], [3600, 1, 1, 3600, 2])
        self.assertEqual([job.camera for job in jobs], [2, 2, 3, 2, 2])
        self.assertIs(jobs[2], second)
        self.assertEqual(jobs[3].source_start, third.source_start)

    def test_custom_chunk_limit_is_enforced(self):
        jobs = partition_render_segments([self.segment(67)], 60, 2, 0.5)
        self.assertEqual([job.frames for job in jobs], [30, 30, 7])

    def test_subframe_limit_keeps_one_frame_per_job(self):
        jobs = partition_render_segments([self.segment(3)], 60, 2, 0.001)
        self.assertEqual([job.frames for job in jobs], [1, 1, 1])

    def test_empty_input(self):
        self.assertEqual(partition_render_segments([], 60, 4), [])

    def test_known_30_to_5994_uses_complete_2000_output_frame_periods(self):
        fps = Fraction(60000, 1001)
        source = self.segment(4256, float(fps))
        jobs = partition_render_segments([source], fps, 3,
                                         source_frame_rates={source.path: Fraction(30)})
        self.assertEqual([job.frames for job in jobs], [2000, 2000, 256])
        self.assertIsInstance(jobs[0].duration, float)
        self.assertEqual(jobs[1].source_start, source.source_start+2000/float(fps))

    def test_known_2997_to_5994_aligns_chunks_to_even_output_frames(self):
        fps = Fraction(60000, 1001)
        source = self.segment(156, float(fps))
        jobs = partition_render_segments([source], fps, 2, 1,
                                         source_frame_rates={source.path: Fraction(30000, 1001)})
        self.assertEqual([job.frames for job in jobs], [58, 58, 40])

    def test_no_full_conversion_period_within_limit_retains_original(self):
        fps = Fraction(60000, 1001)
        source = self.segment(156, float(fps))
        jobs = partition_render_segments([source], fps, 2, 1,
                                         source_frame_rates={source.path: Fraction(30)})
        self.assertEqual(len(jobs), 1)
        self.assertIs(jobs[0], source)

    def test_unverified_source_does_not_split_even_if_another_path_is_known(self):
        unknown = self.segment(3601, path="unknown.mp4")
        known = self.segment(3601, path="known.mp4", start=unknown.duration)
        jobs = partition_render_segments([unknown, known], 60, 3,
                                         source_frame_rates={known.path: Fraction(60)})
        self.assertEqual([job.frames for job in jobs], [3601, 3600, 1])
        self.assertIs(jobs[0], unknown)

    def test_black_frames_need_no_verified_source_rate(self):
        source = self.segment(3601, path=None)
        jobs = partition_render_segments([source], 60, 3, source_frame_rates={})
        self.assertEqual([job.frames for job in jobs], [3600, 1])

    def test_invalid_verified_source_rate_rejected(self):
        source = self.segment(3601)
        with self.assertRaises(ValueError):
            partition_render_segments([source], 60, 3,
                                      source_frame_rates={source.path: Fraction(0)})

    def test_reject_invalid_planning_parameters(self):
        for fps in (True, 0, -30, math.inf, math.nan, "60"):
            with self.subTest(fps=fps), self.assertRaises(ValueError):
                partition_render_segments([], fps, 2)
        for workers in (True, 0, -1, 2.5, "3"):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                partition_render_segments([], 60, workers)
        for seconds in (True, 0, -1, math.inf, math.nan, "60"):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                partition_render_segments([], 60, 2, seconds)


if __name__ == "__main__":
    unittest.main()
