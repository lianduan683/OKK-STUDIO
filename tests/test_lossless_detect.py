from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import lossless_detect as ld

try:
    from lossless_core import CutSegment as CoreCutSegment
except ImportError:  # pragma: no cover - lossless_core is optional
    CoreCutSegment = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]


def _find_ffmpeg() -> str | None:
    candidate = ROOT / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if candidate.exists():
        return str(candidate)
    return shutil.which("ffmpeg")


FFMPEG = _find_ffmpeg()


def _pcm(*values: int) -> bytes:
    return struct.pack(f"<{len(values)}h", *values)


def _close(actual: float, expected: float, tolerance: float = 1e-6) -> bool:
    return abs(actual - expected) <= tolerance


class BlackdetectParserTests(unittest.TestCase):
    def test_parses_standard_log_sorts_and_merges(self) -> None:
        log = """
[Parsed_blackdetect_0 @ 0x1] black_start:2.0 black_end:3.0 black_duration:1.0
[Parsed_blackdetect_0 @ 0x1] black_start:-0.5 black_end:0.25 black_duration:0.75
[Parsed_blackdetect_0 @ 0x1] black_start:1.0 black_end:1.5 black_duration:0.5
[Parsed_blackdetect_0 @ 0x1] black_start:nan black_end:inf black_duration:nan
[Parsed_blackdetect_0 @ 0x1] black_start:3.0 black_end:4.0 black_duration:1.0
[Parsed_blackdetect_0 @ 0x1] black_start:1.5 black_end:2.0 black_duration:0.5
"""
        ranges = ld.parse_blackdetect(log, duration=10.0)
        self.assertEqual(
            [(item.start, item.end) for item in ranges],
            [(0.0, 0.25), (1.0, 4.0)],
        )

    def test_clips_negative_start_and_duration_and_drops_invalid(self) -> None:
        log = "\n".join(
            [
                "black_start:-2.0 black_end:-1.0 black_duration:1.0",
                "black_start:-1.0 black_end:0.5 black_duration:1.5",
                "black_start:9.5 black_end:12.0 black_duration:2.5",
                "black_start:inf black_end:nan black_duration:nan",
                "black_start:4.0 black_end:4.0 black_duration:0.0",
            ]
        )
        ranges = ld.parse_blackdetect(log, duration=10.0)
        self.assertEqual(
            [(item.start, item.end) for item in ranges],
            [(0.0, 0.5), (9.5, 10.0)],
        )

    def test_accepts_start_and_end_on_separate_lines(self) -> None:
        log = "black_start:1.25\nblack_end:3.75\nblack_duration:2.5"
        self.assertEqual(
            [(item.start, item.end) for item in ld.parse_blackdetect(log)],
            [(1.25, 3.75)],
        )

    def test_start_with_duration_only(self) -> None:
        log = "black_start:1.0 black_duration:2.0"
        self.assertEqual(
            [(item.start, item.end) for item in ld.parse_blackdetect(log)],
            [(1.0, 3.0)],
        )

    def test_unclosed_start_closes_at_duration_only_when_known(self) -> None:
        log = "black_start:5.0"
        self.assertEqual(ld.parse_blackdetect(log), [])
        self.assertEqual(
            [(item.start, item.end) for item in ld.parse_blackdetect(log, duration=8.0)],
            [(5.0, 8.0)],
        )

    def test_negative_values_on_a_complete_line_are_not_left_pending(self) -> None:
        log = "black_start:1.0 black_end:nan\nblack_start:2.0 black_end:3.0"
        self.assertEqual(
            [(item.start, item.end) for item in ld.parse_blackdetect(log, duration=10.0)],
            [(2.0, 3.0)],
        )

    def test_invalid_start_or_duration_does_not_consume_a_pending_start(self) -> None:
        log = "\n".join(
            [
                "black_start:1.0",
                "black_start:nan black_end:2.0",
                "black_start:3.0 black_duration:nan",
                "black_start:4.0 black_end:5.0",
            ]
        )
        self.assertEqual(
            [(item.start, item.end) for item in ld.parse_blackdetect(log, duration=10.0)],
            [(4.0, 5.0)],
        )

    def test_scientific_notation_and_case(self) -> None:
        log = "BLACK_START:1e-3 BLACK_END:2E-1 BLACK_DURATION:1.99e-1"
        self.assertEqual(
            [(item.start, item.end) for item in ld.parse_blackdetect(log)],
            [(0.001, 0.2)],
        )

    def test_empty_and_garbage_logs(self) -> None:
        self.assertEqual(ld.parse_blackdetect(""), [])
        self.assertEqual(ld.parse_blackdetect("no black frames here"), [])
        self.assertEqual(ld.parse_blackdetect(b""), [])

    def test_accepts_bytes_and_line_iterables(self) -> None:
        log = "black_start:1 black_end:2 black_duration:1"
        self.assertEqual(len(ld.parse_blackdetect(log.encode("utf-8"))), 1)
        self.assertEqual(len(ld.parse_blackdetect(log.splitlines())), 1)

    def test_merge_gap_and_min_duration(self) -> None:
        log = "\n".join(
            [
                "black_start:0 black_end:1 black_duration:1",
                "black_start:1.5 black_end:2.0 black_duration:0.5",
                "black_start:5 black_end:5.01 black_duration:0.01",
            ]
        )
        merged = ld.parse_blackdetect(log, merge_gap=0.6)
        self.assertEqual([(item.start, item.end) for item in merged], [(0.0, 2.0), (5.0, 5.01)])
        filtered = ld.parse_blackdetect(log, min_duration=0.4)
        self.assertEqual([(item.start, item.end) for item in filtered], [(0.0, 1.0), (1.5, 2.0)])

    def test_invalid_duration_and_merge_gap_raise(self) -> None:
        with self.assertRaises(ValueError):
            ld.parse_blackdetect("", duration=-1)
        with self.assertRaises(ValueError):
            ld.parse_blackdetect("", duration=float("nan"))
        with self.assertRaises(ValueError):
            ld.parse_blackdetect("", merge_gap=-0.1)

    def test_parse_blackdetect_segments_has_shared_fields(self) -> None:
        log = "black_start:1 black_end:2 black_duration:1"
        segments = ld.parse_blackdetect_segments(log, name="B", tags="black", selected=False)
        self.assertEqual(len(segments), 1)
        segment = segments[0]
        self.assertAlmostEqual(segment.start, 1.0)
        self.assertAlmostEqual(segment.end, 2.0)
        self.assertEqual(segment.name, "B")
        self.assertEqual(segment.tags, ("black",))
        self.assertFalse(segment.selected)

    def test_large_log_is_processed_linearly(self) -> None:
        lines = [
            f"black_start:{index * 2.0} black_end:{index * 2.0 + 1.0} black_duration:1.0"
            for index in range(50000)
        ]
        started = time.perf_counter()
        ranges = ld.parse_blackdetect("\n".join(lines), duration=100000.0)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(ranges), 50000)
        self.assertLess(elapsed, 10.0)


