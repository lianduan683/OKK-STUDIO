from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lossless_core import (  # noqa: E402
    Chapter,
    CutSegment,
    HistoryError,
    KeyframeIndex,
    PlaybackMode,
    SegmentHistory,
    SegmentModel,
    SnapDirection,
    bytes_per_second_from_source,
    chapters_to_segments,
    clone_segment,
    decide_playback,
    estimate_size,
    estimate_size_from_source,
    find_gaps,
    find_overlaps,
    gap_segments,
    has_overlaps,
    invert_segments,
    merge_overlapping_segments,
    normalize_keyframes,
    segments_to_chapters,
    snap_segment_to_keyframes,
    snap_time_to_keyframe,
    sort_segments,
    split_range_by_count,
    split_range_by_duration,
    split_range_by_size,
    timeline_span,
    total_clip_duration,
    validate_segment,
    validate_segments,
    validate_time,
)


def seg(
    segment_id: str,
    start: float,
    end: float | None,
    *,
    name: str = "",
    tags: dict[str, str] | None = None,
    selected: bool = False,
) -> CutSegment:
    return CutSegment(
        id=segment_id,
        start=start,
        end=end,
        name=name,
        tags=dict(tags or {}),
        selected=selected,
    )


class TimeValidationTests(unittest.TestCase):
    def test_valid_times_and_numeric_strings_are_normalised(self) -> None:
        self.assertEqual(validate_time(0), 0.0)
        self.assertEqual(validate_time("12.5"), 12.5)
        self.assertEqual(validate_time(None, allow_none=True), None)
        self.assertIsInstance(validate_time(3), float)

    def test_negative_nan_infinite_bool_and_missing_times_are_rejected(self) -> None:
        invalid = (-0.1, -1, float("nan"), float("inf"), float("-inf"), True, None)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_time(value)
        with self.assertRaises(ValueError):
            validate_time("abc")
        with self.assertRaises(ValueError):
            validate_time(10**1000)

    def test_marker_needs_explicit_none_and_range_must_be_positive(self) -> None:
        marker = validate_segment(seg("m", 10.0, None))
        self.assertTrue(marker.is_marker)
        self.assertEqual(marker.duration, 0.0)

        with self.assertRaises(ValueError):
            validate_segment(seg("a", 10.0, 10.0))
        with self.assertRaises(ValueError):
            validate_segment(seg("a", 10.0, 5.0))

    def test_media_duration_bounds_are_enforced_with_markers_allowed_at_the_end(self) -> None:
        self.assertEqual(validate_segment(seg("a", 50.0, 60.0), media_duration=60.0).end, 60.0)
        self.assertTrue(validate_segment(seg("m", 60.0, None), media_duration=60.0).is_marker)
        for candidate in (seg("a", 0.0, 60.1), seg("m", 60.1, None)):
            with self.subTest(id=candidate.id):
                with self.assertRaises(ValueError):
                    validate_segment(candidate, media_duration=60.0)

    def test_segment_metadata_is_strict(self) -> None:
        with self.assertRaises(ValueError):
            validate_segment(seg("", 0.0, 1.0))
        with self.assertRaises(ValueError):
            validate_segment(CutSegment(id="a", start=0.0, end=1.0, name=1))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            validate_segment(
                CutSegment(id="a", start=0.0, end=1.0, tags={"key": 1})  # type: ignore[dict-item]
            )
        with self.assertRaises(ValueError):
            validate_segment(
                CutSegment(id="a", start=0.0, end=1.0, selected=1)  # type: ignore[arg-type]
            )

    def test_validation_copies_tags_and_duplicate_ids_are_rejected(self) -> None:
        original = seg("a", 0.0, 10.0, tags={"camera": "A"})
        checked = validate_segment(original)
        checked.tags["camera"] = "B"
        self.assertEqual(original.tags, {"camera": "A"})

        with self.assertRaises(ValueError):
            validate_segments([seg("same", 0.0, 1.0), seg("same", 2.0, 3.0)])

    def test_validation_rejects_non_segment_values(self) -> None:
        with self.assertRaises(TypeError):
            validate_segment("not a segment")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            validate_segments("abc")


