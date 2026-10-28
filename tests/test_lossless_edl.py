from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lossless_core import Chapter, CutSegment  # noqa: E402
from lossless_edl import (  # noqa: E402
    LLC_VERSION,
    LosslessEdlError,
    LlcFormatError,
    LlcProject,
    SegmentValidationError,
    chapters_to_segments,
    detect_format,
    dump_chapters,
    dump_csv,
    dump_llc,
    dump_llc_project,
    dump_mplayer_edl,
    dump_srt,
    dump_tsv,
    dump_youtube_chapters,
    export_chapters,
    export_segments,
    format_from_path,
    format_seconds,
    format_srt_timecode,
    format_timecode,
    import_chapters,
    load_chapters,
    load_csv,
    load_document,
    load_llc,
    load_mplayer_edl,
    load_segments,
    load_srt,
    load_tsv,
    normalize_format,
    parse_chapters,
    parse_csv,
    parse_document,
    parse_llc,
    parse_mplayer_edl,
    parse_segments,
    parse_srt,
    parse_timecode,
    parse_tsv,
    parse_youtube_chapters,
    read_text_file,
    resolve_media_path,
    save_llc,
    save_llc_project,
    save_segments,
    save_chapters,
    save_csv,
    save_mplayer_edl,
    save_srt,
    save_tsv,
    segments_to_chapters,
    validate_media_file_name,
    validate_segments,
    write_text_file,
)


def segment(
    segment_id: str,
    start: float,
    end: float,
    *,
    name: str = "",
    tags: dict[str, str] | None = None,
    selected: bool = True,
) -> CutSegment:
    return CutSegment(
        id=segment_id,
        start=start,
        end=end,
        name=name,
        tags=dict(tags or {}),
        selected=selected,
    )


class TimecodeTests(unittest.TestCase):
    def test_full_timecode_has_millisecond_precision(self) -> None:
        self.assertEqual(format_timecode(3661.234), "01:01:01.234")
        self.assertEqual(format_timecode(0), "00:00:00.000")
        self.assertEqual(format_timecode(360000), "100:00:00.000")
        self.assertEqual(format_srt_timecode(1.005), "00:00:01,005")
        self.assertEqual(format_seconds(1.2345), "1.235")
        self.assertEqual(format_seconds(0.0004), "0.000")

    def test_parse_timecode_accepts_seconds_and_clock_forms(self) -> None:
        self.assertEqual(parse_timecode("12.345"), 12.345)
        self.assertEqual(parse_timecode("1:02.5"), 62.5)
        self.assertEqual(parse_timecode("01:02:03.456"), 3723.456)
        self.assertAlmostEqual(parse_timecode("00:00:01,005"), 1.005)
        self.assertEqual(parse_timecode("90:00"), 5400.0)
        self.assertEqual(parse_timecode(" 7 "), 7.0)

    def test_parse_timecode_rejects_damaged_values(self) -> None:
        invalid = ("", "abc", "-1", -1.0, "1:60", "00:00:60,000", "1.2.3", "1::2", True, "9" * 400)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(LosslessEdlError):
                    parse_timecode(value)

    def test_format_rejects_negative_and_invalid_separator(self) -> None:
        with self.assertRaises(LosslessEdlError):
            format_timecode(-0.001)
        with self.assertRaises(LosslessEdlError):
            format_timecode(1, separator=";")
        with self.assertRaises(LosslessEdlError):
            format_seconds(True)


class MediaPathTests(unittest.TestCase):
    def test_relative_media_name_is_accepted(self) -> None:
        self.assertEqual(
            validate_media_file_name("clips/video.mp4"),
            "clips/video.mp4",
        )
        self.assertEqual(validate_media_file_name("video.mp4"), "video.mp4")

    def test_unsafe_media_names_are_rejected(self) -> None:
        invalid = (
            "",
            "../video.mp4",
            "clips/../../video.mp4",
            "/absolute/video.mp4",
            "\\\\server\\share\\video.mp4",
            "C:\\video.mp4",
            "C:/video.mp4",
            "https://example.com/video.mp4",
            "file:video.mp4",
            "bad\x00name.mp4",
            "clips/",
            "clips\\",
            "foo:bar.mp4",
            "bad?.mp4",
            "CON.mp4",
            " padded.mp4",
            "video.mp4 ",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(LlcFormatError):
                    validate_media_file_name(value)
        with self.assertRaises(LlcFormatError):
            validate_media_file_name(123)

    def test_resolve_media_path_stays_below_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "clips").mkdir()
            target = base / "clips" / "video.mp4"
            target.write_bytes(b"x")
            self.assertEqual(
                resolve_media_path("clips/video.mp4", base, must_exist=True),
                target.resolve(),
            )
            with self.assertRaises(LlcFormatError):
                resolve_media_path("../video.mp4", base)
            with self.assertRaises(LlcFormatError):
                resolve_media_path("missing.mp4", base, must_exist=True)