class SilencedetectParserTests(unittest.TestCase):
    def test_parses_standard_log(self) -> None:
        log = """
[silencedetect @ 0x1] silence_start: 0
[silencedetect @ 0x1] silence_end: 1.000125 | silence_duration: 1.000125
[silencedetect @ 0x1] silence_start: 3
[silencedetect @ 0x1] silence_end: 4 | silence_duration: 1
"""
        ranges = ld.parse_silencedetect(log, duration=4.0)
        self.assertEqual(len(ranges), 2)
        self.assertAlmostEqual(ranges[0].start, 0.0)
        self.assertAlmostEqual(ranges[0].end, 1.000125)
        self.assertEqual((ranges[1].start, ranges[1].end), (3.0, 4.0))

    def test_negative_nan_and_out_of_order(self) -> None:
        log = """
silence_start: 5
silence_end: 6 | silence_duration: 1
silence_start: -0.5
silence_end: 0.25 | silence_duration: 0.75
silence_start: nan
silence_end: inf | silence_duration: nan
silence_start: 2
silence_end: 3 | silence_duration: 1
"""
        self.assertEqual(
            [(item.start, item.end) for item in ld.parse_silencedetect(log, duration=10.0)],
            [(0.0, 0.25), (2.0, 3.0), (5.0, 6.0)],
        )

    def test_unclosed_silence_extends_to_duration(self) -> None:
        self.assertEqual(
            [(item.start, item.end) for item in ld.parse_silencedetect("silence_start: 7", duration=9.0)],
            [(7.0, 9.0)],
        )

    def test_duplicate_and_overlapping_silence_is_merged(self) -> None:
        log = """
silence_start: 1
silence_end: 3 | silence_duration: 2
silence_start: 2
silence_end: 4 | silence_duration: 2
"""
        self.assertEqual(
            [(item.start, item.end) for item in ld.parse_silencedetect(log)],
            [(1.0, 4.0)],
        )

    def test_empty_log(self) -> None:
        self.assertEqual(ld.parse_silencedetect(""), [])
        self.assertEqual(ld.parse_silencedetect("\n\n"), [])

    def test_parse_silencedetect_segments(self) -> None:
        segments = ld.parse_silencedetect_segments("silence_start:1\nsilence_end:2 | silence_duration:1")
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].name, "静音")
        self.assertIn("silence", segments[0].tags)