class SortingAndOverlapTests(unittest.TestCase):
    def test_sorting_places_markers_before_clips_at_the_same_time(self) -> None:
        segments = [
            seg("late", 20.0, 30.0),
            seg("marker", 10.0, None),
            seg("long", 10.0, 20.0),
            seg("short", 10.0, 15.0),
            seg("early", 0.0, 5.0),
        ]
        ordered = [item.id for item in sort_segments(segments)]
        self.assertEqual(ordered, ["early", "marker", "short", "long", "late"])

    def test_clip_duration_and_timeline_span_ignore_markers(self) -> None:
        segments = [seg("m", 100.0, None), seg("a", 10.0, 20.0), seg("b", 30.0, 45.0)]
        self.assertEqual(total_clip_duration(segments), 25.0)
        self.assertEqual(timeline_span(segments), (10.0, 45.0))
        self.assertIsNone(timeline_span([seg("m", 10.0, None)]))

    def test_touching_ranges_are_not_overlaps_but_nested_ranges_are(self) -> None:
        touching = [seg("a", 0.0, 10.0), seg("b", 10.0, 20.0)]
        self.assertEqual(find_overlaps(touching), ())
        self.assertFalse(has_overlaps(touching))

        overlapping = [
            seg("a", 0.0, 10.0),
            seg("b", 5.0, 15.0),
            seg("c", 8.0, 12.0),
            seg("m", 9.0, None),
        ]
        overlaps = find_overlaps(overlapping)
        self.assertEqual(len(overlaps), 3)
        self.assertEqual(
            [(item.first_id, item.second_id, item.start, item.end) for item in overlaps],
            [
                ("a", "b", 5.0, 10.0),
                ("a", "c", 8.0, 10.0),
                ("b", "c", 8.0, 12.0),
            ],
        )
        self.assertEqual(overlaps[0].duration, 5.0)

    def test_merge_overlapping_is_transitive_and_preserves_metadata(self) -> None:
        segments = [
            seg("a", 0.0, 10.0, name="first", tags={"one": "1"}),
            seg("b", 5.0, 15.0, name="second", tags={"two": "2"}),
            seg("c", 20.0, 30.0, selected=True),
            seg("m", 25.0, None),
        ]
        merged = merge_overlapping_segments(segments)
        self.assertEqual([(item.id, item.start, item.end) for item in merged], [
            ("a", 0.0, 15.0),
            ("c", 20.0, 30.0),
            ("m", 25.0, None),
        ])
        first = merged[0]
        self.assertEqual(first.name, "first")
        self.assertEqual(first.tags, {"one": "1", "two": "2"})
        self.assertTrue(merged[1].selected)

    def test_merge_overlapping_can_leave_touching_ranges_separate(self) -> None:
        segments = [seg("a", 0.0, 10.0), seg("b", 10.0, 20.0)]
        merged = merge_overlapping_segments(segments, join_adjacent=False)
        self.assertEqual([(item.start, item.end) for item in merged], [(0.0, 10.0), (10.0, 20.0)])

    def test_find_gaps_handles_full_coverage_start_and_overlaps(self) -> None:
        self.assertEqual(
            find_gaps([seg("a", 0.0, 30.0), seg("b", 30.0, 60.0)], 60.0),
            (),
        )
        gaps = find_gaps([seg("a", 10.0, 20.0), seg("b", 30.0, 40.0)], 60.0)
        self.assertEqual([(gap.start, gap.end) for gap in gaps], [(0.0, 10.0), (20.0, 30.0), (40.0, 60.0)])

        overlapping = find_gaps([seg("a", 0.0, 15.0), seg("b", 5.0, 20.0)], 30.0)
        self.assertEqual([(gap.start, gap.end) for gap in overlapping], [(20.0, 30.0)])

        uncovered = find_gaps([], 30.0)
        self.assertEqual([(gap.start, gap.end) for gap in uncovered], [(0.0, 30.0)])

    def test_invert_segments_returns_complement_and_drops_markers_by_default(self) -> None:
        ads = [seg("ad1", 10.0, 20.0), seg("ad2", 30.0, 40.0), seg("m", 15.0, None)]
        kept = invert_segments(ads, 60.0)
        self.assertEqual(
            [(item.start, item.end) for item in kept],
            [(0.0, 10.0), (20.0, 30.0), (40.0, 60.0)],
        )
        self.assertTrue(all(not item.is_marker for item in kept))
        self.assertEqual(len({item.id for item in kept}), 3)

        with_markers = invert_segments(ads, 60.0, keep_markers=True)
        self.assertTrue(any(item.is_marker for item in with_markers))

    def test_gap_segments_create_editable_segments(self) -> None:
        gaps = gap_segments([seg("a", 10.0, 20.0)], 30.0, name="gap")
        self.assertEqual([(item.start, item.end, item.name) for item in gaps], [
            (0.0, 10.0, "gap"),
            (20.0, 30.0, "gap"),
        ])
        self.assertTrue(all(item.id for item in gaps))