class SegmentValidationTests(unittest.TestCase):
    def test_empty_and_adjacent_segments_are_valid(self) -> None:
        self.assertEqual(validate_segments([]), [])
        items = [segment("a", 0, 5), segment("b", 5, 9)]
        self.assertEqual(validate_segments(items), items)

    def test_overlap_is_rejected_even_when_input_is_unsorted(self) -> None:
        items = [segment("b", 4, 8), segment("a", 0, 5)]
        with self.assertRaisesRegex(SegmentValidationError, "重叠"):
            validate_segments(items)

    def test_negative_zero_length_and_out_of_bounds_are_rejected(self) -> None:
        cases = (
            [segment("a", -1, 5)],
            [segment("a", 4, 4)],
            [segment("a", 5, 4)],
        )
        for items in cases:
            with self.subTest(items=items):
                with self.assertRaises(SegmentValidationError):
                    validate_segments(items)
        with self.assertRaisesRegex(SegmentValidationError, "超出"):
            validate_segments([segment("a", 0, 10.001)], media_duration=10.0)

    def test_bool_nan_infinity_and_bad_fields_are_rejected(self) -> None:
        with self.assertRaises(SegmentValidationError):
            validate_segments([segment("a", True, 5)])
        with self.assertRaises(SegmentValidationError):
            validate_segments([segment("a", float("nan"), 5)])
        with self.assertRaises(SegmentValidationError):
            validate_segments([segment("a", 0, float("inf"))])
        with self.assertRaises(SegmentValidationError):
            validate_segments([CutSegment(id="a", start=0, end=1, tags=[])])
        with self.assertRaises(SegmentValidationError):
            validate_segments([CutSegment(id="a", start=0, end=1, selected="yes")])
        with self.assertRaises(SegmentValidationError):
            validate_segments([CutSegment(id="a", start=0, end=None)])
        with self.assertRaises(SegmentValidationError):
            validate_segments([segment("", 0, 1)])

    def test_duplicate_ids_and_sub_millisecond_ranges_are_rejected(self) -> None:
        with self.assertRaisesRegex(SegmentValidationError, "重复"):
            validate_segments([segment("same", 0, 1), segment("same", 2, 3)])
        with self.assertRaisesRegex(SegmentValidationError, "1 毫秒"):
            validate_segments([segment("a", 1.2346, 1.2354)])