class SceneParserTests(unittest.TestCase):
    SELECT_LOG = """
[Parsed_select_0 @ 0x1] n:76.000000 pts:38912.000000 t:3.040000 key:0 interlace_type:P pict_type:P scene:1.000000 -> select:1.000000 select_out:0
[Parsed_select_0 @ 0x1] n:25.000000 pts:12800.000000 t:1.000000 key:1 interlace_type:P pict_type:I scene:1.000000 -> select:1.000000 select_out:0
[Parsed_select_0 @ 0x1] n:0.000000 pts:0.000000 t:-0.040000 key:1 interlace_type:P pict_type:I scene:0.500000 -> select:0.500000 select_out:0
[Parsed_select_0 @ 0x1] n:5.000000 pts:0.000000 t:1.000000 key:1 scene:nan -> select:nan select_out:-1
"""

    def test_parses_select_debug_log(self) -> None:
        events = ld.parse_scene_log(self.SELECT_LOG, duration=4.0)
        self.assertEqual([event.time for event in events], [0.0, 1.0, 3.04])
        self.assertEqual([event.score for event in events], [0.5, 1.0, 1.0])
        self.assertEqual([event.frame for event in events], [0, 25, 76])
        self.assertEqual(ld.parse_scene_cuts(self.SELECT_LOG, duration=4.0), [0.0, 1.0, 3.04])

    def test_unselected_select_debug_frames_are_ignored(self) -> None:
        log = """
[Parsed_select_0 @ 0x1] n:1.000000 t:1.0 scene:0.9 -> select:0.9 select_out:-1
[Parsed_select_0 @ 0x1] n:2.000000 t:2.0 scene:0.9 -> select:0.9 select_out:0
"""
        self.assertEqual(ld.parse_scene_cuts(log), [2.0])

    def test_duplicate_time_keeps_highest_score(self) -> None:
        log = """
[Parsed_select_0 @ 0x1] n:10.000000 t:1.0 scene:0.4 -> select:0.4 select_out:0
[Parsed_select_0 @ 0x1] n:10.000000 t:1.00001 scene:0.9 -> select:0.9 select_out:0
"""
        events = ld.parse_scene_log(log)
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0].time, 1.00001)
        self.assertAlmostEqual(events[0].score or 0.0, 0.9)

    def test_parses_showinfo_without_scores(self) -> None:
        log = """
[Parsed_showinfo_1 @ 0x1] config in time_base: 1/12800, frame_rate: 25/1
[Parsed_showinfo_1 @ 0x1] n:   1 pts:  38912 pts_time:3.04 duration:512 fmt:yuv420p
[Parsed_showinfo_1 @ 0x1] n:   0 pts:  12800 pts_time:1 duration:512 fmt:yuv420p
"""
        events = ld.parse_scene_log(log, duration=4.0)
        self.assertEqual([event.time for event in events], [1.0, 3.04])
        self.assertEqual([event.score for event in events], [None, None])
        self.assertEqual([event.frame for event in events], [0, 1])

    def test_parses_scdet_score_and_time(self) -> None:
        log = """
[Parsed_scdet_0 @ 0x1] lavfi.scd.score: 85.547, lavfi.scd.time: 1
[Parsed_scdet_0 @ 0x1] lavfi.scd.score=42.0, lavfi.scd.time=2.5
[Parsed_scdet_0 @ 0x1] lavfi.scd.score: nan, lavfi.scd.time: 9
"""
        events = ld.parse_scene_log(log, duration=4.0)
        self.assertEqual([event.time for event in events], [1.0, 2.5])
        self.assertEqual([event.score for event in events], [85.547, 42.0])

    def test_filename_containing_scene_does_not_hide_showinfo_frames(self) -> None:
        log = """
Input #0, mov, from 'scene:1.mov':
[Parsed_showinfo_1 @ 0x1] n:0 pts:100 pts_time:2.0 duration:1 fmt:yuv420p
"""
        self.assertEqual(ld.parse_scene_cuts(log), [2.0])

    def test_duration_filtering_and_negative_clamping(self) -> None:
        log = """
[Parsed_select_0 @ 0x1] n:1 t:-2.0 scene:0.9 -> select:0.9 select_out:0
[Parsed_select_0 @ 0x1] n:2 t:5.0 scene:0.9 -> select:0.9 select_out:0
[Parsed_select_0 @ 0x1] n:3 t:4.0 scene:0.9 -> select:0.9 select_out:0
"""
        self.assertEqual(ld.parse_scene_cuts(log, duration=4.0), [0.0, 4.0])

    def test_empty_garbage_and_invalid_log(self) -> None:
        self.assertEqual(ld.parse_scene_log(""), [])
        self.assertEqual(ld.parse_scene_log("frame= 100 fps=25"), [])
        self.assertEqual(ld.parse_scene_log("scene:nan"), [])
        with self.assertRaises(ValueError):
            ld.parse_scene_log("", duration=-1)

    def test_scene_ranges_cover_the_whole_timeline(self) -> None:
        ranges = ld.scene_ranges_from_cuts(
            [2, 1, 2, 0, 4, 5, -1, float("nan")],
            4.0,
        )
        self.assertEqual(
            [(item.start, item.end) for item in ranges],
            [(0.0, 1.0), (1.0, 2.0), (2.0, 4.0)],
        )

    def test_scene_ranges_min_duration_keeps_coverage(self) -> None:
        ranges = ld.scene_ranges_from_cuts([1, 2], 4.0, min_duration=1.5)
        self.assertEqual(
            [(item.start, item.end) for item in ranges],
            [(0.0, 2.0), (2.0, 4.0)],
        )
        self.assertEqual(ld.scene_ranges_from_cuts([], 3.0), [ld.TimeRange(0.0, 3.0)])

    def test_single_numeric_cut_is_accepted(self) -> None:
        self.assertEqual(
            [(item.start, item.end) for item in ld.scene_ranges_from_cuts(2.0, 4.0)],
            [(0.0, 2.0), (2.0, 4.0)],
        )
        self.assertEqual(
            [(item.start, item.end) for item in ld.scene_ranges_from_cuts(ld.SceneEvent(2.0), 4.0)],
            [(0.0, 2.0), (2.0, 4.0)],
        )

    def test_scene_cut_segments(self) -> None:
        segments = ld.scene_cut_segments([1, 2], 4.0, name="S", tags=("scene", "cut"))
        self.assertEqual([(item.start, item.end) for item in segments], [(0.0, 1.0), (1.0, 2.0), (2.0, 4.0)])
        self.assertTrue(all(item.name == "S" for item in segments))
        self.assertTrue(all(item.tags == ("scene", "cut") for item in segments))

    def test_invalid_scene_duration(self) -> None:
        with self.assertRaises(ValueError):
            ld.scene_ranges_from_cuts([], 0)
        with self.assertRaises(ValueError):
            ld.scene_ranges_from_cuts([], float("inf"))


class CutSegmentTests(unittest.TestCase):
    def test_segment_fields_and_dict(self) -> None:
        segment = ld.CutSegment(1, 2.5, "A", ("tag", "tag", "other"), False)
        self.assertEqual(segment.start, 1.0)
        self.assertEqual(segment.end, 2.5)
        self.assertEqual(segment.name, "A")
        self.assertEqual(segment.tags, ("tag", "other"))
        self.assertFalse(segment.selected)
        self.assertEqual(
            segment.to_dict(),
            {
                "start": 1.0,
                "end": 2.5,
                "name": "A",
                "tags": ["tag", "other"],
                "selected": False,
            },
        )
        self.assertEqual(ld.CutSegment.from_mapping(segment.to_dict()), segment)

    def test_mapping_tags_are_preserved_for_core_compatibility(self) -> None:
        segment = ld.CutSegment(1, 2, tags={"camera": "A"})
        self.assertEqual(segment.tags, {"camera": "A"})
        self.assertEqual(segment.to_dict()["tags"], {"camera": "A"})
        self.assertEqual(ld.coerce_cut_segment(segment.to_dict()).tags, {"camera": "A"})
        segments = ld.ranges_to_cut_segments([(0, 1)], tags={"kind": "black"})
        self.assertEqual(segments[0].tags, {"kind": "black"})
        self.assertFalse(ld.CutSegment(1, 2, selected="false").selected)

    def test_rejects_nan_inf_and_empty_ranges(self) -> None:
        for start, end in (
            (float("nan"), 1),
            (0, float("inf")),
            (2, 1),
            (1, 1),
        ):
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValueError):
                    ld.CutSegment(start, end)

    def test_coerces_foreign_objects_and_tuples(self) -> None:
        foreign = SimpleNamespace(start=1, end=2, name="X", tags=["a"], selected=False)
        segment = ld.coerce_cut_segment(foreign)
        self.assertEqual((segment.start, segment.end, segment.name), (1.0, 2.0, "X"))
        self.assertEqual(segment.tags, ("a",))
        self.assertFalse(segment.selected)
        tuple_segment = ld.coerce_cut_segment((3, 4, "Y", "b", True))
        self.assertEqual((tuple_segment.start, tuple_segment.end), (3.0, 4.0))
        self.assertEqual(tuple_segment.tags, ("b",))
        mapping = ld.coerce_cut_segment({"start": 5, "end": 6})
        self.assertEqual((mapping.name, mapping.tags, mapping.selected), ("", (), True))

    def test_coerce_rejects_malformed_values(self) -> None:
        with self.assertRaises(ValueError):
            ld.coerce_cut_segment((1,))
        with self.assertRaises(ValueError):
            ld.coerce_cut_segment({"start": None, "end": 2})
        with self.assertRaises(ValueError):
            ld.coerce_cut_segment({"start": "nan", "end": 2})

    def test_ranges_to_cut_segments(self) -> None:
        segments = ld.ranges_to_cut_segments(
            [(1, 2), (2, 3)],
            name="cut",
            tags="one",
            selected=False,
            min_duration=0.0,
        )
        self.assertEqual([(item.start, item.end) for item in segments], [(1.0, 3.0)])
        self.assertEqual(segments[0].name, "cut")
        self.assertEqual(segments[0].tags, ("one",))
        self.assertFalse(segments[0].selected)

    def test_ranges_to_cut_segments_custom_factory(self) -> None:
        class OtherSegment:
            def __init__(self, start, end, name, tags, selected):
                self.start = start
                self.end = end
                self.name = name
                self.tags = tags
                self.selected = selected

        segments = ld.ranges_to_cut_segments([(1, 2)], factory=OtherSegment)
        self.assertIsInstance(segments[0], OtherSegment)
        self.assertEqual(segments[0].name, "")

    def test_segment_is_duck_type_compatible(self) -> None:
        segment = ld.CutSegment(1, 2)
        for field in ("start", "end", "name", "tags", "selected"):
            self.assertTrue(hasattr(segment, field))


