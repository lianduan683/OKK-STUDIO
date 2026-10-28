"""Subtitle import, persistence, safe rendering, and real CPU libass coverage."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subtitle_core import (
    SubtitleCue, SubtitleStyle, SubtitleTrack, MAX_SRT_BYTES,
    escape_ass_text, parse_srt, subtitle_at, subtitle_filter, subtitle_track_from_dict,
    subtitle_track_to_dict, validate_subtitle_track, write_ass, write_srt,
)


class SubtitleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def srt(self, content, encoding="utf-8"):
        path = self.root / "字幕.srt"
        path.write_text(content, encoding=encoding, newline="")
        return path

    def test_all_supported_encodings_crlf_multiline_and_optional_index(self):
        content = "1\r\n00:00:01,250 --> 00:00:03,500\r\n中文第一行\r\n第二行\r\n\r\n00:04.5 --> 00:05.75\r\n<b>结束</b>\r\n"
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "gb18030"):
            with self.subTest(encoding=encoding):
                cues = parse_srt(self.srt(content, encoding))
                self.assertEqual(len(cues), 2)
                self.assertEqual(cues[0].text, "中文第一行\n第二行")
                self.assertEqual((cues[0].start, cues[0].end), (1.25, 3.5))
                self.assertEqual((cues[1].start, cues[1].end, cues[1].text), (4.5, 5.75, "结束"))

    def test_out_of_order_cues_sorted_and_duplicate_srt_indexes_safe(self):
        cues = parse_srt(self.srt("1\n00:00:04,000 --> 00:00:05,000\nB\n\n1\n00:00:01,000 --> 00:00:02,000\nA"))
        self.assertEqual([cue.text for cue in cues], ["A", "B"])
        self.assertEqual(len({cue.id for cue in cues}), 2)

    def test_bad_time_and_incomplete_cue_report_errors(self):
        for content in ("", "1\nmissing time\ntext", "1\n00:61:00,000 --> 00:62:00,000\ntext",
                        "1\n00:00:03,000 --> 00:00:02,000\ntext",
                        "1\n00:00:02,000 --> 00:00:02,000\ntext",
                        "1\n00:00:00,000 --> 00:00:02,000\n", "bad\n00:00:00,000 --> 00:00:02,000\ntext"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_srt(self.srt(content))

    def test_import_size_cap_before_loading(self):
        path = self.srt("x")
        with path.open("r+b") as file:
            file.truncate(MAX_SRT_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "16 MiB"):
            parse_srt(path)

    def test_roundtrip_all_style_and_track_fields(self):
        track = SubtitleTrack(
            [SubtitleCue("one", 1, 3, "中文")],
            SubtitleStyle(font_family="Arial", font_size=67, color="#123456", outline_color="#543210",
                          outline_width=3.5, shadow=2, bold=True, italic=True, alignment=8,
                          margin_x=32, margin_y=50, box=True, background_color="#445566", background_opacity=.75),
            offset=-2.5, enabled=False, burn_in=False, source_path="C:/字幕.srt")
        self.assertEqual(subtitle_track_from_dict(subtitle_track_to_dict(track)), track)
        self.assertEqual(subtitle_track_from_dict({}), SubtitleTrack())

    def test_style_validation_rejects_nonfinite_and_injection(self):
        for fields in ({"font_size": float("nan")}, {"font_family": "Arial,48\n[Events]"},
                       {"font_family": "{\\pos(0,0)}"}, {"alignment": 10}, {"alignment": True},
                       {"color": "white"}, {"background_opacity": 80}, {"margin_x": -1},
                       {"bold": 1}, {"shadow": float("inf")}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_subtitle_track(SubtitleTrack(style=replace(SubtitleStyle(), **fields)))
        for data in (None, {"unexpected": True}, {"enabled": 1}, {"offset": float("nan")}, {"cues": "x"},
                     {"cues": [{"id": "bad", "start": -1, "end": 1, "text": "x"}]},
                     {"style": {"unexpected": 1}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                subtitle_track_from_dict(data)

    def test_offset_overlaps_and_half_open_playback(self):
        track = SubtitleTrack([SubtitleCue("a", 1, 4, "A"), SubtitleCue("b", 2, 3, "B")], offset=1)
        self.assertEqual(subtitle_at(track, 1), [])
        self.assertEqual([cue.id for cue in subtitle_at(track, 3)], ["a", "b"])
        self.assertEqual([cue.id for cue in subtitle_at(track, 4)], ["a"])
        self.assertEqual(subtitle_at(track, 5), [])
        track.enabled = False
        self.assertEqual(subtitle_at(track, 3), [])

    def test_range_export_clips_boundaries_applies_offset_without_mutating(self):
        track = SubtitleTrack([SubtitleCue("a", 0, 3, "跨起点"), SubtitleCue("b", 4, 9, "跨终点"),
                               SubtitleCue("c", 10, 12, "范围外")], offset=1)
        before = subtitle_track_to_dict(track)
        path = write_srt(track, self.root / "out.srt", 2, 7)
        cues = parse_srt(path)
        self.assertEqual([(cue.start, cue.end, cue.text) for cue in cues], [(0, 2, "跨起点"), (3, 5, "跨终点")])
        ass = write_ass(track, self.root / "out.ass", 1920, 1080, 2, 7).read_text(encoding="utf-8-sig")
        self.assertIn("Dialogue: 0,00:00:00.00,00:00:02.00", ass)
        self.assertIn("Dialogue: 0,00:00:03.00,00:00:05.00", ass)
        self.assertNotIn("范围外", ass)
        self.assertEqual(before, subtitle_track_to_dict(track))

    def test_ass_safe_text_style_scaling_and_newlines(self):
        malicious = r"{\pos(0,0)\an8}hello\Ninjected" + "\n<b>中文</b> &amp; <script>literal</script>"
        track = SubtitleTrack([SubtitleCue("a", .004, 1.001, malicious)],
                              SubtitleStyle(color="#123456", bold=True, italic=True, font_size=48))
        text = write_ass(track, self.root / "safe.ass", 3840, 2160).read_text(encoding="utf-8-sig")
        self.assertIn("Default,Microsoft YaHei,96,&H00563412", text)
        self.assertIn("4,2,2,120,120,120,1", text)
        event = text.split("Dialogue: ")[1]
        self.assertNotIn(r"\pos", event)
        self.assertNotIn("{", event)
        self.assertIn(r"\N中文 & <script>literal</script>", event)
        self.assertIn("00:00:00.00,00:00:01.01", event)
        self.assertEqual(escape_ass_text("a\nb"), r"a\Nb")

    def test_disabled_track_writes_no_events_and_invalid_range_rejected(self):
        track = SubtitleTrack([SubtitleCue("a", 0, 1, "A")], enabled=False)
        self.assertNotIn("Dialogue:", write_ass(track, self.root / "off.ass", 320, 180).read_text(encoding="utf-8-sig"))
        self.assertEqual(write_srt(track, self.root / "off.srt").read_text(encoding="utf-8-sig"), "")
        for start, end in ((2, 1), (1, 1), (-1, 2), (0, float("nan"))):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                write_srt(track, self.root / "invalid.srt", start, end)


class SubtitleLibassTests(unittest.TestCase):
    """One actual encoded clip verifies Chinese font rendering and range timing."""
    def test_chinese_styled_subtitles_reach_exported_pixels(self):
        ffmpeg = Path(__file__).resolve().parents[1] / "bin" / "ffmpeg.exe"
        if not ffmpeg.is_file():
            self.skipTest("bundled FFmpeg unavailable")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            track = SubtitleTrack([SubtitleCue("cn", .5, 1.5, "中文测试\n字幕轨道")],
                                  SubtitleStyle(font_size=160, bold=True, color="#FF0000", outline_width=0, shadow=0))
            unusual = root / "中文 目录'号[字幕],分;号"
            unusual.mkdir()
            ass_path = write_ass(track, unusual / "subtitle's 中文.ass", 640, 360)
            command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                       "color=c=black:s=640x360:r=10:d=2", "-vf", subtitle_filter(ass_path), "-c:v", "libx264",
                       "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p", "clip.mp4"]
            encoded = subprocess.run(command, cwd=root, capture_output=True, timeout=45)
            self.assertEqual(encoded.returncode, 0, encoded.stderr.decode(errors="replace"))
            def frame(timestamp):
                result = subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", str(timestamp),
                                         "-i", "clip.mp4", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                        cwd=root, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertEqual(len(result.stdout), 640 * 360 * 3)
                return result.stdout
            dark = frame(.1)
            visible = frame(.8)
            after = frame(1.7)
            red_pixels = sum(1 for r, g, b in zip(visible[::3], visible[1::3], visible[2::3]) if r > 70 and r > g * 2 and r > b * 2)
            self.assertGreater(red_pixels, 250, "Chinese red subtitle did not render")
            self.assertLess(sum(dark), 5000)
            self.assertLess(sum(after), 5000)


if __name__ == "__main__":
    unittest.main()