class LlcTests(unittest.TestCase):
    def test_llc_roundtrip_preserves_name_tags_selected_and_id(self) -> None:
        items = [
            segment(
                "first",
                0,
                10.5,
                name="开场",
                tags={"intro": "", "mood": "calm"},
                selected=True,
            ),
            segment(
                "second",
                12,
                20,
                name="第二段",
                tags={"b-roll": ""},
                selected=False,
            ),
        ]
        text = dump_llc("clips/video.mp4", items, media_duration=30)
        data = json.loads(text)
        self.assertEqual(data["version"], LLC_VERSION)
        self.assertEqual(data["mediaFileName"], "clips/video.mp4")
        self.assertEqual(data["cutSegments"][0]["tags"], ["intro", "mood=calm"])
        self.assertFalse(data["cutSegments"][1]["selected"])

        project = parse_llc(text)
        self.assertEqual(project.media_file_name, "clips/video.mp4")
        self.assertEqual(project.media_duration, 30)
        self.assertEqual(len(project.segments), 2)
        self.assertEqual(project.segments[0].id, "first")
        self.assertEqual(project.segments[0].name, "开场")
        self.assertEqual(project.segments[0].tags, {"intro": "", "mood": "calm"})
        self.assertTrue(project.segments[0].selected)
        self.assertFalse(project.segments[1].selected)

    def test_llc_tag_values_with_equals_and_backslash_roundtrip(self) -> None:
        items = [
            segment(
                "a",
                0,
                1,
                tags={"k=1": "v=2\\x", "plain": ""},
            )
        ]
        text = dump_llc("v.mp4", items)
        loaded = parse_llc(text)
        self.assertEqual(loaded.segments[0].tags, {"k=1": "v=2\\x", "plain": ""})
        self.assertEqual(parse_llc(dump_llc_project(LlcProject("v.mp4", items))), parse_llc(text))

    def test_integral_float_version_two_is_accepted(self) -> None:
        project = parse_llc("{ version: 2.0, mediaFileName: 'v.mp4', cutSegments: [] }")
        self.assertEqual(project.segments, [])

    def test_json5_comments_unquoted_keys_single_quotes_and_trailing_commas(self) -> None:
        text = """
        // LosslessCut style JSON5
        {
          version: 2,
          mediaFileName: 'video.mp4',
          cutSegments: [
            {
              id: 'a',
              start: 0,
              end: 1.5,
              name: 'First',
              tags: ['intro', 'mood=calm'],
              selected: true,
            },
            {
              id: 'b',
              start: 2,
              end: 4,
              name: "Second",
              /* tags can be a mapping too */
              tags: { 'b-roll': '' },
            },
          ],
          mediaDuration: 10,
        }
        """
        project = parse_llc(text)
        self.assertEqual(project.segments[0].tags, {"intro": "", "mood": "calm"})
        self.assertTrue(project.segments[1].selected)
        self.assertEqual(project.segments[1].tags, {"b-roll": ""})
        self.assertEqual(project.media_duration, 10)

    def test_json5_string_escapes_and_line_continuation(self) -> None:
        text = (
            "{ version: 2, mediaFileName: 'v\\u002emp4', cutSegments: ["
            "{ id: 'a', start: 0, end: 1, name: 'A\\x42\\u4e2d' },"
            "{ id: 'b', start: 2, end: 3, name: 'line\\\ncontinued' } ] }"
        )
        project = parse_llc(text)
        self.assertEqual(project.media_file_name, "v.mp4")
        self.assertEqual(project.segments[0].name, "AB中")
        self.assertEqual(project.segments[1].name, "linecontinued")

    def test_missing_version_uses_version_two(self) -> None:
        project = parse_llc("{ mediaFileName: 'v.mp4', cutSegments: [] }")
        self.assertEqual(project.segments, [])
        self.assertIsNone(project.media_duration)

    def test_damaged_or_unsupported_llc_is_rejected(self) -> None:
        invalid = (
            "",
            "{",
            "[]",
            "{ mediaFileName: 'v.mp4' }",
            "{ version: 3, mediaFileName: 'v.mp4', cutSegments: [] }",
            "{ version: '2', mediaFileName: 'v.mp4', cutSegments: [] }",
            "{ version: 2, cutSegments: [] }",
            "{ version: 2, mediaFileName: '', cutSegments: [] }",
            "{ version: 2, mediaFileName: '../v.mp4', cutSegments: [] }",
            "{ version: 2, mediaFileName: 'v.mp4', cutSegments: {} }",
            "{ version: 2, mediaFileName: 'v.mp4', cutSegments: [1] }",
            "{ version: 2, mediaFileName: 'v.mp4', cutSegments: [{ start: 0 }] }",
            "{ version: 2, mediaFileName: 'v.mp4', cutSegments: [{ start: 0, end: null }] }",
            "{ version: 2, mediaFileName: 'v.mp4', cutSegments: [{ start: 2, end: 1 }] }",
            "{ version: 2, mediaFileName: 'v.mp4', mediaDuration: -1, cutSegments: [] }",
            "{ version: 2, mediaFileName: 'v.mp4', cutSegments: [ { id: 'a', start: 0, end: 1 }, { id: 'a', start: 2, end: 3 } ] }",
            "{ version: 2, mediaFileName: 'v.mp4', cutSegments: [ { id: 'a', start: 0, end: 11 } ], mediaDuration: 10 }",
            "{ version: 2, mediaFileName: 'v.mp4', cutSegments: [ { start: 0, end: 1, tags: 7 } ] }",
            "{ version: 2, mediaFileName: 'v.mp4', cutSegments: [ { start: 0, end: 1, tags: ['a', 2] } ] }",
            "{ version: 2, mediaFileName: 'v.mp4', cutSegments: [ { start: 0, end: 1, tags: ['a=x', 'a=y'] } ] }",
            "{ version: 2, mediaFileName: 'v.mp4', cutSegments: [] } trailing",
            "{ version: 2, version: 2, mediaFileName: 'v.mp4', cutSegments: [] }",
            "{ version: 2, mediaFileName: 'v.mp4', cutSegments: [] /* unclosed",
            "{ version: 2, mediaFileName: 'v.mp4', cutSegments: [ { start: 0, end: 1, name: 'x"
            + "\n"
            + "y' } ] }",
        )
        for text in invalid:
            with self.subTest(text=text):
                with self.assertRaises(LosslessEdlError):
                    parse_llc(text)

    def test_overlapping_segments_in_llc_are_rejected(self) -> None:
        text = (
            "{ version: 2, mediaFileName: 'v.mp4', cutSegments: ["
            "{ id: 'a', start: 0, end: 5 },"
            "{ id: 'b', start: 4, end: 8 } ] }"
        )
        with self.assertRaisesRegex(SegmentValidationError, "重叠"):
            parse_llc(text)

    def test_save_and_load_llc_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "project.llc"
            items = [segment("a", 0, 2.5, name="A", tags={"x": ""}, selected=False)]
            project = LlcProject("video.mp4", items, media_duration=12)
            self.assertEqual(save_llc_project(path, project), path)
            loaded = load_llc(path)
            self.assertEqual(loaded.media_file_name, "video.mp4")
            self.assertEqual(loaded.media_duration, 12)
            self.assertEqual(loaded.segments[0].tags, {"x": ""})
            self.assertFalse(loaded.segments[0].selected)

            save_llc(path, "other.mp4", items)
            self.assertEqual(load_llc(path).media_file_name, "other.mp4")
            save_llc(path, project, media_duration=20)
            self.assertEqual(load_llc(path).media_duration, 20)
            with self.assertRaises(LosslessEdlError):
                save_llc(path, project, items)

    def test_llc_file_encoding_compatibility(self) -> None:
        text = dump_llc("视频.mp4", [segment("a", 0, 1, name="中文")])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            utf16 = root / "utf16.llc"
            utf16.write_bytes(text.encode("utf-16"))
            self.assertEqual(load_llc(utf16).segments[0].name, "中文")

            gb = root / "gb.llc"
            gb.write_bytes(text.encode("gb18030"))
            self.assertEqual(load_llc(gb).segments[0].name, "中文")

    def test_atomic_writer_creates_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "out.csv"
            write_text_file(path, "x\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "x\n")


class CsvTsvTests(unittest.TestCase):
    def test_csv_roundtrip_with_quoted_name_and_escaped_tags(self) -> None:
        items = [
            segment(
                "a",
                0,
                10.5,
                name="第一行, 带逗号\n第二行",
                tags={"intro": "", "mood": "calm;x=y|z"},
                selected=False,
            )
        ]
        text = dump_csv(items)
        self.assertTrue(text.startswith("start,end,name,tags,selected\n"))
        loaded = parse_csv(text)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].start, 0)
        self.assertEqual(loaded[0].end, 10.5)
        self.assertEqual(loaded[0].name, "第一行, 带逗号\n第二行")
        self.assertEqual(loaded[0].tags, {"intro": "", "mood": "calm;x=y|z"})
        self.assertFalse(loaded[0].selected)

    def test_tsv_roundtrip(self) -> None:
        items = [segment("a", 0, 1, name="A\tB", tags={"k": "v"})]
        text = dump_tsv(items)
        self.assertIn("\t", text)
        loaded = parse_tsv(text)
        self.assertEqual(loaded[0].name, "A\tB")
        self.assertEqual(loaded[0].tags, {"k": "v"})

    def test_header_aliases_any_order_and_extra_columns(self) -> None:
        text = (
            "title,out,in,selected,tags,notes\n"
            "Clip,5,1,false,a|b,ignored\n"
        )
        items = parse_csv(text)
        self.assertEqual(items[0].start, 1)
        self.assertEqual(items[0].end, 5)
        self.assertEqual(items[0].name, "Clip")
        self.assertEqual(items[0].tags, {"a": "", "b": ""})
        self.assertFalse(items[0].selected)

    def test_headerless_csv_supports_timecodes_and_partial_columns(self) -> None:
        text = "00:00:01.000,00:00:02.500,Name,tag;key=value,是\n3,4\n"
        items = parse_csv(text)
        self.assertEqual(items[0].start, 1)
        self.assertEqual(items[0].end, 2.5)
        self.assertEqual(items[0].tags, {"tag": "", "key": "value"})
        self.assertTrue(items[0].selected)
        self.assertEqual(items[1].name, "")
        self.assertEqual(items[1].tags, {})

    def test_headerless_csv_can_use_header_words_as_names(self) -> None:
        items = parse_csv("0,1,start\n2,3,end\n")
        self.assertEqual([item.name for item in items], ["start", "end"])

    def test_csv_rejects_damage_overlap_bounds_and_bad_selected(self) -> None:
        with self.assertRaises(LosslessEdlError):
            parse_csv("start\n0\n")
        with self.assertRaises(LosslessEdlError):
            parse_csv("start,start,end\n0,1,2\n")
        with self.assertRaises(LosslessEdlError):
            parse_csv("0,1\n2,3,4,5,1,extra\n")
        with self.assertRaises(LosslessEdlError):
            parse_csv("0,1,x,,\n2,3,y,,maybe\n")
        with self.assertRaises(LosslessEdlError):
            parse_csv("0,5\n4,8\n")
        with self.assertRaises(LosslessEdlError):
            parse_csv("0,11\n", media_duration=10)
        with self.assertRaises(LosslessEdlError):
            parse_csv("0,1,x,a;a=b\n")
        with self.assertRaises(LosslessEdlError):
            parse_csv("0,1,x,=bad\n")
        with self.assertRaises(LosslessEdlError):
            parse_csv('start,end,name\n0,1,"unterminated\n')

    def test_empty_and_header_only_tables(self) -> None:
        self.assertEqual(parse_csv(""), [])
        self.assertEqual(parse_tsv("\n"), [])
        self.assertEqual(parse_csv("start,end,name,tags,selected\n"), [])

    def test_bom_and_selected_aliases(self) -> None:
        text = "\ufeffstart,end,selected\n0,1,no\n2,3,选中\n"
        items = parse_csv(text)
        self.assertFalse(items[0].selected)
        self.assertTrue(items[1].selected)