class SplitFunctionTests(unittest.TestCase):
    def test_split_by_duration_keeps_the_remainder_short(self) -> None:
        pieces = split_range_by_duration(0.0, 10.0, 3.0)
        self.assertEqual([(item.start, item.end) for item in pieces], [
            (0.0, 3.0),
            (3.0, 6.0),
            (6.0, 9.0),
            (9.0, 10.0),
        ])
        self.assertEqual(pieces[-1].duration, 1.0)

    def test_split_by_count_covers_whole_range_exactly(self) -> None:
        pieces = split_range_by_count(100.0, 107.0, 4)
        self.assertEqual(len(pieces), 4)
        self.assertEqual(pieces[0].start, 100.0)
        self.assertEqual(pieces[-1].end, 107.0)
        self.assertEqual([item.duration for item in pieces], [1.75, 1.75, 1.75, 1.75])
        for left, right in zip(pieces, pieces[1:]):
            self.assertEqual(left.end, right.start)

    def test_split_by_size_uses_the_estimated_bitrate(self) -> None:
        pieces = split_range_by_size(0.0, 10.0, max_bytes=50, bytes_per_second=25)
        self.assertEqual(len(pieces), 5)
        self.assertTrue(all(item.duration <= 2.0 + 1e-9 for item in pieces))
        self.assertAlmostEqual(sum(item.duration for item in pieces), 10.0)

    def test_estimated_size_helpers(self) -> None:
        segments = [seg("a", 0.0, 10.0), seg("b", 20.0, 25.0), seg("m", 30.0, None)]
        self.assertEqual(estimate_size(segments, 1000), 15000)
        self.assertAlmostEqual(bytes_per_second_from_source(30_000, 15.0), 2000.0)
        self.assertEqual(estimate_size_from_source(segments, 30_000, 15.0), 30000)

    def test_split_arguments_are_strict(self) -> None:
        for args in ((0.0, 10.0, 0.0), (0.0, 10.0, -1.0), (10.0, 10.0, 1.0)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    split_range_by_duration(*args)
        for count in (0, -1, 2.5, True, float("inf")):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    split_range_by_count(0.0, 10.0, count)
        with self.assertRaises(ValueError):
            split_range_by_size(0.0, 10.0, 0, 1)
        with self.assertRaises(ValueError):
            split_range_by_size(0.0, 10.0, 10, 0)


class SegmentModelEditingTests(unittest.TestCase):
    def test_constructor_validates_sorts_and_copies_state(self) -> None:
        first = seg("b", 20.0, 30.0, tags={"k": "v"})
        model = SegmentModel([first, seg("a", 0.0, 10.0)], media_duration=60.0)
        self.assertEqual([item.id for item in model.segments], ["a", "b"])
        self.assertEqual(len(model), 2)
        self.assertEqual(model.total_duration, 20.0)
        self.assertEqual(model.media_duration, 60.0)

        returned = model.segments
        self.assertIsNot(returned[1], first)
        returned[1].tags["k"] = "changed"
        returned[1].start = 99.0
        again = model.get("b")
        self.assertEqual(again.tags, {"k": "v"})
        self.assertEqual(again.start, 20.0)

    def test_add_cut_and_marker_with_metadata(self) -> None:
        model = SegmentModel(media_duration=60.0)
        added = model.add(10.0, 20.0, name="intro", tags={"take": "1"}, selected=True)
        marker = model.add(30.0, name="cue")
        self.assertEqual(added.start, 10.0)
        self.assertTrue(added.selected)
        self.assertTrue(marker.is_marker)
        self.assertEqual(len(model), 2)
        self.assertEqual(model.selected_clips, (added,))

        with self.assertRaises(ValueError):
            model.add(20.0, 40.0, segment_id=added.id)
        with self.assertRaises(ValueError):
            model.add(-1.0, 5.0)
        with self.assertRaises(ValueError):
            model.add(55.0, 61.0)

        end_marker = model.add(60.0)
        self.assertTrue(end_marker.is_marker)

    def test_remove_and_clear(self) -> None:
        model = SegmentModel(
            [seg("a", 0.0, 10.0), seg("b", 20.0, 30.0), seg("m", 40.0, None)],
            media_duration=60.0,
        )
        removed = model.remove("b")
        self.assertEqual(removed.id, "b")
        self.assertEqual([item.id for item in model.segments], ["a", "m"])

        with self.assertRaises(KeyError):
            model.remove("missing")
        self.assertEqual(model.remove_many(["missing"], missing_ok=True), ())

        model.select("m")
        self.assertEqual([item.id for item in model.remove_selected()], ["m"])

        model.add(5.0, 6.0, segment_id="c")
        model.add(50.0, segment_id="keepme")
        self.assertEqual(model.clear(keep_markers=True), 2)
        self.assertEqual([item.id for item in model.segments], ["keepme"])

        self.assertEqual(model.clear(), 1)
        self.assertEqual(model.segments, ())

    def test_split_preserves_metadata_and_creates_a_new_right_id(self) -> None:
        model = SegmentModel(
            [seg("a", 0.0, 10.0, name="take", tags={"camera": "A"}, selected=True)],
            media_duration=60.0,
        )
        left, right = model.split("a", 4.0)
        self.assertEqual((left.start, left.end), (0.0, 4.0))
        self.assertEqual((right.start, right.end), (4.0, 10.0))
        self.assertEqual(left.id, "a")
        self.assertNotEqual(right.id, "a")
        self.assertEqual((left.name, right.name), ("take", "take"))
        self.assertEqual((left.tags, right.tags), ({"camera": "A"}, {"camera": "A"}))
        self.assertTrue(left.selected and right.selected)
        self.assertEqual(len(model), 2)

        right.tags["camera"] = "B"
        self.assertEqual(model.get("a").tags, {"camera": "A"})

        for at in (0.0, 10.0, -1.0):
            with self.subTest(at=at):
                with self.assertRaises(ValueError):
                    model.split("a", at)
        marker = model.add(20.0)
        with self.assertRaises(ValueError):
            model.split(marker.id, 20.0)

    def test_duplicate_copies_tags_and_supports_offsets_and_clamping(self) -> None:
        model = SegmentModel(
            [seg("a", 5.0, 10.0, tags={"camera": "A"})],
            media_duration=20.0,
        )
        copy = model.duplicate("a", offset=3.0)
        self.assertNotEqual(copy.id, "a")
        self.assertEqual((copy.start, copy.end), (8.0, 13.0))
        copy.tags["camera"] = "B"
        self.assertEqual(model.get("a").tags, {"camera": "A"})

        with self.assertRaises(ValueError):
            model.duplicate("a", offset=-10.0)
        clamped = model.duplicate("a", offset=-10.0, clamp=True)
        self.assertEqual((clamped.start, clamped.end), (0.0, 5.0))

        model.add(0.0, 5.0, segment_id="wide")
        near_end = model.duplicate("wide", offset=18.0, clamp=True)
        self.assertEqual((near_end.start, near_end.end), (15.0, 20.0))

    def test_merge_selected_spans_gaps_and_keeps_first_identity(self) -> None:
        model = SegmentModel(
            [
                seg("a", 0.0, 10.0, name="A", tags={"one": "1"}, selected=True),
                seg("b", 20.0, 30.0, name="B", tags={"two": "2"}, selected=True),
                seg("c", 40.0, 50.0),
            ],
            media_duration=60.0,
        )
        merged = model.merge_selected()
        self.assertEqual((merged.id, merged.start, merged.end), ("a", 0.0, 30.0))
        self.assertEqual(merged.name, "A")
        self.assertEqual(merged.tags, {"one": "1", "two": "2"})
        self.assertTrue(merged.selected)
        self.assertEqual([item.id for item in model.segments], ["a", "c"])

        model.deselect(["a", "c"])
        with self.assertRaises(ValueError):
            model.merge_selected()

        model.select("c")
        model.add(55.0, segment_id="marker")
        model.select("marker")
        model.deselect("c")
        with self.assertRaises(ValueError):
            model.merge_selected()

    def test_merge_overlapping_method(self) -> None:
        model = SegmentModel(
            [seg("a", 0.0, 10.0), seg("b", 5.0, 15.0), seg("m", 12.0, None)],
            media_duration=60.0,
        )
        merged = model.merge_overlapping()
        self.assertEqual([(item.start, item.end) for item in merged], [(0.0, 15.0), (12.0, None)])
        self.assertEqual(model.overlaps(), ())

    def test_invert_and_selected_only(self) -> None:
        model = SegmentModel(
            [seg("ad1", 10.0, 20.0), seg("ad2", 30.0, 40.0)],
            media_duration=60.0,
        )
        kept = model.invert()
        self.assertEqual(
            [(item.start, item.end) for item in kept],
            [(0.0, 10.0), (20.0, 30.0), (40.0, 60.0)],
        )
        self.assertEqual(model.total_duration, 40.0)

        selected_model = SegmentModel(
            [
                seg("ad", 10.0, 20.0, selected=True),
                seg("not-ad", 25.0, 30.0),
                seg("m", 15.0, None),
            ],
            media_duration=60.0,
        )
        kept = selected_model.invert(selected_only=True, keep_markers=True)
        self.assertEqual(
            [(item.start, item.end) for item in kept],
            [(0.0, 10.0), (15.0, None), (20.0, 60.0)],
        )

        no_selection = SegmentModel(media_duration=60.0)
        with self.assertRaises(ValueError):
            no_selection.invert(selected_only=True)

        complete = SegmentModel(media_duration=60.0).invert()
        self.assertEqual([(item.start, item.end) for item in complete], [(0.0, 60.0)])

    def test_fill_gaps_adds_segments_and_is_idempotent(self) -> None:
        model = SegmentModel([seg("a", 10.0, 20.0)], media_duration=60.0)
        added = model.fill_gaps(name="gap")
        self.assertEqual(
            [(item.start, item.end, item.name) for item in added],
            [(0.0, 10.0, "gap"), (20.0, 60.0, "gap")],
        )
        self.assertEqual(len(model), 3)
        history_size = len(model.history)
        self.assertEqual(model.fill_gaps(), ())
        self.assertEqual(len(model.history), history_size)
        self.assertEqual(model.gaps(), ())

    def test_move_and_move_by_clamp_inside_the_media(self) -> None:
        model = SegmentModel(
            [seg("a", 10.0, 20.0), seg("m", 5.0, None)],
            media_duration=60.0,
        )
        moved = model.move("a", 30.0)
        self.assertEqual((moved.start, moved.end), (30.0, 40.0))
        clamped = model.move("a", 100.0)
        self.assertEqual((clamped.start, clamped.end), (50.0, 60.0))
        with self.assertRaises(ValueError):
            model.move("a", 100.0, clamp=False)

        marker = model.move("m", 100.0)
        self.assertEqual(marker.start, 60.0)
        self.assertTrue(marker.is_marker)

        with self.assertRaises(ValueError):
            model.move_by("a", -100.0)
        moved_to_zero = model.move_by("a", -100.0, clamp=True)
        self.assertEqual((moved_to_zero.start, moved_to_zero.end), (0.0, 10.0))

    def test_reorder_reflows_clips_and_preserves_markers(self) -> None:
        model = SegmentModel(
            [
                seg("a", 0.0, 5.0, name="A"),
                seg("b", 10.0, 20.0, name="B"),
                seg("m", 30.0, None, name="marker"),
            ],
            media_duration=60.0,
        )
        reordered = model.reorder(["b", "a"])
        clips = [item for item in reordered if not item.is_marker]
        self.assertEqual([(item.id, item.start, item.end) for item in clips], [
            ("b", 0.0, 10.0),
            ("a", 10.0, 15.0),
        ])
        marker = [item for item in reordered if item.is_marker][0]
        self.assertEqual(marker.start, 30.0)

        for order in (["a"], ["a", "a"], ["a", "missing"]):
            with self.subTest(order=order):
                with self.assertRaises(ValueError):
                    model.reorder(order)

    def test_reorder_rejects_an_overflowing_sequence(self) -> None:
        model = SegmentModel(
            [seg("a", 0.0, 10.0), seg("b", 0.0, 10.0)],
            media_duration=15.0,
        )
        with self.assertRaises(ValueError):
            model.reorder(["a", "b"])

    def test_selection_helpers(self) -> None:
        model = SegmentModel(
            [seg("a", 0.0, 1.0), seg("b", 2.0, 3.0), seg("m", 4.0, None)],
            media_duration=10.0,
        )
        self.assertEqual([item.id for item in model.select("a")], ["a"])
        self.assertEqual([item.id for item in model.select(["b", "m"])], ["a", "b", "m"])
        self.assertEqual([item.id for item in model.toggle_selected(["a", "b"])], ["m"])
        self.assertEqual([item.id for item in model.select_only("b")], ["b"])
        model.select_all()
        self.assertEqual(len(model.selected_segments), 3)
        model.clear_selection()
        self.assertEqual(model.selected_segments, ())
        with self.assertRaises(KeyError):
            model.select("missing")

    def test_set_media_duration_and_required_duration_errors(self) -> None:
        model = SegmentModel([seg("a", 0.0, 10.0)])
        with self.assertRaises(ValueError):
            model.fill_gaps()
        self.assertEqual(model.set_media_duration(20.0), 20.0)
        with self.assertRaises(ValueError):
            model.set_media_duration(5.0)

    def test_snap_replaces_a_segment_and_records_history(self) -> None:
        model = SegmentModel([seg("a", 0.0, 10.0)], media_duration=20.0)
        snapped = model.snap("a", [2.0, 8.0])
        self.assertEqual((snapped.start, snapped.end), (2.0, 8.0))
        self.assertEqual(snapped.id, "a")
        self.assertTrue(model.can_undo)
        self.assertEqual((model.get("a").start, model.get("a").end), (2.0, 8.0))


class HistoryTests(unittest.TestCase):
    def test_initial_state_cannot_undo_then_undo_and_redo_work(self) -> None:
        history = SegmentHistory([seg("a", 0.0, 1.0)])
        self.assertFalse(history.can_undo)
        self.assertFalse(history.can_redo)
        with self.assertRaises(HistoryError):
            history.undo()

        self.assertTrue(history.record([seg("a", 0.0, 2.0)]))
        self.assertTrue(history.can_undo)
        self.assertEqual(history.undo()[0].end, 1.0)
        self.assertTrue(history.can_redo)
        self.assertEqual(history.redo()[0].end, 2.0)

    def test_no_op_record_is_ignored_and_new_edit_clears_redo(self) -> None:
        history = SegmentHistory([seg("a", 0.0, 1.0)])
        self.assertFalse(history.record([seg("a", 0.0, 1.0)]))
        history.record([seg("a", 0.0, 2.0)])
        history.undo()
        self.assertTrue(history.can_redo)
        history.record([seg("a", 0.0, 3.0)])
        self.assertFalse(history.can_redo)
        self.assertEqual(history.current[0].end, 3.0)

    def test_history_limit_drops_old_states(self) -> None:
        history = SegmentHistory(limit=3)
        history.record([seg("a", 0.0, 1.0)])
        history.record([seg("a", 0.0, 2.0)])
        history.record([seg("a", 0.0, 3.0)])
        self.assertEqual(len(history), 3)
        history.record([seg("a", 0.0, 4.0)])
        self.assertEqual(len(history), 3)
        history.undo()
        history.undo()
        self.assertEqual(history.current[0].end, 2.0)
        self.assertFalse(history.can_undo)
        with self.assertRaises(HistoryError):
            history.undo()

    def test_snapshots_are_isolated_from_callers(self) -> None:
        source = seg("a", 0.0, 1.0, tags={"k": "v"})
        history = SegmentHistory([source])
        source.tags["k"] = "changed"
        source.start = 50.0
        self.assertEqual(history.current[0].tags, {"k": "v"})
        self.assertEqual(history.current[0].start, 0.0)

        returned = history.current
        returned[0].tags["k"] = "also changed"
        returned[0].start = 99.0
        self.assertEqual(history.current[0].tags, {"k": "v"})
        self.assertEqual(history.current[0].start, 0.0)

    def test_reset_discards_old_history(self) -> None:
        history = SegmentHistory([seg("a", 0.0, 1.0)])
        history.record([seg("a", 0.0, 2.0)])
        history.reset([seg("b", 10.0, 20.0)])
        self.assertFalse(history.can_undo)
        self.assertFalse(history.can_redo)
        self.assertEqual(history.current[0].id, "b")

    def test_invalid_limit_is_rejected(self) -> None:
        for limit in (0, -1, 1.5, True):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    SegmentHistory(limit=limit)

    def test_history_rejects_invalid_snapshots(self) -> None:
        with self.assertRaises(ValueError):
            SegmentHistory([seg("a", -1.0, 1.0)])
        history = SegmentHistory()
        with self.assertRaises(ValueError):
            history.record([seg("a", 10.0, 10.0)])
        with self.assertRaises(ValueError):
            history.reset([seg("a", 1.0, 2.0), seg("a", 3.0, 4.0)])

    def test_model_undo_redo_round_trip(self) -> None:
        model = SegmentModel(media_duration=60.0)
        model.add(0.0, 10.0, segment_id="a")
        model.add(20.0, 30.0, segment_id="b")
        self.assertEqual([item.id for item in model.undo()], ["a"])
        self.assertEqual([item.id for item in model.undo()], [])
        self.assertFalse(model.can_undo)
        self.assertEqual([item.id for item in model.redo()], ["a"])
        self.assertEqual([item.id for item in model.redo()], ["a", "b"])


class KeyframeTests(unittest.TestCase):
    def test_normalize_keyframes_sorts_deduplicates_and_validates(self) -> None:
        self.assertEqual(normalize_keyframes([10, 0, 10, 5]), (0.0, 5.0, 10.0))
        self.assertEqual(normalize_keyframes(None), ())
        for keyframes in ([-1], [float("nan")], [float("inf")]):
            with self.subTest(keyframes=keyframes):
                with self.assertRaises(ValueError):
                    normalize_keyframes(keyframes)
        with self.assertRaises(TypeError):
            normalize_keyframes("01020")  # type: ignore[arg-type]

    def test_previous_next_and_nearest_include_edges_and_ties(self) -> None:
        index = KeyframeIndex([0.0, 10.0, 20.0])
        self.assertEqual(index.previous(10.0), 10.0)
        self.assertEqual(index.next(10.0), 10.0)
        self.assertIsNone(index.previous(-1.0))
        self.assertIsNone(index.next(21.0))
        self.assertEqual(index.next(-1.0), 0.0)
        self.assertEqual(index.previous(21.0), 20.0)
        self.assertEqual(index.nearest(15.0), 10.0)
        self.assertEqual(index.nearest(16.0), 20.0)
        self.assertIsNone(KeyframeIndex([]).nearest(5.0))

    def test_snap_direction_and_tolerance(self) -> None:
        snap = snap_time_to_keyframe(7.0, [0.0, 10.0], direction="next")
        self.assertEqual(snap.snapped, 10.0)
        self.assertEqual(snap.direction, SnapDirection.NEXT)
        self.assertTrue(snap.moved)

        missed = snap_time_to_keyframe(6.0, [0.0, 10.0], tolerance=3.0)
        self.assertEqual(missed.snapped, 6.0)
        self.assertIsNone(missed.index)
        self.assertFalse(missed.moved)

        hit = snap_time_to_keyframe(6.0, [0.0, 10.0], tolerance=4.0)
        self.assertEqual(hit.snapped, 10.0)
        self.assertEqual(hit.offset, 4.0)

        with self.assertRaises(ValueError):
            snap_time_to_keyframe(6.0, [0.0, 10.0], tolerance=-1.0)
        with self.assertRaises(ValueError):
            snap_time_to_keyframe(6.0, [0.0, 10.0], direction="sideways")

    def test_snap_segment_snaps_both_boundaries_and_markers(self) -> None:
        snapped = snap_segment_to_keyframes(seg("a", 0.0, 10.0), [2.0, 4.0, 8.0])
        self.assertEqual((snapped.start, snapped.end), (2.0, 8.0))
        self.assertEqual(snapped.id, "a")

        directed = snap_segment_to_keyframes(
            seg("a", 0.0, 10.0),
            [2.0, 4.0, 8.0],
            start_direction="next",
            end_direction="previous",
        )
        self.assertEqual((directed.start, directed.end), (2.0, 8.0))

        marker = snap_segment_to_keyframes(seg("m", 11.0, None), [10.0, 12.0])
        self.assertEqual(marker.start, 10.0)
        self.assertTrue(marker.is_marker)

    def test_snapping_can_collapse_and_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            snap_segment_to_keyframes(
                seg("a", 3.0, 6.0),
                [4.0],
                start_direction="next",
                end_direction="previous",
            )


class ChapterTests(unittest.TestCase):
    def test_chapters_infer_ends_from_next_start_and_media_duration(self) -> None:
        chapters = [
            Chapter(0.0, title="intro"),
            Chapter(10.0, title="body"),
            Chapter(20.0, title="outro"),
        ]
        segments = chapters_to_segments(chapters, media_duration=30.0)
        self.assertEqual(
            [(item.start, item.end, item.name) for item in segments],
            [(0.0, 10.0, "intro"), (10.0, 20.0, "body"), (20.0, 30.0, "outro")],
        )

        without_duration = chapters_to_segments(chapters)
        self.assertTrue(without_duration[-1].is_marker)
        self.assertEqual(without_duration[-1].start, 20.0)

    def test_explicit_chapter_end_can_leave_a_gap(self) -> None:
        segments = chapters_to_segments(
            [Chapter(0.0, 5.0, "a"), Chapter(10.0, 20.0, "b")],
            media_duration=30.0,
        )
        self.assertEqual([(item.start, item.end) for item in segments], [(0.0, 5.0), (10.0, 20.0)])

    def test_overlapping_or_zero_length_chapters_are_rejected(self) -> None:
        invalid = (
            [Chapter(0.0, 15.0, "a"), Chapter(10.0, 20.0, "b")],
            [Chapter(0.0, None, "a"), Chapter(0.0, None, "b")],
            [Chapter(10.0, 10.0, "a")],
            [Chapter(10.0, 5.0, "a")],
            [Chapter(-1.0, 5.0, "a")],
        )
        for chapters in invalid:
            with self.subTest(chapters=chapters):
                with self.assertRaises(ValueError):
                    chapters_to_segments(chapters, media_duration=60.0)
        with self.assertRaises(ValueError):
            chapters_to_segments([Chapter(70.0, 80.0, "late")], media_duration=60.0)
        with self.assertRaises(ValueError):
            chapters_to_segments([Chapter(0.0, 70.0, "long")], media_duration=60.0)

    def test_segments_to_chapters_and_round_trip(self) -> None:
        segments = [
            seg("a", 10.0, 20.0, name="one"),
            seg("m", 15.0, None, name="cue"),
            seg("b", 20.0, 30.0, name="two"),
        ]
        chapters = segments_to_chapters(segments, include_markers=True)
        self.assertEqual(
            [(item.start, item.end, item.title) for item in chapters],
            [(10.0, 20.0, "one"), (15.0, None, "cue"), (20.0, 30.0, "two")],
        )
        self.assertEqual(len(segments_to_chapters(segments)), 2)

        model = SegmentModel(segments, media_duration=30.0)
        restored = SegmentModel.from_chapters(model.to_chapters(), media_duration=30.0)
        self.assertEqual(
            [(item.start, item.end, item.name) for item in restored.segments],
            [(10.0, 20.0, "one"), (20.0, 30.0, "two")],
        )
        self.assertTrue(all(not item.selected for item in restored.segments))


class PlaybackTests(unittest.TestCase):
    def test_empty_plan(self) -> None:
        plan = decide_playback([seg("m", 5.0, None)])
        self.assertEqual(plan.mode, PlaybackMode.EMPTY)
        self.assertTrue(plan.is_empty)
        self.assertEqual(plan.total_duration, 0.0)
        self.assertIsNone(plan.next_index(0))

    def test_single_segment_loop_and_once(self) -> None:
        loop = decide_playback([seg("a", 10.0, 20.0)])
        self.assertEqual(loop.mode, PlaybackMode.SINGLE_LOOP)
        self.assertTrue(loop.loop)
        self.assertEqual(loop.items[0].duration, 10.0)
        self.assertEqual(loop.next_index(0), 0)

        once = decide_playback([seg("a", 10.0, 20.0)], loop=False)
        self.assertEqual(once.mode, PlaybackMode.ONCE)
        self.assertFalse(once.loop)
        self.assertIsNone(once.next_index(0))

    def test_multi_segment_sequence_is_sorted_and_optionally_looped(self) -> None:
        plan = decide_playback(
            [seg("b", 30.0, 40.0), seg("a", 10.0, 20.0), seg("m", 15.0, None)],
            loop=True,
        )
        self.assertEqual(plan.mode, PlaybackMode.SEQUENCE)
        self.assertEqual([item.segment_id for item in plan.items], ["a", "b"])
        self.assertEqual(plan.total_duration, 20.0)
        self.assertEqual(plan.next_index(0), 1)
        self.assertEqual(plan.next_index(1), 0)

        once = decide_playback([seg("a", 0.0, 5.0), seg("b", 10.0, 15.0)], loop=False)
        self.assertIsNone(once.next_index(1))

    def test_selected_only_playback(self) -> None:
        segments = [seg("a", 0.0, 5.0), seg("b", 10.0, 20.0, selected=True)]
        plan = decide_playback(segments, selected_only=True)
        self.assertEqual([item.segment_id for item in plan.items], ["b"])
        self.assertEqual(plan.mode, PlaybackMode.SINGLE_LOOP)

        empty = decide_playback([seg("a", 0.0, 5.0)], selected_only=True)
        self.assertTrue(empty.is_empty)


class ThreeHourFilmTests(unittest.TestCase):
    DURATION = 3.0 * 60.0 * 60.0

    def test_three_hour_clip_splits_by_count_and_duration(self) -> None:
        model = SegmentModel(
            [seg("whole", 0.0, self.DURATION)],
            media_duration=self.DURATION,
        )
        pieces = model.split_all_by_count(6)
        self.assertEqual(len(pieces), 6)
        self.assertEqual(pieces[0].id, "whole")
        self.assertTrue(all(abs(item.duration - 1800.0) < 1e-9 for item in pieces))
        self.assertEqual(sum(item.duration for item in pieces), self.DURATION)

        self.assertTrue(model.can_undo)
        self.assertEqual(len(model.undo()), 1)
        self.assertEqual(len(model.redo()), 6)

        fresh = SegmentModel(
            [seg("whole", 0.0, self.DURATION)],
            media_duration=self.DURATION,
        )
        pieces = fresh.split_all_by_duration(600.0)
        self.assertEqual(len(pieces), 18)
        self.assertTrue(all(abs(item.duration - 600.0) < 1e-9 for item in pieces))
        self.assertEqual(sum(item.duration for item in pieces), self.DURATION)

    def test_three_hour_size_split_stays_under_the_limit(self) -> None:
        model = SegmentModel(
            [seg("whole", 0.0, self.DURATION)],
            media_duration=self.DURATION,
        )
        pieces = model.split_all_by_size(1_000_000_000, bytes_per_second=1_000_000)
        self.assertEqual(len(pieces), 11)
        self.assertTrue(all(item.duration <= 1000.0 + 1e-9 for item in pieces))
        self.assertEqual(sum(item.duration for item in pieces), self.DURATION)

        total_bytes = int(self.DURATION * 500_000)
        self.assertEqual(model.estimated_size_from_source(total_bytes), total_bytes)
        self.assertEqual(
            model.estimated_size_from_source(total_bytes),
            total_bytes,
        )

    def test_three_hour_keyframe_lookup_and_snapping(self) -> None:
        keyframes = list(range(0, int(self.DURATION) + 1, 2))
        index = KeyframeIndex(keyframes)
        self.assertEqual(index.previous(5431.0), 5430.0)
        self.assertEqual(index.next(5431.0), 5432.0)
        self.assertEqual(index.nearest(5431.0), 5430.0)
        self.assertEqual(index.nearest(self.DURATION + 10.0), self.DURATION)

        snapped = snap_segment_to_keyframes(
            seg("long", 5400.4, 8999.6),
            keyframes,
        )
        self.assertEqual((snapped.start, snapped.end), (5400.0, 9000.0))

    def test_three_hour_ad_removal(self) -> None:
        ads = [
            seg("ad1", 60.0, 120.0),
            seg("ad2", 3600.0, 3720.0),
            seg("ad3", 7200.0, 7260.0),
        ]
        model = SegmentModel(ads, media_duration=self.DURATION)
        kept = model.invert()
        self.assertEqual(len(kept), 4)
        self.assertEqual(kept[0].start, 0.0)
        self.assertEqual(kept[-1].end, self.DURATION)
        self.assertEqual(model.total_duration, self.DURATION - 240.0)


class PublicApiSmokeTests(unittest.TestCase):
    def test_clone_and_invert_helpers_are_independent(self) -> None:
        original = seg("a", 0.0, 10.0, tags={"k": "v"})
        copied = clone_segment(original, new_id=True, start=5.0)
        self.assertNotEqual(copied.id, original.id)
        self.assertEqual((copied.start, copied.end), (5.0, 10.0))
        copied.tags["k"] = "changed"
        self.assertEqual(original.tags, {"k": "v"})

    def test_estimate_size_ignores_markers_and_validates_rate(self) -> None:
        self.assertEqual(estimate_size([seg("m", 10.0, None)], 1000), 0)
        with self.assertRaises(ValueError):
            estimate_size([seg("a", 0.0, 1.0)], 0)


if __name__ == "__main__":
    unittest.main()