class RangeHelperTests(unittest.TestCase):
    def test_normalize_clamps_sorts_merges_and_filters(self) -> None:
        ranges = ld.normalize_ranges(
            [
                (5, 6),
                (1, 2),
                (-1, 0.5),
                (2, 4),
                (9, 9),
                (9.5, 12),
            ],
            duration=10,
            merge_gap=0.0,
            min_duration=0.1,
        )
        self.assertEqual(
            [(item.start, item.end) for item in ranges],
            [(0.0, 0.5), (1.0, 4.0), (5.0, 6.0), (9.5, 10.0)],
        )

    def test_normalize_accepts_single_range_and_generator(self) -> None:
        self.assertEqual(
            [(item.start, item.end) for item in ld.normalize_ranges((1, 2))],
            [(1.0, 2.0)],
        )
        self.assertEqual(
            [(item.start, item.end) for item in ld.normalize_ranges((item for item in [(1, 2), (3, 4)]))],
            [(1.0, 2.0), (3.0, 4.0)],
        )

    def test_normalize_accepts_single_foreign_object(self) -> None:
        foreign = SimpleNamespace(start=1, end=2)
        self.assertEqual(
            [(item.start, item.end) for item in ld.normalize_ranges(foreign)],
            [(1.0, 2.0)],
        )

    def test_normalize_rejects_nan_inf(self) -> None:
        with self.assertRaises(ValueError):
            ld.normalize_ranges([(float("nan"), 1)])
        with self.assertRaises(ValueError):
            ld.normalize_ranges([(0, float("inf"))])

    def test_subtract_ranges(self) -> None:
        pieces = ld.subtract_ranges([(0, 10)], [(2, 3), (5, 6)])
        self.assertEqual(
            [(item.start, item.end) for item in pieces],
            [(0.0, 2.0), (3.0, 5.0), (6.0, 10.0)],
        )
        pieces = ld.subtract_ranges([(0, 10)], [(2, 5), (4, 6)])
        self.assertEqual(
            [(item.start, item.end) for item in pieces],
            [(0.0, 2.0), (6.0, 10.0)],
        )
        pieces = ld.subtract_ranges([(0, 10)], [(-2, 12)])
        self.assertEqual(pieces, [])

    def test_subtract_ranges_min_duration(self) -> None:
        pieces = ld.subtract_ranges([(0, 10)], [(1, 9)], min_duration=0.5)
        self.assertEqual(
            [(item.start, item.end) for item in pieces],
            [(0.0, 1.0), (9.0, 10.0)],
        )
        self.assertEqual(ld.subtract_ranges([(0, 10)], [(1, 9)], min_duration=1.5), [])

    def test_normalize_can_keep_touching_ranges_separate(self) -> None:
        merged = ld.normalize_ranges([(0, 1), (1, 2)])
        self.assertEqual([(item.start, item.end) for item in merged], [(0.0, 2.0)])
        separate = ld.normalize_ranges([(0, 1), (1, 2)], merge_touching=False)
        self.assertEqual(
            [(item.start, item.end) for item in separate],
            [(0.0, 1.0), (1.0, 2.0)],
        )

    def test_invert_ranges(self) -> None:
        pieces = ld.invert_ranges([(2, 3), (5, 6)], 8)
        self.assertEqual(
            [(item.start, item.end) for item in pieces],
            [(0.0, 2.0), (3.0, 5.0), (6.0, 8.0)],
        )

    def test_time_range_is_tuple_like(self) -> None:
        item = ld.TimeRange(1, 2)
        start, end = item
        self.assertEqual((start, end), (1.0, 2.0))
        self.assertEqual(item[0], 1.0)
        self.assertEqual(item[-1], 2.0)
        self.assertEqual(item.duration, 1.0)
        self.assertEqual(item.midpoint, 1.5)
        self.assertTrue(item.contains(1.5))
        self.assertTrue(item.overlaps(ld.TimeRange(1.5, 3)))
        self.assertEqual(item, (1.0, 2.0))
        self.assertEqual(ld.parse_blackdetect("black_start:1 black_end:2 black_duration:1"), [(1.0, 2.0)])
        with self.assertRaises(IndexError):
            _ = item[3]