class MPlayerEdlTests(unittest.TestCase):
    def test_mplayer_roundtrip(self) -> None:
        items = [segment("a", 0, 10.5), segment("b", 12, 20)]
        text = dump_mplayer_edl(items)
        self.assertEqual(text, "0.000 10.500 0\n12.000 20.000 0\n")
        loaded = parse_mplayer_edl(text)
        self.assertEqual([(item.start, item.end) for item in loaded], [(0, 10.5), (12, 20)])
        self.assertTrue(all(item.selected for item in loaded))

    def test_mplayer_accepts_comments_optional_action_and_timecodes(self) -> None:
        text = (
            "# comment\n\n"
            "00:00:01,000 2.500\n"
            "00:00:03.000 00:00:04,000 0 # inline\n"
        )
        items = parse_mplayer_edl(text)
        self.assertEqual([(item.start, item.end) for item in items], [(1, 2.5), (3, 4)])

    def test_mplayer_accepts_zero_float_action(self) -> None:
        items = parse_mplayer_edl("0 1 0.0\n")
        self.assertEqual(len(items), 1)

    def test_mplayer_rejects_bad_lines_and_mute(self) -> None:
        invalid = (
            "0\n",
            "0 1 0 extra\n",
            "0 1 1\n",
            "0 1 mute\n",
            "a b 0\n",
            "0,5\n",
        )
        for text in invalid:
            with self.subTest(text=text):
                with self.assertRaises(LosslessEdlError):
                    parse_mplayer_edl(text)
        with self.assertRaises(LosslessEdlError):
            parse_mplayer_edl("0 5\n4 8\n")
        with self.assertRaises(LosslessEdlError):
            parse_mplayer_edl("0 11\n", media_duration=10)

    def test_empty_mplayer_edl(self) -> None:
        self.assertEqual(parse_mplayer_edl(""), [])
        self.assertEqual(dump_mplayer_edl([]), "")


class SrtTests(unittest.TestCase):
    def test_srt_export_uses_comma_milliseconds(self) -> None:
        items = [
            segment("a", 0, 1.234, name="开场"),
            segment("b", 2, 3.5, name="第二段"),
        ]
        text = dump_srt(items)
        self.assertIn("00:00:00,000 --> 00:00:01,234", text)
        self.assertIn("00:00:02,000 --> 00:00:03,500", text)
        self.assertNotIn("00:00:00.000", text)
        self.assertEqual(parse_srt(text)[0].name, "开场")

    def test_srt_accepts_crlf_bom_dot_and_short_fractions(self) -> None:
        text = (
            "\ufeff1\r\n"
            "00:00:00,5 --> 00:00:01,50\r\n"
            "A\r\n\r\n"
            "2\r\n"
            "00:00:02.000 --> 00:00:03,050\r\n"
            "B\r\n"
        )
        items = parse_srt(text)
        self.assertEqual(items[0].start, 0.5)
        self.assertEqual(items[0].end, 1.5)
        self.assertEqual(items[1].start, 2)
        self.assertEqual(items[1].end, 3.05)
        self.assertEqual(items[1].name, "B")

    def test_srt_without_indices_and_with_position_tail(self) -> None:
        text = (
            "00:00:00,000 --> 00:00:01,000 X1:1 X2:2 Y1:3 Y2:4\n"
            "Line one\n\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "Line two\n"
        )
        items = parse_srt(text)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].name, "Line one")
        self.assertEqual(items[1].name, "Line two")

    def test_srt_multiline_name_and_empty_name_fallback(self) -> None:
        items = [
            segment("a", 0, 1, name="第一行\n第二行"),
            segment("b", 1, 2, name=""),
        ]
        loaded = parse_srt(dump_srt(items))
        self.assertEqual(loaded[0].name, "第一行\n第二行")
        self.assertEqual(loaded[1].name, "片段 2")

    def test_srt_rejects_damaged_blocks(self) -> None:
        invalid = (
            "1\n00:00:00,000 -> 00:00:01,000\nA\n",
            "1\n00:00:60,000 --> 00:00:61,000\nA\n",
            "1\n00:00:00,000 --> 00:00:01,000 X1:1 junk\nA\n",
            "0\n00:00:00,000 --> 00:00:01,000\nA\n",
            "1\n00:00:00,000 --> 00:00:01,000\nA\n\n1\n00:00:02,000 --> 00:00:03,000\nB\n",
            "00:00:00,000 --> 00:00:01,000\nA\n\n2\n00:00:02,000 --> 00:00:03,000\nB\n",
        )
        for text in invalid:
            with self.subTest(text=text):
                with self.assertRaises(LosslessEdlError):
                    parse_srt(text)

    def test_srt_rejects_overlap_and_bounds(self) -> None:
        with self.assertRaises(LosslessEdlError):
            parse_srt(
                "1\n00:00:00,000 --> 00:00:02,000\nA\n\n"
                "2\n00:00:01,000 --> 00:00:03,000\nB\n"
            )
        with self.assertRaises(LosslessEdlError):
            parse_srt(
                "1\n00:00:00,000 --> 00:00:11,000\nA\n",
                media_duration=10,
            )

    def test_empty_srt(self) -> None:
        self.assertEqual(parse_srt(""), [])
        self.assertEqual(dump_srt([]), "")