class SegmentEditTests(unittest.TestCase):
    def _base(self) -> ld.CutSegment:
        return ld.CutSegment(0, 10, "A", ("keep",), False)

    def test_cut_out_ranges_preserves_fields(self) -> None:
        result = ld.cut_out_ranges([self._base()], [(2, 3), (5, 6)])
        self.assertEqual(
            [(item.start, item.end) for item in result],
            [(0.0, 2.0), (3.0, 5.0), (6.0, 10.0)],
        )
        for item in result:
            self.assertEqual(item.name, "A")
            self.assertEqual(item.tags, ("keep",))
            self.assertFalse(item.selected)

    def test_cut_out_ranges_adds_tags_and_overrides(self) -> None:
        result = ld.cut_out_ranges(
            [self._base()],
            [(2, 3)],
            add_tags=("cut", "keep"),
            name="B",
            selected=True,
        )
        self.assertEqual([(item.start, item.end) for item in result], [(0.0, 2.0), (3.0, 10.0)])
        self.assertTrue(all(item.name == "B" for item in result))
        self.assertTrue(all(item.tags == ("keep", "cut") for item in result))
        self.assertTrue(all(item.selected for item in result))

    def test_single_segment_tuple_is_accepted(self) -> None:
        result = ld.cut_out_ranges((0, 10), [(2, 3)])
        self.assertEqual([(item.start, item.end) for item in result], [(0.0, 2.0), (3.0, 10.0)])
        result = ld.split_segments_by_scene((0, 10), [5])
        self.assertEqual([(item.start, item.end) for item in result], [(0.0, 5.0), (5.0, 10.0)])

    def test_cut_out_ranges_without_overlap_keeps_original(self) -> None:
        original = self._base()
        result = ld.cut_out_ranges([original], [(20, 30)], min_duration=5.0)
        self.assertEqual(len(result), 1)
        self.assertIs(result[0], original)

    def test_cut_segments_by_black_ranges_and_log(self) -> None:
        result = ld.cut_segments_by_black([self._base()], [(2, 3)])
        self.assertEqual([(item.start, item.end) for item in result], [(0.0, 2.0), (3.0, 10.0)])
        log = "black_start:4 black_end:5 black_duration:1"
        result = ld.cut_segments_by_black([self._base()], log, duration=10)
        self.assertEqual([(item.start, item.end) for item in result], [(0.0, 4.0), (5.0, 10.0)])

    def test_cut_segments_by_black_applies_merge_gap_to_ranges(self) -> None:
        result = ld.cut_segments_by_black([self._base()], [(0, 1), (1.2, 2)], merge_gap=0.3)
        self.assertEqual([(item.start, item.end) for item in result], [(2.0, 10.0)])

    def test_cut_segments_by_silence_overlapping_black(self) -> None:
        log = "silence_start: 3\nsilence_end: 5 | silence_duration: 2"
        result = ld.cut_segments_by_silence([self._base()], log, duration=10)
        self.assertEqual([(item.start, item.end) for item in result], [(0.0, 3.0), (5.0, 10.0)])

    def test_cut_segments_by_black_or_silence(self) -> None:
        result = ld.cut_segments_by_black_or_silence(
            [self._base()],
            [(2, 4)],
            [(3, 6)],
            duration=10,
        )
        self.assertEqual([(item.start, item.end) for item in result], [(0.0, 2.0), (6.0, 10.0)])
        only_silence = ld.cut_segments_by_black_or_silence(
            [self._base()],
            [(2, 4)],
            [(3, 6)],
            remove_black=False,
            duration=10,
        )
        self.assertEqual([(item.start, item.end) for item in only_silence], [(0.0, 3.0), (6.0, 10.0)])
        only_black = ld.cut_segments_by_black_or_silence(
            [self._base()],
            [(2, 4)],
            [(3, 6)],
            remove_silence=False,
            duration=10,
        )
        self.assertEqual([(item.start, item.end) for item in only_black], [(0.0, 2.0), (4.0, 10.0)])

    def test_combined_helper_accepts_single_range_objects(self) -> None:
        result = ld.cut_segments_by_black_or_silence(
            [self._base()],
            ld.TimeRange(2, 3),
            duration=10,
        )
        self.assertEqual([(item.start, item.end) for item in result], [(0.0, 2.0), (3.0, 10.0)])

    def test_min_duration_drops_micro_pieces(self) -> None:
        result = ld.cut_segments_by_black([self._base()], [(1, 9)], min_duration=1.5)
        self.assertEqual(result, [])

    def test_split_segments_by_scene(self) -> None:
        result = ld.split_segments_by_scene([self._base()], [0, 2, 4, 10])
        self.assertEqual(
            [(item.start, item.end) for item in result],
            [(0.0, 2.0), (2.0, 4.0), (4.0, 10.0)],
        )
        self.assertTrue(all(item.name == "A" for item in result))

    def test_split_segments_by_scene_min_duration(self) -> None:
        result = ld.split_segments_by_scene([self._base()], [2, 4], min_duration=2.5)
        self.assertEqual(
            [(item.start, item.end) for item in result],
            [(0.0, 4.0), (4.0, 10.0)],
        )

    def test_split_segments_by_scene_log(self) -> None:
        log = "[Parsed_showinfo_1 @ 0x1] n:   0 pts:1 pts_time:5 duration:1"
        result = ld.split_segments_by_scene([self._base()], log, duration=10)
        self.assertEqual([(item.start, item.end) for item in result], [(0.0, 5.0), (5.0, 10.0)])

    def test_split_preserves_tag_and_selected_overrides(self) -> None:
        result = ld.split_segments_by_scene(
            [self._base()],
            [5],
            name="shot",
            add_tags="scene",
            selected=True,
        )
        self.assertEqual(len(result), 2)
        for item in result:
            self.assertEqual(item.name, "shot")
            self.assertEqual(item.tags, ("keep", "scene"))
            self.assertTrue(item.selected)


class CommandBuilderTests(unittest.TestCase):
    def test_blackdetect_command(self) -> None:
        command = ld.build_blackdetect_command(
            "ffmpeg",
            "in.mov",
            min_duration=0.5,
            picture_threshold=0.7,
            pixel_threshold=0.2,
            start=1.0,
            duration=2.0,
        )
        self.assertEqual(command[0], "ffmpeg")
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertGreater(command.index("-t"), command.index("-i"))
        self.assertIn("blackdetect=d=0.5:pix_th=0.2:pic_th=0.7", command)
        self.assertEqual(command[-3:], ["-f", "null", "-"])
        self.assertIn("-map", command)
        self.assertEqual(command[command.index("-map") + 1], "0:v:0")

    def test_blackdetect_rejects_invalid_thresholds(self) -> None:
        for kwargs in (
            {"picture_threshold": -0.1},
            {"picture_threshold": 1.1},
            {"pixel_threshold": -0.1},
            {"pixel_threshold": 1.1},
            {"min_duration": -1},
            {"min_duration": float("nan")},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ld.build_blackdetect_command("ffmpeg", "in.mov", **kwargs)
        with self.assertRaises(ValueError):
            ld.build_blackdetect_command("", "in.mov")
        with self.assertRaises(ValueError):
            ld.build_blackdetect_command("ffmpeg", "")

    def test_silencedetect_command(self) -> None:
        command = ld.build_silencedetect_command(
            "ffmpeg",
            "in.mov",
            noise_db=-40,
            min_duration=0.25,
        )
        self.assertIn("silencedetect=noise=-40dB:d=0.25", command)
        self.assertIn("-af", command)
        self.assertEqual(command[command.index("-map") + 1], "0:a:0")
        with self.assertRaises(ValueError):
            ld.build_silencedetect_command("ffmpeg", "in.mov", noise_db=float("nan"))

    def test_scene_detect_command_select_and_scdet(self) -> None:
        select = ld.build_scene_detect_command("ffmpeg", "in.mov", threshold=0.35)
        self.assertIn("select='gt(scene,0.35)',showinfo", select)
        scdet = ld.build_scene_detect_command("ffmpeg", "in.mov", threshold=0.35, method="scdet")
        self.assertIn("scdet=threshold=35", scdet)
        with self.assertRaises(ValueError):
            ld.build_scene_detect_command("ffmpeg", "in.mov", threshold=1.1)
        with self.assertRaises(ValueError):
            ld.build_scene_detect_command("ffmpeg", "in.mov", method="other")

    def test_thumbnail_command(self) -> None:
        command = ld.build_thumbnail_command(
            "ffmpeg",
            "in.mov",
            1.5,
            "out.jpg",
            width=120,
            height=-2,
            quality=6,
        )
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertIn("scale=120:-2", command)
        self.assertEqual(command[command.index("-frames:v") + 1], "1")
        self.assertEqual(command[command.index("-q:v") + 1], "6")
        self.assertEqual(command[-1], "out.jpg")
        fast = ld.build_thumbnail_command("ffmpeg", "in.mov", 1.5, "out.jpg", accurate=False)
        self.assertIn("-noaccurate_seek", fast)
        self.assertLess(fast.index("-noaccurate_seek"), fast.index("-i"))

    def test_thumbnail_command_validation(self) -> None:
        for kwargs in (
            {"timestamp": -1},
            {"width": 0},
            {"width": 80.5},
            {"height": 0},
            {"height": 80.5},
            {"height": -3},
            {"quality": 0},
            {"quality": 32},
        ):
            with self.subTest(kwargs=kwargs):
                values = {"ffmpeg": "ffmpeg", "source": "in.mov", "timestamp": 1, "output": "out.jpg"}
                values.update(kwargs)
                with self.assertRaises(ValueError):
                    ld.build_thumbnail_command(**values)

    def test_timeline_thumbnail_times_and_batch(self) -> None:
        times = ld.timeline_thumbnail_times(0, 4, 4)
        self.assertEqual(times, [0.5, 1.5, 2.5, 3.5])
        self.assertEqual(ld.timeline_thumbnail_times(2, 4, 1), [3.0])
        with self.assertRaises(ValueError):
            ld.timeline_thumbnail_times(4, 4, 1)
        with self.assertRaises(ValueError):
            ld.timeline_thumbnail_times(0, 4, 0)
        commands = ld.build_timeline_thumbnail_commands(
            "ffmpeg",
            "in.mov",
            times,
            "thumbs",
            prefix="shot",
            extension="png",
            width=80,
        )
        self.assertEqual(len(commands), 4)
        self.assertTrue(commands[0][1].replace("\\", "/").endswith("shot_0001.png"))
        self.assertIn("scale=80:-2", commands[0][2])

    def test_timeline_sheet_command(self) -> None:
        command = ld.build_timeline_thumbnail_sheet_command(
            "ffmpeg",
            "in.mov",
            "sheet.jpg",
            start=0,
            end=4,
            count=10,
            columns=5,
            width=80,
        )
        self.assertIn("fps=2.500000,scale=80:-2,tile=5x2", command)
        self.assertEqual(command[command.index("-frames:v") + 1], "1")
        self.assertEqual(command[-1], "sheet.jpg")
        with self.assertRaises(ValueError):
            ld.build_timeline_thumbnail_sheet_command("ffmpeg", "in.mov", "sheet.jpg", end=0)

    def test_waveform_command(self) -> None:
        command = ld.build_waveform_command(
            "ffmpeg",
            "in.mov",
            sample_rate=8000,
            start=1.0,
            duration=2.0,
        )
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertGreater(command.index("-t"), command.index("-i"))
        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertEqual(command[command.index("-ar") + 1], "8000")
        self.assertEqual(command[command.index("-f") + 1], "s16le")
        self.assertEqual(command[-1], "pipe:1")
        with self.assertRaises(ValueError):
            ld.build_waveform_command("ffmpeg", "in.mov", sample_rate=0)


class WaveformTests(unittest.TestCase):
    def test_iter_peak_windows(self) -> None:
        windows = list(
            ld.iter_peak_windows(
                [_pcm(0, 100, -200, 30000)],
                sample_rate=4,
                window_seconds=0.5,
            )
        )
        self.assertEqual(len(windows), 2)
        self.assertEqual([window.peak for window in windows], [100.0, 30000.0])
        self.assertTrue(_close(windows[0].rms, math.sqrt(5000.0)))
        self.assertTrue(_close(windows[1].rms, math.sqrt(450020000.0)))
        self.assertTrue(_close(windows[0].start, 0.0))
        self.assertTrue(_close(windows[1].start, 0.5))
        self.assertTrue(_close(windows[1].end, 1.0))

    def test_chunk_boundaries_including_odd_bytes(self) -> None:
        raw = _pcm(0, 100, -200, 30000, 5, -6, 7, 8)
        whole = list(ld.iter_peak_windows([raw], sample_rate=4, window_seconds=0.5))
        chunks = [raw[0:1], raw[1:4], raw[4:9], raw[9:]]
        split = list(ld.iter_peak_windows(chunks, sample_rate=4, window_seconds=0.5))
        self.assertEqual(
            [(item.index, item.start, item.peak, item.rms) for item in whole],
            [(item.index, item.start, item.peak, item.rms) for item in split],
        )

    def test_partial_last_window_and_odd_trailing_byte(self) -> None:
        windows = list(
            ld.iter_peak_windows(
                [_pcm(1000, -1000, 2000) + b"\x01"],
                sample_rate=4,
                window_seconds=0.5,
            )
        )
        self.assertEqual([window.peak for window in windows], [1000.0, 2000.0])
        self.assertTrue(_close(windows[1].duration, 0.25))
        waveform = ld.waveform_from_pcm(
            [_pcm(1000, -1000, 2000)],
            sample_rate=4,
            window_seconds=0.5,
        )
        self.assertEqual(len(waveform), 2)
        self.assertTrue(_close(waveform.duration, 0.75))

    def test_stereo_channels(self) -> None:
        windows = list(
            ld.iter_peak_windows(
                [_pcm(100, -200, 300, -400)],
                sample_rate=2,
                window_seconds=0.5,
                channels=2,
            )
        )
        self.assertEqual([window.peak for window in windows], [200.0, 400.0])
        self.assertTrue(_close(windows[0].rms, math.sqrt(25000.0)))
        self.assertTrue(_close(windows[1].rms, math.sqrt(125000.0)))

    def test_empty_and_rms_disabled(self) -> None:
        self.assertEqual(list(ld.iter_peak_windows([b""], sample_rate=4, window_seconds=0.5)), [])
        empty = ld.waveform_from_pcm(b"", sample_rate=4, window_seconds=0.5)
        self.assertEqual(len(empty), 0)
        self.assertEqual(empty.duration, 0.0)
        self.assertEqual(empty.sample(5), empty)
        windows = list(
            ld.iter_peak_windows(
                [_pcm(1, 2)],
                sample_rate=4,
                window_seconds=0.5,
                include_rms=False,
            )
        )
        self.assertEqual(windows[0].rms, 0.0)
        waveform = ld.waveform_from_pcm(
            [_pcm(1, 2)],
            sample_rate=4,
            window_seconds=0.5,
            include_rms=False,
        )
        self.assertEqual(waveform.rms, ())

    def test_waveform_queries(self) -> None:
        waveform = ld.waveform_from_pcm(
            [_pcm(0, 100, -200, 30000)],
            sample_rate=4,
            window_seconds=0.5,
        )
        self.assertEqual(waveform.peaks, (100.0, 30000.0))
        self.assertEqual(waveform.peak_at(-1), 100.0)
        self.assertEqual(waveform.peak_at(0.6), 30000.0)
        self.assertEqual(waveform.peak_at(100), 30000.0)
        self.assertEqual(waveform.peak_at(float("nan")), 0.0)
        self.assertEqual(waveform.max_peak, 30000.0)
        self.assertTrue(_close(waveform.time_at(1), 0.5))
        self.assertTrue(_close(waveform.normalized_peaks()[1], 30000.0 / 32768.0))
        self.assertEqual(len(waveform.to_pairs()), 2)

    def test_sampling_keeps_maximum(self) -> None:
        self.assertEqual(ld.sample_peaks([1, 5, 2, 8], 2), [5.0, 8.0])
        self.assertEqual(ld.sample_peaks([1, 2], 5), [1.0, 2.0])
        self.assertEqual(ld.sample_peaks([], 2), [])
        with self.assertRaises(ValueError):
            ld.sample_peaks([1], 0)
        with self.assertRaises(ValueError):
            ld.sample_peaks([float("nan")], 2)
        waveform = ld.waveform_from_pcm(
            [_pcm(1, 2, 3, 4)],
            sample_rate=4,
            window_seconds=0.25,
        )
        sampled = waveform.sample(2)
        self.assertEqual(len(sampled), 2)
        self.assertEqual(sampled.peaks, (2.0, 4.0))
        self.assertTrue(_close(sampled.duration, waveform.duration))
        self.assertTrue(_close(sampled.window_seconds, 0.5))

    def test_pcm_peaks_alias(self) -> None:
        waveform = ld.pcm_peaks(_pcm(10, 20), sample_rate=2, window_seconds=1.0)
        self.assertEqual(waveform.peaks, (20.0,))

    def test_invalid_waveform_parameters(self) -> None:
        with self.assertRaises(ValueError):
            ld.waveform_from_pcm(b"", sample_rate=0)
        with self.assertRaises(ValueError):
            ld.waveform_from_pcm(b"", sample_rate=4, window_seconds=0)
        with self.assertRaises(ValueError):
            ld.waveform_from_pcm(b"", sample_rate=4, channels=0)
        with self.assertRaises(ValueError):
            ld.generate_waveform("ffmpeg", "in.mov", max_points=0)

    def test_three_hour_waveform_is_bounded_and_correct(self) -> None:
        samples = 100 * 10800
        raw = b"\x00\x40" * samples
        waveform = ld.waveform_from_pcm(
            raw,
            sample_rate=100,
            window_seconds=1.0,
            include_rms=False,
        )
        self.assertEqual(len(waveform), 10800)
        self.assertTrue(_close(waveform.duration, 10800.0))
        self.assertEqual(waveform.peaks[0], 16384.0)


@unittest.skipUnless(CoreCutSegment is not None, "需要 lossless_core")
class CoreSegmentIntegrationTests(unittest.TestCase):
    def _source(self) -> object:
        assert CoreCutSegment is not None
        return CoreCutSegment(
            id="source01",
            start=0.0,
            end=10.0,
            name="A",
            tags={"camera": "A"},
            selected=True,
        )

    def test_cut_out_ranges_preserves_core_type_id_and_tags(self) -> None:
        result = ld.cut_out_ranges([self._source()], [(2, 3)])
        self.assertEqual(len(result), 2)
        self.assertTrue(all(isinstance(item, CoreCutSegment) for item in result))
        self.assertNotEqual(result[0].id, result[1].id)
        self.assertNotEqual(result[0].id, "source01")
        for item in result:
            self.assertEqual(item.name, "A")
            self.assertEqual(item.tags, {"camera": "A"})
            self.assertTrue(item.selected)
        self.assertEqual([(item.start, item.end) for item in result], [(0.0, 2.0), (3.0, 10.0)])

    def test_split_segments_by_scene_preserves_core_type(self) -> None:
        result = ld.split_segments_by_scene([self._source()], [4, 7])
        self.assertTrue(all(isinstance(item, CoreCutSegment) for item in result))
        self.assertEqual(len({item.id for item in result}), 3)
        self.assertEqual([(item.start, item.end) for item in result], [(0.0, 4.0), (4.0, 7.0), (7.0, 10.0)])
        self.assertTrue(all(item.tags == {"camera": "A"} for item in result))

    def test_no_edit_returns_the_same_core_object(self) -> None:
        source = self._source()
        self.assertIs(ld.cut_out_ranges([source], [])[0], source)
        self.assertIs(ld.split_segments_by_scene([source], [])[0], source)

    def test_to_core_segments_from_detected_ranges(self) -> None:
        segments = ld.to_core_segments(
            ld.parse_blackdetect_segments("black_start:1 black_end:2 black_duration:1")
        )
        self.assertEqual(len(segments), 1)
        self.assertIsInstance(segments[0], CoreCutSegment)
        self.assertEqual(segments[0].tags, {"black": "1"})
        self.assertTrue(segments[0].selected)
        self.assertEqual(segments[0].name, "黑场")
        single = ld.to_core_segments(ld.TimeRange(1, 2))
        self.assertEqual(len(single), 1)
        self.assertEqual((single[0].start, single[0].end), (1.0, 2.0))

    def test_cut_out_ranges_can_add_tags_to_core_mapping(self) -> None:
        result = ld.cut_out_ranges([self._source()], [(2, 3)], add_tags="detected")
        self.assertTrue(all(item.tags == {"camera": "A", "detected": "1"} for item in result))

    def test_ranges_to_cut_segments_with_core_factory(self) -> None:
        assert CoreCutSegment is not None
        result = ld.ranges_to_cut_segments(
            [(1, 2)],
            name="detected",
            tags="black",
            selected=True,
            factory=CoreCutSegment,
        )
        self.assertIsInstance(result[0], CoreCutSegment)
        self.assertEqual(result[0].tags, {"black": "1"})
        self.assertTrue(result[0].selected)


@unittest.skipUnless(FFMPEG, "需要 FFmpeg 才能运行真实样本测试")
class RealFFmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory()
        cls.sample = str(Path(cls._temp.name) / "sample.mov")
        cls.encoder = cls._generate_sample(cls.sample)
        if cls.encoder is None:
            cls._temp.cleanup()
            raise unittest.SkipTest("无法生成 FFmpeg 测试样本")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    @staticmethod
    def _generate_sample(path: str) -> str | None:
        video = (
            "color=c=black:s=160x120:r=25:d=4,"
            "drawbox=enable='between(t,1,3)':color=white:t=fill"
        )
        audio = r"aevalsrc=if(between(t\,1\,3)\,0.5*sin(2*PI*440*t)\,0):s=8000:d=4"
        base = [
            FFMPEG,
            "-hide_banner",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            video,
            "-f",
            "lavfi",
            "-i",
            audio,
        ]
        attempts = (
            (
                "libx264",
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-g",
                    "25",
                    "-keyint_min",
                    "25",
                    "-sc_threshold",
                    "0",
                ],
            ),
            ("mpeg4", ["-c:v", "mpeg4", "-q:v", "2"]),
        )
        for encoder, video_args in attempts:
            command = base + video_args + ["-c:a", "pcm_s16le", "-shortest", path]
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=ld.CREATE_NO_WINDOW,
            )
            if completed.returncode == 0 and Path(path).exists():
                return encoder
        return None

    def test_real_blackdetect(self) -> None:
        ranges = ld.detect_black(
            FFMPEG,
            self.sample,
            min_duration=0.3,
            parse_duration=4.0,
        )
        self.assertGreaterEqual(len(ranges), 2)
        self.assertTrue(any(abs(item.start) < 0.15 and abs(item.end - 1.0) < 0.2 for item in ranges))
        self.assertTrue(any(abs(item.start - 3.04) < 0.3 and abs(item.end - 3.96) < 0.3 for item in ranges))

    def test_real_silencedetect(self) -> None:
        ranges = ld.detect_silence(
            FFMPEG,
            self.sample,
            noise_db=-40,
            min_duration=0.3,
            parse_duration=4.0,
        )
        self.assertGreaterEqual(len(ranges), 2)
        self.assertTrue(any(item.start <= 0.05 and abs(item.end - 1.0) < 0.1 for item in ranges))
        self.assertTrue(any(abs(item.start - 3.0) < 0.1 and abs(item.end - 4.0) < 0.1 for item in ranges))

    def test_real_scene_select(self) -> None:
        cuts = ld.detect_scene_cuts(
            FFMPEG,
            self.sample,
            threshold=0.2,
            parse_duration=4.0,
        )
        self.assertGreaterEqual(len(cuts), 1)
        self.assertEqual(cuts, sorted(cuts))
        self.assertTrue(any(abs(cut - 1.0) < 0.25 or abs(cut - 3.0) < 0.35 for cut in cuts))

    def test_real_scdet_scores(self) -> None:
        events = ld.detect_scenes(
            FFMPEG,
            self.sample,
            threshold=0.2,
            method="scdet",
            parse_duration=4.0,
        )
        self.assertGreaterEqual(len(events), 1)
        self.assertTrue(all(event.score is not None and event.score > 0 for event in events))
        self.assertTrue(all(0.0 <= event.time <= 4.0 for event in events))

    def test_real_waveform(self) -> None:
        waveform = ld.generate_waveform(
            FFMPEG,
            self.sample,
            sample_rate=8000,
            window_seconds=0.1,
        )
        self.assertEqual(len(waveform), 40)
        self.assertTrue(_close(waveform.duration, 4.0, tolerance=0.15))
        self.assertLess(waveform.peak_at(0.5), 100.0)
        self.assertGreater(waveform.peak_at(2.0), 8000.0)
        sampled = waveform.sample(10)
        self.assertLessEqual(len(sampled), 10)
        self.assertTrue(_close(sampled.duration, waveform.duration))

    def test_real_thumbnail(self) -> None:
        output = Path(self._temp.name) / "thumb.png"
        command = ld.build_thumbnail_command(
            FFMPEG,
            self.sample,
            1.5,
            str(output),
            width=80,
        )
        subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.assertTrue(output.exists())
        self.assertGreater(output.stat().st_size, 100)

    def test_real_timeline_sheet(self) -> None:
        output = Path(self._temp.name) / "sheet.jpg"
        command = ld.build_timeline_thumbnail_sheet_command(
            FFMPEG,
            self.sample,
            str(output),
            start=0,
            end=4,
            count=10,
            columns=5,
            width=80,
        )
        subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.assertTrue(output.exists())
        self.assertGreater(output.stat().st_size, 100)


if __name__ == "__main__":
    unittest.main()