class ChapterTests(unittest.TestCase):
    def test_parse_chapters_returns_core_chapter_objects(self) -> None:
        text = (
            "# YouTube chapters\n"
            "0:00 Intro\n"
            "1:02.500 Part 2\n"
            "1:02:03,250 Part 3\n"
        )
        chapters = parse_chapters(text)
        self.assertTrue(all(isinstance(item, Chapter) for item in chapters))
        self.assertEqual([item.start for item in chapters], [0, 62.5, 3723.25])
        self.assertEqual([item.title for item in chapters], ["Intro", "Part 2", "Part 3"])
        self.assertTrue(all(item.end is None for item in chapters))
        self.assertEqual(parse_youtube_chapters("0:00 A\n0:10 B"), parse_chapters("0:00 A\n0:10 B"))

    def test_chapters_to_segments_needs_duration_for_last_chapter(self) -> None:
        chapters = parse_chapters("0:00 A\n0:10 B\n0:20 C")
        with self.assertRaisesRegex(LosslessEdlError, "总时长"):
            chapters_to_segments(chapters)
        items = chapters_to_segments(chapters, 30)
        self.assertEqual([(item.start, item.end) for item in items], [(0, 10), (10, 20), (20, 30)])
        self.assertTrue(all(item.selected for item in items))

    def test_explicit_chapter_ends_do_not_need_duration(self) -> None:
        chapters = [
            Chapter(start=0, end=5, title="A"),
            Chapter(start=5, end=10, title="B"),
        ]
        items = chapters_to_segments(chapters)
        self.assertEqual([(item.start, item.end) for item in items], [(0, 5), (5, 10)])

    def test_chapter_conversion_rejects_overlap_and_out_of_bounds(self) -> None:
        overlapping = [
            Chapter(start=0, end=6, title="A"),
            Chapter(start=5, end=10, title="B"),
        ]
        with self.assertRaises(LosslessEdlError):
            chapters_to_segments(overlapping)
        with self.assertRaises(LosslessEdlError):
            chapters_to_segments([Chapter(start=0, end=None, title="A")], 0)
        with self.assertRaises(LosslessEdlError):
            chapters_to_segments(parse_chapters("0:00 A"), -1)

    def test_dump_chapters_formats_youtube_timestamps(self) -> None:
        chapters = [
            Chapter(start=0, title="Intro"),
            Chapter(start=62.5, title="Part 2"),
            Chapter(start=3723.25, title="Part 3"),
            Chapter(start=4000, title=""),
        ]
        self.assertEqual(
            dump_chapters(chapters),
            "0:00 Intro\n1:02.500 Part 2\n1:02:03.250 Part 3\n1:06:40 片段 4\n",
        )

    def test_segments_to_chapters_sorts_and_keeps_end_and_title(self) -> None:
        items = [segment("b", 10, 20, name="B"), segment("a", 0, 5, name="A")]
        chapters = segments_to_chapters(items)
        self.assertEqual(
            [(item.start, item.end, item.title) for item in chapters],
            [(0, 5, "A"), (10, 20, "B")],
        )
        self.assertEqual(
            dump_youtube_chapters(items),
            "0:00 A\n0:10 B\n",
        )

    def test_chapter_parse_and_dump_reject_damage(self) -> None:
        invalid = (
            "junk\n",
            "0:60 A\n",
            "0:10 A\n0:10 B\n",
            "0:20 A\n0:10 B\n",
            "1:60:00 A\n",
        )
        for text in invalid:
            with self.subTest(text=text):
                with self.assertRaises(LosslessEdlError):
                    parse_chapters(text)
        with self.assertRaises(LosslessEdlError):
            dump_chapters([Chapter(start=10, title="A"), Chapter(start=10, title="B")])
        with self.assertRaises(LosslessEdlError):
            dump_chapters([Chapter(start=0, title=123)])  # type: ignore[arg-type]

    def test_import_export_chapter_aliases(self) -> None:
        text = "0:00 A\n0:10 B\n"
        items = import_chapters(text, 20)
        self.assertEqual(export_chapters(items), text)
        self.assertEqual(parse_chapters(text), parse_youtube_chapters(text))


class GenericFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.items = [
            segment("a", 0, 2.5, name="A", tags={"x": "1"}, selected=True),
            segment("b", 3, 5, name="B", tags={"y": ""}, selected=False),
        ]

    def test_detect_format(self) -> None:
        self.assertEqual(detect_format(dump_llc("v.mp4", self.items)), "llc")
        self.assertEqual(
            detect_format("// project comment\n/* another */\n" + dump_llc("v.mp4", self.items)),
            "llc",
        )
        self.assertEqual(detect_format(dump_csv(self.items)), "csv")
        self.assertEqual(detect_format(dump_tsv(self.items)), "tsv")
        self.assertEqual(detect_format(dump_srt(self.items)), "srt")
        self.assertEqual(detect_format(dump_youtube_chapters(self.items)), "chapters")
        self.assertEqual(detect_format(dump_mplayer_edl(self.items)), "mplayer")
        self.assertEqual(detect_format("0 1 0 # inline\n"), "mplayer")
        self.assertEqual(detect_format("0:00 0:10 0\n"), "mplayer")
        self.assertEqual(detect_format("0:00, 0:10\n"), "csv")
        self.assertEqual(detect_format("0:00 Intro\n0:10 Part 2\n"), "chapters")
        with self.assertRaises(LosslessEdlError):
            detect_format("   \n")

    def test_normalize_format_and_extensions(self) -> None:
        self.assertEqual(normalize_format("losslesscut"), "llc")
        self.assertEqual(normalize_format("youtube-chapters"), "chapters")
        self.assertEqual(normalize_format(".srt"), "srt")
        self.assertEqual(format_from_path("a.LLC"), "llc")
        self.assertIsNone(format_from_path("a.txt"))
        with self.assertRaises(LosslessEdlError):
            normalize_format("docx")

    def test_export_and_parse_roundtrip_for_every_text_format(self) -> None:
        for fmt in ("csv", "tsv", "mplayer", "srt"):
            with self.subTest(fmt=fmt):
                text = export_segments(self.items, fmt)
                loaded = parse_segments(text, fmt)
                self.assertEqual(
                    [(item.start, item.end) for item in loaded],
                    [(item.start, item.end) for item in self.items],
                )
                if fmt != "mplayer":
                    self.assertEqual(
                        [item.name for item in loaded],
                        [item.name for item in self.items],
                    )
                else:
                    self.assertEqual([item.name for item in loaded], ["", ""])
        text = export_segments(self.items, "llc", media_file_name="v.mp4")
        self.assertEqual(parse_segments(text, "llc")[0].tags, {"x": "1"})

    def test_chapter_generic_export_loses_gaps_and_requires_duration(self) -> None:
        text = export_segments(self.items, "chapters")
        with self.assertRaises(LosslessEdlError):
            parse_segments(text, "chapters")
        with self.assertRaises(LosslessEdlError):
            export_segments(self.items, "chapters", media_duration=4)
        loaded = parse_segments(text, "chapters", media_duration=5)
        self.assertEqual([item.start for item in loaded], [0, 3])
        self.assertEqual([item.end for item in loaded], [3, 5])

    def test_export_llc_requires_media_file_name(self) -> None:
        with self.assertRaises(LosslessEdlError):
            export_segments(self.items, "llc")
        with self.assertRaises(LosslessEdlError):
            parse_segments("", "unknown")

    def test_save_and_load_each_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for extension, fmt in (
                (".csv", "csv"),
                (".tsv", "tsv"),
                (".edl", "mplayer"),
                (".srt", "srt"),
                (".chapters", "chapters"),
            ):
                with self.subTest(extension=extension):
                    path = root / f"cuts{extension}"
                    save_segments(
                        path,
                        self.items,
                        media_duration=5,
                        media_file_name="v.mp4",
                    )
                    loaded = load_segments(path, media_duration=5)
                    self.assertEqual([item.start for item in loaded], [0, 3])
                    self.assertEqual(fmt, format_from_path(path))

            llc_path = root / "cuts.llc"
            save_segments(llc_path, self.items, media_file_name="v.mp4")
            project = load_document(llc_path)
            self.assertEqual(project.media_file_name, "v.mp4")
            self.assertEqual(project.segments[0].tags, {"x": "1"})

    def test_load_document_passes_media_file_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "cuts.csv"
            save_segments(csv_path, self.items, media_duration=5)
            project = load_document(
                csv_path,
                media_file_name="源 素材.mkv",
                media_duration=5,
            )
            self.assertEqual(project.media_file_name, "源 素材.mkv")
            self.assertEqual(project.media_duration, 5)
            self.assertEqual(len(project.segments), 2)

            llc_path = root / "cuts.llc"
            save_segments(llc_path, self.items, media_file_name="自己的素材.mp4")
            llc_project = load_document(
                llc_path,
                media_file_name="别的素材.mkv",
            )
            self.assertEqual(llc_project.media_file_name, "自己的素材.mp4")

            with self.assertRaises(LosslessEdlError):
                load_document(csv_path, media_file_name="../越界.mkv")

    def test_parse_document_keeps_generic_metadata(self) -> None:
        project = parse_document(
            dump_csv(self.items),
            "csv",
            media_file_name="v.mp4",
            media_duration=5,
        )
        self.assertEqual(project.media_file_name, "v.mp4")
        self.assertEqual(project.media_duration, 5)
        self.assertEqual(len(project.segments), 2)
        with self.assertRaises(LosslessEdlError):
            parse_document("0,6\n", "csv", media_duration=5)

    def test_load_segments_uses_extension_or_content_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unknown = root / "cuts.txt"
            unknown.write_text(dump_csv(self.items), encoding="utf-8")
            self.assertEqual(len(load_segments(unknown)), 2)

            empty_csv = root / "empty.csv"
            empty_csv.write_text("", encoding="utf-8")
            self.assertEqual(load_segments(empty_csv), [])

    def test_save_segments_rejects_unknown_extension_and_missing_llc_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(LosslessEdlError):
                save_segments(root / "cuts.txt", self.items)
            with self.assertRaises(LosslessEdlError):
                save_segments(root / "cuts.llc", self.items)

    def test_read_text_file_supports_utf16_and_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "utf16.csv"
            path.write_bytes("start,end\n0,1\n".encode("utf-16"))
            self.assertEqual(read_text_file(path), "start,end\n0,1\n")
            with self.assertRaises(LosslessEdlError):
                read_text_file(root / "missing.csv")

    def test_path_helpers_for_each_text_format(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "cuts.csv"
            save_csv(csv_path, self.items)
            self.assertEqual([item.start for item in load_csv(csv_path)], [0, 3])

            tsv_path = root / "cuts.tsv"
            save_tsv(tsv_path, self.items)
            self.assertEqual([item.end for item in load_tsv(tsv_path)], [2.5, 5])

            srt_path = root / "cuts.srt"
            save_srt(srt_path, self.items)
            self.assertEqual([item.name for item in load_srt(srt_path)], ["A", "B"])

            edl_path = root / "cuts.edl"
            save_mplayer_edl(edl_path, self.items)
            self.assertEqual([item.start for item in load_mplayer_edl(edl_path)], [0, 3])

            chapter_path = root / "cuts.chapters"
            save_chapters(chapter_path, segments_to_chapters(self.items))
            self.assertEqual(
                [item.title for item in load_chapters(chapter_path)],
                ["A", "B"],
            )

    def test_project_dataclass_accepts_generic_documents(self) -> None:
        project = LlcProject("", [segment("a", 0, 1)])
        self.assertEqual(project.media_file_name, "")
        self.assertEqual(len(project.segments), 1)


if __name__ == "__main__":
    unittest.main()
