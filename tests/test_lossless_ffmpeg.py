"""Tests for lossless_ffmpeg command construction and real FFmpeg copying.

Run: py -3 -m unittest tests.test_lossless_ffmpeg -v
The real-media class skips automatically when bin/ffmpeg.exe and
bin/ffprobe.exe are not available.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lossless_ffmpeg as ll  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ll.find_binary("ffmpeg", ROOT)
FFPROBE = ll.find_binary("ffprobe", ROOT)
HAS_REAL_FFMPEG = bool(FFMPEG and FFPROBE)


try:  # The sibling agent supplies this class; the module itself uses duck typing.
    from lossless_core import CutSegment as CoreCutSegment  # type: ignore
except Exception:  # pragma: no cover - optional integration
    CoreCutSegment = None


@dataclass
class CutSegment:
    start: float
    end: float
    name: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    selected: bool = True


PROBE_PAYLOAD = {
    "streams": [
        {
            "index": 0,
            "codec_name": "hevc",
            "codec_long_name": "H.265 / HEVC",
            "profile": "Main 10",
            "codec_type": "video",
            "width": 3840,
            "height": 2160,
            "pix_fmt": "yuv420p10le",
            "avg_frame_rate": "30/1",
            "r_frame_rate": "30/1",
            "time_base": "1/1000",
            "start_time": "0.000000",
            "duration": "120.120000",
            "bit_rate": "50000000",
            "nb_frames": "3600",
            "bits_per_raw_sample": "10",
            "color_primaries": "bt2020",
            "color_transfer": "arib-std-b67",
            "color_space": "bt2020nc",
            "sample_aspect_ratio": "1:1",
            "disposition": {"default": 1, "attached_pic": 0},
            "tags": {"language": "und", "title": "主画面"},
        },
        {
            "index": 1,
            "codec_name": "aac",
            "codec_long_name": "AAC",
            "codec_type": "audio",
            "sample_rate": "48000",
            "channels": 2,
            "channel_layout": "stereo",
            "start_time": "0.000000",
            "duration": "120.000000",
            "bit_rate": "320000",
            "disposition": {"default": 1},
            "tags": {"language": "eng", "title": "现场声"},
        },
        {
            "index": 2,
            "codec_name": "aac",
            "codec_type": "audio",
            "sample_rate": "48000",
            "channels": 2,
            "disposition": {"default": 0},
            "tags": {"language": "jpn"},
        },
        {
            "index": 3,
            "codec_name": "subrip",
            "codec_type": "subtitle",
            "disposition": {"default": 0, "forced": 1},
            "tags": {"language": "chi", "title": "中文字幕"},
        },
        {
            "index": 4,
            "codec_name": "ttf",
            "codec_type": "attachment",
            "disposition": {"default": 0},
            "tags": {"filename": "字体 文件.ttf", "mimetype": "font/ttf"},
        },
        {
            "index": 5,
            "codec_name": "tmcd",
            "codec_type": "data",
            "disposition": {"default": 0},
            "tags": {},
        },
        {
            "index": 6,
            "codec_name": "mjpeg",
            "codec_type": "video",
            "width": 600,
            "height": 600,
            "disposition": {"attached_pic": 1},
            "tags": {"title": "封面"},
        },
    ],
    "chapters": [
        {
            "id": 0,
            "time_base": "1/1000",
            "start": 0,
            "start_time": "0.000000",
            "end": 60000,
            "end_time": "60.000000",
            "tags": {"title": "上半场"},
        },
        {
            "id": 1,
            "time_base": "1/1000",
            "start": 60000,
            "start_time": "60.000000",
            "end": 120000,
            "end_time": "120.000000",
            "tags": {"title": "下半场"},
        },
    ],
    "format": {
        "filename": "D:/素材/源 文件.mkv",
        "format_name": "matroska,webm",
        "format_long_name": "Matroska / WebM",
        "duration": "120.120000",
        "size": "123456789",
        "bit_rate": "8123456",
        "tags": {"title": "样本 标题", "timecode": "01:00:00:00"},
    },
}


def probe(video_codec: str = "hevc") -> ll.MediaProbe:
    payload = json.loads(json.dumps(PROBE_PAYLOAD))
    payload["streams"][0]["codec_name"] = video_codec
    return ll.parse_probe_json("D:/素材/源 文件.mkv", payload)


def keyframes() -> ll.KeyframeIndex:
    return ll.KeyframeIndex(
        stream_index=0,
        frames=tuple(ll.Keyframe(time=value) for value in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5)),
    )


def value_after(command: list[str], option: str) -> str:
    return command[command.index(option) + 1]


def spec_value(command: list[str], option: str) -> str:
    return command[command.index(option) + 1]


class ProbeDataTests(unittest.TestCase):
    def test_all_streams_chapters_format_tags_and_disposition_are_parsed(self) -> None:
        media = probe()
        self.assertEqual(len(media.streams), 7)
        self.assertEqual(len(media.video_streams), 2)
        self.assertEqual(len(media.audio_streams), 2)
        self.assertEqual(len(media.subtitle_streams), 1)
        self.assertEqual(len(media.attachment_streams), 1)
        self.assertEqual(len(media.data_streams), 1)
        self.assertEqual(media.primary_video.index, 0)
        self.assertAlmostEqual(media.duration, 120.12)
        self.assertEqual(media.format_tags["title"], "样本 标题")
        self.assertEqual(media.format.timecode, "01:00:00:00")
        self.assertEqual([chapter.title for chapter in media.chapters], ["上半场", "下半场"])
        self.assertAlmostEqual(media.chapters[1].duration, 60.0)
        self.assertEqual(media.chapters[1].id, 1)
        self.assertAlmostEqual(media.chapters[1].start_time, 60.0)
        self.assertAlmostEqual(media.chapters[1].end_time, 120.0)

        audio = media.audio_streams[0]
        self.assertEqual(audio.language, "eng")
        self.assertEqual(audio.title, "现场声")
        self.assertTrue(audio.is_default)
        self.assertEqual(audio.fps, 0.0)
        self.assertEqual(media.primary_video.fps, 30.0)
        self.assertEqual(media.primary_video.bits_per_raw_sample, 10)

        subtitle = media.subtitle_streams[0]
        self.assertTrue(subtitle.is_forced)
        self.assertFalse(subtitle.is_default)
        self.assertEqual(subtitle.disposition["attached_pic"], 0)

        cover = media.video_streams[1]
        self.assertTrue(cover.is_attached_pic)
        self.assertEqual(cover.title, "封面")
        self.assertEqual(media.audio_streams[0].filename, "")

    def test_probe_json_accepts_string_and_to_dict_preserves_payload(self) -> None:
        media = ll.parse_probe_json("素材.mkv", json.dumps(PROBE_PAYLOAD))
        payload = media.to_dict()
        self.assertEqual(payload["streams"][0]["codec_name"], "hevc")
        self.assertEqual(payload["format"]["tags"]["title"], "样本 标题")
        self.assertEqual(payload["chapters"][0]["tags"]["title"], "上半场")

    def test_probe_rejects_invalid_json_and_non_object(self) -> None:
        with self.assertRaises(ValueError):
            ll.parse_probe_json("a.mkv", "{not json")
        with self.assertRaises(ValueError):
            ll.parse_probe_json("a.mkv", ["not", "an", "object"])

    def test_probe_command_requests_format_streams_chapters_as_json(self) -> None:
        command = ll.build_probe_command("ffprobe", "D:/素材/源 文件.mkv")
        self.assertIn("-show_streams", command)
        self.assertIn("-show_chapters", command)
        self.assertIn("-show_format", command)
        self.assertEqual(value_after(command, "-of"), "json")
        self.assertEqual(command[-1], "D:/素材/源 文件.mkv")

    def test_hls_detection(self) -> None:
        self.assertTrue(ll.parse_probe_json("D:/节目/播放列表.m3u8", PROBE_PAYLOAD).is_hls)
        self.assertTrue(ll.parse_probe_json("https://host/live.m3u8?token=1", PROBE_PAYLOAD).is_hls)
        self.assertFalse(probe().is_hls)


class KeyframeTests(unittest.TestCase):
    def test_parse_keyframes_skips_non_keyframes_and_missing_timestamps(self) -> None:
        payload = {
            "frames": [
                {"best_effort_timestamp_time": "0.000000", "key_frame": 1, "pict_type": "I"},
                {"best_effort_timestamp_time": "0.033333", "key_frame": 0, "pict_type": "P"},
                {"pts_time": "0.500000", "key_frame": 1, "pict_type": "I"},
                {"best_effort_timestamp_time": "N/A", "key_frame": 1},
            ]
        }
        index = ll.parse_keyframes(payload)
        self.assertEqual(index.times, (0.0, 0.5))
        self.assertEqual(index.max_interval, 0.5)
        self.assertAlmostEqual(index.average_interval, 0.5)

    def test_snap_uses_previous_keyframe_and_explains_limit(self) -> None:
        result = ll.resolve_cut_range(0.75, 1.75, keyframes())
        self.assertTrue(result.snapped)
        self.assertAlmostEqual(result.start, 0.5)
        self.assertAlmostEqual(result.duration, 1.25)
        self.assertIn("关键帧", result.warning)
        self.assertIn("0.750", result.warning)
        self.assertNotIn("0.500s 已", result.warning)

    def test_index_snap_returns_previous_or_first_keyframe(self) -> None:
        index = keyframes()
        self.assertEqual(index.snap(0.75), 0.5)
        self.assertEqual(index.snap(0.1), 0.0)
        self.assertEqual(index.snap(-1.0), 0.0)
        self.assertEqual(ll.KeyframeIndex(0, ()).snap(1.25), 1.25)

    def test_exact_keyframe_does_not_snap(self) -> None:
        result = ll.resolve_cut_range(1.0, 2.0, keyframes())
        self.assertFalse(result.snapped)
        self.assertEqual(result.warning, "")
        self.assertAlmostEqual(result.start, 1.0)

    def test_start_before_first_keyframe_moves_forward(self) -> None:
        index = ll.KeyframeIndex(0, (ll.Keyframe(0.5), ll.Keyframe(1.0)))
        result = ll.resolve_cut_range(0.1, 2.0, index)
        self.assertTrue(result.snapped)
        self.assertAlmostEqual(result.start, 0.5)
        self.assertIn("后移", result.warning)

    def test_snapped_range_must_still_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            ll.resolve_cut_range(0.6, 0.55, keyframes())
        with self.assertRaises(ValueError):
            ll.resolve_cut_range(-1, 2, keyframes())
        with self.assertRaises(ValueError):
            ll.resolve_cut_range(1, 1, keyframes())

    def test_generator_keyframes_are_reusable_across_segments(self) -> None:
        generator = (value for value in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5))
        commands = ll.build_segments_copy_commands(
            "ffmpeg", "in.mkv",
            [CutSegment(0.75, 1.75), CutSegment(1.75, 2.75)],
            ["a.mkv", "b.mkv"],
            probe=probe(), keyframes=generator, overwrite=True,
        )
        self.assertEqual(value_after(commands[0], "-ss"), "0.500000")
        self.assertEqual(value_after(commands[1], "-ss"), "1.500000")

    def test_keyframe_probe_command_targets_video_only(self) -> None:
        command = ll.build_keyframe_probe_command("ffprobe", "in.mkv", 2)
        self.assertEqual(value_after(command, "-select_streams"), "v:2")
        self.assertEqual(value_after(command, "-skip_frame"), "nokey")
        self.assertIn("-show_frames", command)


class NamingTests(unittest.TestCase):
    def test_filename_time_uses_windows_safe_form(self) -> None:
        self.assertEqual(ll.format_filename_time(0), "00-00-00-000")
        self.assertEqual(ll.format_filename_time(3661.25), "01-01-01-250")
        self.assertEqual(ll.format_filename_time(-1.5), "-00-00-01-500")

    def test_name_template_supports_name_index_time_tags_and_ext(self) -> None:
        segment = CutSegment(0.75, 1.75, "第一段", {"场景": "开场"})
        name = ll.render_output_name(
            "{stem}_{index:02d}_{name}_{start}_{tag.场景}_{tags[场景]}{ext}",
            segment,
            2,
            "D:/素材/源 文件.mov",
            ".mkv",
        )
        self.assertEqual(
            name,
            "源 文件_02_第一段_00-00-00-750_开场_开场.mkv",
        )

    def test_unknown_placeholder_and_invalid_names_are_safe(self) -> None:
        segment = CutSegment(1, 2, "a:b/c?")
        name = ll.render_output_name("{name}{ext}", segment, 1, "src.mov", ".mkv")
        self.assertEqual(name, "a_b_c_.mkv")
        reserved = ll.render_output_name("{name}{ext}", CutSegment(1, 2, "CON"), 1, "src.mov", ".mkv")
        self.assertEqual(reserved, "_CON.mkv")
        with self.assertRaises(ValueError):
            ll.render_output_name("{unknown}{ext}", segment, 1, "src.mov", ".mkv")

    def test_plan_outputs_skip_unselected_and_detect_collisions(self) -> None:
        segments = [
            CutSegment(0, 1, "一"),
            CutSegment(1, 2, "二", selected=False),
            CutSegment(2, 3, "三"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            outputs = ll.plan_segment_outputs(
                "D:/素材/源.mkv", segments, folder,
                template="{name}{ext}", overwrite=True,
            )
            self.assertEqual([Path(item).name for item in outputs], ["一.mkv", "三.mkv"])
            self.assertTrue(all(Path(item).parent == Path(folder) for item in outputs))
            duplicate_names = [
                CutSegment(0, 1, "同名"),
                CutSegment(1, 2, "同名"),
            ]
            with self.assertRaises(ValueError):
                ll.plan_segment_outputs(
                    "src.mkv", duplicate_names, folder,
                    template="{name}{ext}", overwrite=True,
                )

    def test_existing_output_requires_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            existing = Path(folder) / "正片.mkv"
            existing.write_bytes(b"x")
            with self.assertRaises(FileExistsError):
                ll.plan_segment_outputs(
                    "src.mkv", [CutSegment(0, 1, "正片")], folder,
                    template="{name}{ext}",
                )
            outputs = ll.plan_segment_outputs(
                "src.mkv", [CutSegment(0, 1, "正片")], folder,
                template="{name}{ext}", overwrite=True,
            )
            self.assertEqual(Path(outputs[0]), existing)

    def test_output_extension_is_appended_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                ll.plan_segment_outputs(
                    "src.mkv", [CutSegment(0, 1)], folder, extension=".gif",
                )
        with tempfile.TemporaryDirectory() as folder:
            command = ll.build_segment_copy_command(
                "ffmpeg", "src.mkv", CutSegment(0, 1),
                str(Path(folder) / "片段"),
            )
            self.assertEqual(command[-1], str(Path(folder) / "片段.mkv"))


class StreamSelectionTests(unittest.TestCase):
    def test_mkv_maps_every_suitable_track_and_skips_cover_art(self) -> None:
        media = probe()
        plan = ll.plan_stream_maps(media, "out.mkv")
        self.assertEqual(plan.args, tuple(
            argument
            for stream in (0, 1, 2, 3, 4, 5)
            for argument in ("-map", f"0:{stream}")
        ))
        self.assertEqual([stream.index for stream in plan.skipped], [6])
        self.assertTrue(any("封面图" in warning for warning in plan.warnings))

    def test_mp4_skips_incompatible_subtitle_and_attachment(self) -> None:
        plan = ll.plan_stream_maps(probe(), "out.mp4")
        indices = [int(item.split(":")[1]) for item in plan.args if item.startswith("0:")]
        self.assertEqual(indices, [0, 1, 2, 5])
        self.assertIn(3, [stream.index for stream in plan.skipped])
        self.assertIn(4, [stream.index for stream in plan.skipped])

    def test_explicit_selection_wins_and_warns_about_container(self) -> None:
        media = probe()
        selection = ll.TrackSelection(
            video=(0,), audio=(2,), subtitle=(3,), attachment=(4,), data=False,
        )
        plan = ll.plan_stream_maps(media, "out.mp4", selection)
        indices = [int(item.split(":")[1]) for item in plan.args if item.startswith("0:")]
        self.assertEqual(indices, [0, 2, 3, 4])
        self.assertTrue(any("封装可能失败" in warning for warning in plan.warnings))

    def test_explicit_selection_rejects_wrong_type_and_empty(self) -> None:
        media = probe()
        with self.assertRaises(ValueError):
            ll.plan_stream_maps(media, "out.mkv", ll.TrackSelection(audio=(0,)))
        with self.assertRaises(ValueError):
            ll.plan_stream_maps(
                media, "out.mkv",
                ll.TrackSelection(video=False, audio=False, subtitle=False, attachment=False, data=False),
            )

    def test_generic_maps_are_container_aware(self) -> None:
        mkv = ll.generic_map_args("out.mkv")
        self.assertIn("0:t?", mkv)
        self.assertIn("0:d?", mkv)
        mp4 = ll.generic_map_args("out.mp4")
        self.assertNotIn("0:t?", mp4)
        self.assertIn("0:d?", mp4)
        ts = ll.generic_map_args("out.ts")
        self.assertEqual(ts, ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?"])

    def test_suitability_helper(self) -> None:
        media = probe()
        self.assertTrue(ll.stream_suitable(media.streams[0], "out.mkv"))
        self.assertFalse(ll.stream_suitable(media.streams[6], "out.mkv"))
        self.assertTrue(ll.stream_suitable(media.streams[6], "out.mkv", include_attached_pic=True))
        self.assertTrue(ll.stream_suitable(media.streams[3], "out.mkv"))
        self.assertFalse(ll.stream_suitable(media.streams[3], "out.mp4"))
        self.assertFalse(ll.stream_suitable(media.streams[4], "out.mp4"))
        self.assertTrue(ll.stream_suitable(media.streams[5], "out.mp4"))
        self.assertTrue(ll.stream_suitable(media.streams[1], "out.m4a"))
        self.assertFalse(ll.stream_suitable(media.streams[0], "out.m4a"))
        self.assertFalse(ll.stream_suitable(media.streams[3], "out.m4a"))
        self.assertTrue(ll.stream_suitable(media.streams[1], "out.m4a"))
        self.assertFalse(ll.stream_suitable(media.streams[1], "out.mp3"))
        self.assertTrue(ll.stream_suitable(media.streams[1], "out.mka"))
        self.assertEqual(ll.generic_map_args("out.m4a"), ["-map", "0:a?"])


class MetadataTests(unittest.TestCase):
    def test_title_language_rotate_and_hvc1_use_specifier_tokens(self) -> None:
        media = probe()
        args = ll.build_metadata_args(
            media, "out.mp4",
            ll.MetadataOptions(
                title="成片", language="zho", rotate=90, hvc1=True,
                global_tags={"comment": "无损"},
            ),
        )
        self.assertIn("-metadata", args)
        self.assertIn("title=成片", args)
        self.assertIn("comment=无损", args)
        self.assertIn("-metadata:s:a:0", args)
        self.assertIn("-metadata:s:a:1", args)
        self.assertIn("-metadata:s:s:0", args)
        self.assertIn("-metadata:s:v:0", args)
        self.assertIn("rotate=90", args)
        self.assertIn("-tag:v:0", args)
        self.assertIn("hvc1", args)

    def test_hvc1_is_skipped_for_h264_mkv_and_audio_only_maps(self) -> None:
        args = ll.build_metadata_args(probe("h264"), "out.mp4", ll.MetadataOptions(hvc1=True))
        self.assertNotIn("-tag:v:0", args)
        args = ll.build_metadata_args(probe(), "out.mkv", ll.MetadataOptions(hvc1=True))
        self.assertNotIn("-tag:v:0", args)
        media = probe()
        args = ll.build_metadata_args(
            media, "out.mp4", ll.MetadataOptions(hvc1=True),
            mapped_streams=media.audio_streams,
        )
        self.assertNotIn("-tag:v:0", args)

    def test_rotate_must_be_right_angle(self) -> None:
        with self.assertRaises(ValueError):
            ll.build_metadata_args(probe(), "out.mp4", ll.MetadataOptions(rotate=45))
        args = ll.build_metadata_args(probe(), "out.mp4", ll.MetadataOptions(rotate=-90))
        self.assertIn("rotate=270", args)

    def test_empty_language_and_bad_tag_keys_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ll.build_metadata_args(probe(), "out.mkv", ll.MetadataOptions(language="  "))
        with self.assertRaises(ValueError):
            ll.build_metadata_args(probe(), "out.mkv", ll.MetadataOptions(global_tags={"bad=key": "x"}))


class CommandTests(unittest.TestCase):
    def test_header_uses_overwrite_protection_and_progress(self) -> None:
        safe = ll.build_ffmpeg_base("ffmpeg")
        self.assertIn("-n", safe)
        self.assertNotIn("-y", safe)
        self.assertEqual(value_after(safe, "-progress"), "pipe:1")
        forced = ll.build_ffmpeg_base("ffmpeg", overwrite=True)
        self.assertIn("-y", forced)
        quiet = ll.build_ffmpeg_base("ffmpeg", progress=False)
        self.assertNotIn("-progress", quiet)

    def test_single_segment_command_snaps_keyframe_and_limits_frames(self) -> None:
        media = probe()
        command = ll.build_segment_copy_command(
            "ffmpeg", "D:/素材/源 文件.mkv",
            CutSegment(0.75, 1.75, "第一段"), "D:/输出/片段.mkv",
            probe=media, keyframes=keyframes(), overwrite=True,
        )
        self.assertEqual(value_after(command, "-ss"), "0.500000")
        self.assertEqual(value_after(command, "-t"), "1.250000")
        self.assertEqual(value_after(command, "-frames:v"), "38")
        for index in (0, 1, 2, 3, 4, 5):
            self.assertIn(f"0:{index}", command)
        self.assertNotIn("0:6", command)
        self.assertEqual(value_after(command, "-c"), "copy")
        self.assertEqual(value_after(command, "-map_metadata"), "0")
        self.assertEqual(value_after(command, "-map_chapters"), "-1")
        self.assertIn("-avoid_negative_ts", command)
        self.assertEqual(command[-1], str(Path("D:/输出/片段.mkv")))

    def test_frames_limit_rounds_up_for_single_cfr_video(self) -> None:
        command = ll.build_segment_copy_command(
            "ffmpeg", "D:/素材/源 文件.mkv",
            CutSegment(0.0, 0.75, "短段"), "D:/输出/短段.mkv",
            probe=probe(), snap_start=False, overwrite=True,
        )
        self.assertEqual(value_after(command, "-frames:v"), "23")

    def test_frames_limit_is_skipped_for_vfr_unknown_multi_video_and_audio(self) -> None:
        def variant(mutate) -> ll.MediaProbe:
            payload = json.loads(json.dumps(PROBE_PAYLOAD))
            mutate(payload)
            return ll.parse_probe_json("D:/素材/变体.mkv", payload)

        def vfr(payload):
            payload["streams"][0]["avg_frame_rate"] = "900/31"
            payload["streams"][0]["r_frame_rate"] = "30/1"

        def unknown(payload):
            payload["streams"][0]["avg_frame_rate"] = "0/0"
            payload["streams"][0]["r_frame_rate"] = "0/0"
            payload["streams"][0].pop("nb_frames", None)
            payload["streams"][0].pop("duration", None)

        def multi_video(payload):
            payload["streams"].append({
                "index": 7,
                "codec_name": "h264",
                "codec_type": "video",
                "width": 1280,
                "height": 720,
                "avg_frame_rate": "30/1",
                "r_frame_rate": "30/1",
            })

        def audio_only(payload):
            payload["streams"] = [payload["streams"][1]]

        cases = {
            "vfr": vfr,
            "unknown": unknown,
            "multi_video": multi_video,
            "audio_only": audio_only,
        }
        for name, mutate in cases.items():
            with self.subTest(source=name):
                media = variant(mutate)
                self.assertFalse(ll.media_is_cfr(media))
                command = ll.build_segment_copy_command(
                    "ffmpeg", "D:/素材/源 文件.mkv",
                    CutSegment(0.0, 0.75, "短段"), f"D:/输出/{name}.mkv",
                    probe=media, snap_start=False, overwrite=True,
                )
                self.assertNotIn("-frames:v", command)
        self.assertTrue(ll.media_is_cfr(probe()))

        self.assertFalse(ll.media_is_cfr(replace(probe(), vfr=True)))
        vfr_command = ll.build_segment_copy_command(
            "ffmpeg", "D:/素材/源 文件.mkv",
            CutSegment(0.0, 0.75, "短段"), "D:/输出/探测为VFR.mkv",
            probe=replace(probe(), vfr=True), snap_start=False, overwrite=True,
        )
        self.assertNotIn("-frames:v", vfr_command)

    def test_segment_without_probe_uses_generic_maps_and_can_disable_snap(self) -> None:
        command = ll.build_segment_copy_command(
            "ffmpeg", "in.mkv", CutSegment(1.0, 2.0), "out.mkv",
            snap_start=False, progress=False,
        )
        self.assertEqual(value_after(command, "-ss"), "1.000000")
        self.assertNotIn("-frames:v", command)
        self.assertIn("0:v?", command)
        self.assertIn("0:t?", command)
        self.assertEqual(value_after(command, "-map_chapters"), "-1")

    def test_segment_can_copy_chapters_when_requested(self) -> None:
        command = ll.build_segment_copy_command(
            "ffmpeg", "in.mkv", CutSegment(1.0, 2.0), "out.mkv",
            probe=probe(), keyframes=keyframes(), copy_chapters=True,
        )
        self.assertEqual(value_after(command, "-map_chapters"), "0")

    def test_multiple_segment_commands_respect_selection(self) -> None:
        segments = [
            CutSegment(0, 1, "一"),
            CutSegment(1, 2, "二", selected=False),
            CutSegment(2, 3, "三"),
        ]
        commands = ll.build_segments_copy_commands(
            "ffmpeg", "in.mkv", segments, ["a.mkv", "b.mkv"],
            probe=probe(), keyframes=keyframes(), overwrite=True,
        )
        self.assertEqual(len(commands), 2)
        self.assertEqual([command[-1] for command in commands], ["a.mkv", "b.mkv"])
        self.assertEqual(value_after(commands[1], "-ss"), "2.000000")

    def test_remux_preserves_chapters_and_adds_metadata(self) -> None:
        command = ll.build_remux_command(
            "ffmpeg", "in.mkv", "out.mp4",
            probe=probe(),
            metadata=ll.MetadataOptions(title="成片", language="zho", rotate=90, hvc1=True),
            overwrite=True,
        )
        self.assertEqual(value_after(command, "-c"), "copy")
        self.assertEqual(value_after(command, "-map_chapters"), "0")
        self.assertIn("-metadata:s:a:0", command)
        self.assertIn("-tag:v:0", command)
        self.assertIn("hvc1", command)
        self.assertIn("+faststart", command)
        self.assertEqual(command[-1], "out.mp4")

    def test_remux_can_drop_chapters_and_use_generic_maps(self) -> None:
        command = ll.build_remux_command(
            "ffmpeg", "in.mkv", "out.ts",
            preserve_chapters=False, progress=False,
        )
        self.assertEqual(value_after(command, "-map_chapters"), "-1")
        self.assertNotIn("-movflags", command)

    def test_external_tracks_map_metadata_disposition_and_codec(self) -> None:
        command = ll.build_external_mux_command(
            "ffmpeg", "in.mkv",
            [
                ll.ExternalTrack("a.aac", language="eng", title="主音", default=True),
                ll.ExternalTrack("s.srt", kind="subtitle", language="zho", forced=True, codec="mov_text"),
            ],
            "out.mp4", probe=probe(), overwrite=True,
        )
        self.assertIn("-map", command)
        self.assertIn("1:a:0", command)
        self.assertIn("2:s:0", command)
        self.assertIn("-c:s:0", command)
        self.assertIn("mov_text", command)
        self.assertIn("-metadata:s:a:2", command)
        self.assertIn("language=eng", command)
        self.assertIn("-metadata:s:s:0", command)
        self.assertIn("-disposition:a:2", command)
        self.assertIn("default", command)
        self.assertIn("-disposition:s:0", command)
        self.assertLess(command.index("-c"), command.index("-c:s:0"))

    def test_external_mux_requires_a_track(self) -> None:
        with self.assertRaises(ValueError):
            ll.build_external_mux_command("ffmpeg", "in.mkv", [], "out.mkv", probe=probe())
        with self.assertRaises(ValueError):
            ll.ExternalTrack("x", kind="video")

    def test_extract_video_audio_subtitle_and_data(self) -> None:
        for kind, stream, expected in (
            ("video", 0, "0:v:0"),
            ("audio", 1, "0:a:1"),
            ("subtitle", 0, "0:s:0"),
            ("data", 0, "0:d:0"),
        ):
            command = ll.build_extract_command(
                "ffmpeg", "in.mkv", f"out-{kind}.mkv", kind,
                stream=stream, probe=probe(), overwrite=True,
            )
            self.assertIn(expected, command)
            self.assertEqual(value_after(command, "-c"), "copy")
            self.assertEqual(value_after(command, "-map_chapters"), "-1")

    def test_extract_all_streams_and_transcode_override(self) -> None:
        command = ll.build_extract_command(
            "ffmpeg", "in.mkv", "all.mkv", "audio",
            all_streams=True, probe=probe(), codec="pcm_s24le", overwrite=True,
        )
        self.assertIn("0:a", command)
        self.assertIn("-c:a", command)
        self.assertIn("pcm_s24le", command)

    def test_extract_attachment_uses_dump_attachment(self) -> None:
        command = ll.build_extract_command(
            "ffmpeg", "in.mkv", "font.ttf", "attachment", stream=0,
            probe=probe(), overwrite=True,
        )
        self.assertIn("-dump_attachment:t:0", command)
        self.assertIn("font.ttf", command)
        self.assertEqual(command[-1], "-")
        self.assertEqual(value_after(command, "-f"), "null")

    def test_bulk_attachment_extract_names_and_commands(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            commands = ll.build_attachment_extract_commands(
                "ffmpeg", "in.mkv", folder, probe=probe(), overwrite=True,
            )
            self.assertEqual(len(commands), 1)
            self.assertIn("-dump_attachment:t:0", commands[0])
            self.assertTrue(str(Path(folder) / "字体 文件.ttf") in commands[0])

    def test_chapters_from_source_and_concat_timelines(self) -> None:
        segments = [CutSegment(0.5, 1.5, "一"), CutSegment(1.5, 2.5, "二")]
        source = ll.chapters_from_segments(segments, timeline="source")
        self.assertEqual([(item.start, item.end) for item in source], [(0.5, 1.5), (1.5, 2.5)])
        self.assertEqual([item.title for item in source], ["一", "二"])
        merged = ll.chapters_from_segments(segments, timeline="concat")
        self.assertEqual([(item.start, item.end) for item in merged], [(0.0, 1.0), (1.0, 2.0)])

    def test_chapters_reject_overlap_and_render_ffmetadata(self) -> None:
        with self.assertRaises(ValueError):
            ll.chapters_from_segments(
                [CutSegment(0, 2, "一"), CutSegment(1, 3, "二")],
                timeline="source",
            )
        text = ll.render_chapter_metadata(
            [ll.ChapterInfo(1, 0, 1.5, tags={"title": "a=b;#", "other": "x"})],
            title="成片",
        )
        self.assertTrue(text.startswith(";FFMETADATA1\n"))
        self.assertIn("START=0", text)
        self.assertIn("END=1500", text)
        self.assertIn("title=a\\=b\\;\\#", text)
        self.assertIn("title=成片", text)

    def test_segments_to_chapters_command_imports_metadata_input(self) -> None:
        command = ll.build_segments_chapters_command(
            "ffmpeg", "in.mkv", "out.mkv", "chapters.ffmetadata",
            probe=probe(), overwrite=True,
        )
        self.assertIn("chapters.ffmetadata", command)
        self.assertEqual(value_after(command, "-map_chapters"), "1")
        self.assertEqual(value_after(command, "-c"), "copy")
        with self.assertRaises(ValueError):
            ll.build_segments_chapters_command(
                "ffmpeg", "in.mkv", "out.ts", "chapters.ffmetadata",
                probe=probe(), overwrite=True,
            )

    def test_chapters_are_dropped_for_containers_that_do_not_support_them(self) -> None:
        segment = ll.build_segment_copy_command(
            "ffmpeg", "in.mkv", CutSegment(1, 2), "out.ts",
            probe=probe(), keyframes=keyframes(), copy_chapters=True, overwrite=True,
        )
        self.assertEqual(value_after(segment, "-map_chapters"), "-1")
        remux = ll.build_remux_command(
            "ffmpeg", "in.mkv", "out.ts", probe=probe(), overwrite=True,
        )
        self.assertEqual(value_after(remux, "-map_chapters"), "-1")

    def test_loop_command(self) -> None:
        command = ll.build_loop_command(
            "ffmpeg", "in.mkv", "out.mkv", loops=2, duration=10.5,
            probe=probe(), overwrite=True,
        )
        self.assertEqual(value_after(command, "-stream_loop"), "2")
        self.assertEqual(value_after(command, "-t"), "10.500000")
        self.assertIn("+genpts", command)
        self.assertEqual(value_after(command, "-c"), "copy")

    def test_keyframe_only_copy_and_reencode(self) -> None:
        copy = ll.build_keyframe_only_command(
            "ffmpeg", "in.mkv", "out.mkv", probe=probe(), overwrite=True,
        )
        self.assertEqual(value_after(copy, "-discard"), "nokey")
        self.assertEqual(value_after(copy, "-c"), "copy")
        self.assertNotIn("-vf", copy)

        encoded = ll.build_keyframe_only_command(
            "ffmpeg", "in.mkv", "out.mkv", mode="reencode", overwrite=True,
        )
        self.assertEqual(value_after(encoded, "-vf"), "select=eq(pict_type\\,I)")
        self.assertEqual(value_after(encoded, "-fps_mode"), "vfr")
        self.assertIn("libx264", encoded)

    def test_fps_change_command_and_validation(self) -> None:
        command = ll.build_fps_change_command(
            "ffmpeg", "in.mkv", "out.mp4", "30000/1001", overwrite=True,
        )
        self.assertEqual(value_after(command, "-vf"), "fps=30000/1001")
        self.assertEqual(value_after(command, "-fps_mode"), "cfr")
        self.assertEqual(value_after(command, "-c:a"), "copy")
        with self.assertRaises(ValueError):
            ll.build_fps_change_command("ffmpeg", "in.mkv", "out.mp4", 0)
        with self.assertRaises(ValueError):
            ll.build_fps_change_command("ffmpeg", "in.mkv", "out.mp4", "abc")

    def test_hls_to_mkv_command(self) -> None:
        command = ll.build_hls_to_mkv_command(
            "ffmpeg", "D:/节目/列表.m3u8", "out",
            protocol_whitelist="file,http,https,tcp,tls,crypto",
            overwrite=True,
        )
        self.assertEqual(command[-1], "out.mkv")
        self.assertEqual(value_after(command, "-allowed_extensions"), "ALL")
        self.assertEqual(
            value_after(command, "-protocol_whitelist"),
            "file,http,https,tcp,tls,crypto",
        )
        self.assertEqual(value_after(command, "-c"), "copy")
        with self.assertRaises(ValueError):
            ll.build_hls_to_mkv_command("ffmpeg", "x.m3u8", "out.mp4")

    def test_interval_stills_by_frames_and_seconds(self) -> None:
        frames = ll.build_interval_stills_command(
            "ffmpeg", "in.mkv", "D:/截图",
            every_n_frames=30, start=1.0, end=5.0, overwrite=True,
        )
        self.assertEqual(value_after(frames, "-ss"), "1.000000")
        self.assertEqual(value_after(frames, "-t"), "4.000000")
        self.assertEqual(value_after(frames, "-vf"), "select=not(mod(n\\,30))")
        self.assertEqual(value_after(frames, "-q:v"), "2")
        self.assertTrue(frames[-1].endswith("frame_%06d.jpg"))
        self.assertIn("-start_number", frames)

        seconds = ll.build_interval_stills_command(
            "ffmpeg", "in.mkv", "D:/截图",
            every_n_seconds=2, image_format="png", overwrite=True,
        )
        self.assertEqual(value_after(seconds, "-vf"), "fps=1/2")
        self.assertIn("-compression_level", seconds)
        self.assertTrue(seconds[-1].endswith("frame_%06d.png"))

    def test_interval_stills_validates_exactly_one_interval(self) -> None:
        with self.assertRaises(ValueError):
            ll.build_interval_stills_command("ffmpeg", "in.mkv", "out")
        with self.assertRaises(ValueError):
            ll.build_interval_stills_command(
                "ffmpeg", "in.mkv", "out",
                every_n_frames=10, every_n_seconds=1,
            )
        with self.assertRaises(ValueError):
            ll.build_interval_stills_command(
                "ffmpeg", "in.mkv", "out", every_n_frames=0,
            )


class MergeManifestTests(unittest.TestCase):
    def test_manifest_uses_absolute_forward_slashes_and_ffconcat_quoting(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / "片段 一.mkv"
            second = Path(folder) / "it's 二.mkv"
            text = ll.build_concat_manifest([first, second])
            self.assertTrue(text.startswith("ffconcat version 1.0\n"))
            self.assertIn(first.resolve().as_posix(), text)
            self.assertIn("it'\\''s 二.mkv", text)
            self.assertNotIn("\\\\", text.splitlines()[1])

    def test_write_manifest_is_utf8_lf_and_creates_parents(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "新 目录" / "segments.ffconcat"
            ll.write_concat_manifest(manifest, [Path(folder) / "片段.mkv"])
            data = manifest.read_bytes()
            self.assertNotIn(b"\r\n", data)
            self.assertIn("片段.mkv".encode("utf-8"), data)
            single = Path(folder) / "single.ffconcat"
            ll.write_concat_manifest(single, str(Path(folder) / "只有一个.mkv"))
            self.assertIn("只有一个.mkv", single.read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                ll.build_concat_manifest([])

    def test_merge_command_maps_attachments_and_chapters_from_extra_inputs(self) -> None:
        command = ll.build_concat_merge_command(
            "ffmpeg", "segments.ffconcat", "out.mkv",
            probe=probe(), overwrite=True,
            attachment_source="原片.mkv", chapters_path="chapters.ffmetadata",
        )
        self.assertEqual(value_after(command, "-f"), "concat")
        self.assertEqual(value_after(command, "-safe"), "0")
        self.assertIn("0:v:0", command)
        self.assertIn("0:a:0", command)
        self.assertIn("0:s:0", command)
        self.assertIn("1:t:0", command)
        self.assertIn("-ignore_unknown", command)
        self.assertEqual(value_after(command, "-map_chapters"), "2")
        self.assertIn("+genpts", command)
        self.assertIn("-avoid_negative_ts", command)

    def test_merge_command_without_probe_uses_optional_type_maps(self) -> None:
        command = ll.build_concat_merge_command(
            "ffmpeg", "segments.ffconcat", "out.mkv", progress=False,
        )
        self.assertIn("0:v?", command)
        self.assertIn("0:a?", command)
        self.assertIn("0:s?", command)


class PlanTests(unittest.TestCase):
    def test_separate_plan_builds_one_command_per_selected_segment(self) -> None:
        segments = [
            CutSegment(0.75, 1.75, "一", {"场景": "开场"}),
            CutSegment(1.75, 2.75, "二"),
            CutSegment(2.75, 3.5, "三", selected=False),
        ]
        with tempfile.TemporaryDirectory() as folder:
            plan = ll.plan_lossless_export(
                "ffmpeg", "D:/素材/源 文件.mkv", segments,
                mode="separate", output_dir=folder, template="{name}{ext}",
                probe=probe(), keyframes=keyframes(), overwrite=True,
            )
            self.assertEqual(plan.mode, "separate")
            self.assertEqual(len(plan.commands), 2)
            self.assertEqual([Path(item).name for item in plan.segment_outputs], ["一.mkv", "二.mkv"])
            self.assertIsNone(plan.merged_output)
            self.assertTrue(any("0.750s 不是关键帧" in warning for warning in plan.warnings))
            self.assertEqual(value_after(list(plan.commands[0]), "-ss"), "0.500000")
            self.assertEqual(value_after(list(plan.commands[1]), "-ss"), "1.500000")

    def test_merge_plan_uses_temporary_parts_manifest_and_chapters(self) -> None:
        segments = [CutSegment(0.75, 1.75, "一"), CutSegment(1.75, 2.75, "二")]
        with tempfile.TemporaryDirectory() as folder:
            plan = ll.plan_lossless_export(
                "ffmpeg", "D:/素材/源 文件.mkv", segments,
                mode="merge", output=str(Path(folder) / "合并 成片.mkv"),
                temp_dir=str(Path(folder) / "临时 片段"),
                probe=probe(), keyframes=keyframes(), overwrite=True,
                chapters=True,
            )
            self.assertEqual(plan.mode, "merge")
            self.assertEqual(len(plan.commands), 3)
            self.assertEqual(len(plan.part_outputs), 2)
            self.assertIsNotNone(plan.manifest_text)
            self.assertIn("ffconcat version 1.0", plan.manifest_text or "")
            self.assertIn("临时 片段", plan.manifest_text or "")
            self.assertIsNotNone(plan.chapters_text)
            self.assertIn("title=一", plan.chapters_text or "")
            self.assertIn("END=1250", plan.chapters_text or "")
            self.assertIn("END=2500", plan.chapters_text or "")
            self.assertEqual(plan.cleanup_paths, (str(Path(folder) / "临时 片段"),))
            self.assertEqual(value_after(list(plan.commands[-1]), "-f"), "concat")
            self.assertEqual(value_after(list(plan.commands[-1]), "-map_chapters"), "2")
            self.assertEqual(plan.chapters_path, str(Path(folder) / "临时 片段" / "chapters.ffmetadata"))
            self.assertEqual(plan.manifest_path, str(Path(folder) / "临时 片段" / "segments.ffconcat"))
            sidecars = plan.write_sidecars()
            self.assertEqual(len(sidecars), 2)
            self.assertTrue(all(path.is_file() for path in sidecars))

    def test_both_plan_writes_segments_and_merges_them(self) -> None:
        segments = [CutSegment(0.75, 1.75, "一"), CutSegment(1.75, 2.75, "二")]
        with tempfile.TemporaryDirectory() as folder:
            plan = ll.plan_lossless_export(
                "ffmpeg", "D:/素材/源 文件.mkv", segments,
                mode="both", output=str(Path(folder) / "合并.mkv"),
                output_dir=folder, template="{name}{ext}",
                probe=probe(), keyframes=keyframes(), overwrite=True,
            )
            self.assertEqual(len(plan.commands), 3)
            self.assertEqual(len(plan.segment_outputs), 2)
            self.assertEqual(plan.part_outputs, plan.segment_outputs)
            self.assertEqual(plan.merged_output, str(Path(folder) / "合并.mkv"))
            self.assertIn(Path(plan.segment_outputs[0]).resolve().as_posix(), plan.manifest_text or "")
            self.assertEqual(plan.cleanup_paths, (str(Path(folder) / ".合并_concat"),))

    def test_single_mode_requires_one_segment_and_accepts_explicit_output(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = str(Path(folder) / "单段.mkv")
            plan = ll.plan_lossless_export(
                "ffmpeg", "src.mkv", [CutSegment(1, 2, "一")],
                mode="single", output=output, probe=probe(), keyframes=keyframes(),
                overwrite=True,
            )
            self.assertEqual(plan.segment_outputs, (output,))
            self.assertEqual(len(plan.commands), 1)
            with self.assertRaises(ValueError):
                ll.plan_lossless_export(
                    "ffmpeg", "src.mkv",
                    [CutSegment(0, 1), CutSegment(1, 2)],
                    mode="single", output=output, overwrite=True,
                )

    def test_plan_validation_and_overwrite_protection(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                ll.plan_lossless_export("ffmpeg", "src.mkv", [], output_dir=folder)
            with self.assertRaises(ValueError):
                ll.plan_lossless_export(
                    "ffmpeg", "src.mkv", [CutSegment(0, 1)],
                    mode="unknown", output=str(Path(folder) / "x.mkv"), overwrite=True,
                )
            with self.assertRaises(ValueError):
                ll.plan_lossless_export(
                    "ffmpeg", "src.mkv", [CutSegment(0, 1)],
                    mode="separate", output_dir=folder, chapters=True, overwrite=True,
                )
            with self.assertRaises(ValueError):
                ll.plan_lossless_export(
                    "ffmpeg", "src.mkv", [CutSegment(0, 1)],
                    mode="both", output=str(Path(folder) / "x.mp4"),
                    output_dir=folder, overwrite=True,
                )
            existing = Path(folder) / "合并.mkv"
            existing.write_bytes(b"x")
            with self.assertRaises(FileExistsError):
                ll.plan_lossless_export(
                    "ffmpeg", "src.mkv", [CutSegment(0, 1)],
                    mode="merge", output=str(existing), overwrite=False,
                )

    def test_merge_and_both_reject_overlapping_segments(self) -> None:
        segments = [
            CutSegment(0.0, 2.0, "一"),
            CutSegment(1.0, 3.0, "二"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            for mode in ("merge", "both"):
                with self.subTest(mode=mode):
                    with self.assertRaises(ValueError):
                        ll.plan_lossless_export(
                            "ffmpeg", "src.mkv", segments,
                            mode=mode, output=str(Path(folder) / "合并.mkv"),
                            output_dir=folder, template="{index}{ext}",
                            overwrite=True,
                        )
            plan = ll.plan_lossless_export(
                "ffmpeg", "src.mkv", segments,
                mode="separate", output_dir=folder, template="{index}{ext}",
                overwrite=True,
            )
            self.assertEqual(len(plan.commands), 2)

    def test_both_mode_rejects_merged_file_named_like_a_segment(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            merged = str(Path(folder) / "源 文件.mkv")
            with self.assertRaises(ValueError) as caught:
                ll.plan_lossless_export(
                    "ffmpeg", "D:/素材/源 文件.mkv", [CutSegment(0.0, 1.0, "一")],
                    mode="both", output=merged, output_dir=folder,
                    template="{stem}{ext}", overwrite=True,
                )
            self.assertIn("合并输出与分段输出路径重复", str(caught.exception))

    def test_plan_warns_when_keyframes_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            plan = ll.plan_lossless_export(
                "ffmpeg", "src.mkv", [CutSegment(0, 1)],
                mode="separate", output_dir=folder, overwrite=True,
            )
            self.assertTrue(any("未提供关键帧索引" in warning for warning in plan.warnings))


class RealFfmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not HAS_REAL_FFMPEG:
            raise unittest.SkipTest("需要 bin/ffmpeg.exe 与 bin/ffprobe.exe")
        cls.folder = Path(tempfile.mkdtemp(prefix="lossless-ffmpeg-"))
        cls.source = cls.folder / "素材 目录" / "源 视频.mkv"
        cls.source.parent.mkdir(parents=True)
        base = cls.folder / "base.mp4"
        subtitle = cls.folder / "字 幕.srt"
        chapters = cls.folder / "章 节.ffmetadata"
        attachment = cls.folder / "note.txt"
        cls._run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-g", "15", "-keyint_min", "15", "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "96k", "-shortest", str(base),
        ])
        subtitle.write_text(
            "1\n00:00:00,600 --> 00:00:01,000\n第一句\n\n"
            "2\n00:00:01,800 --> 00:00:02,200\n第二句\n",
            encoding="utf-8",
        )
        chapters.write_text(
            ";FFMETADATA1\n"
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1500\ntitle=第一段\n"
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1500\nEND=3000\ntitle=第二段\n",
            encoding="utf-8",
        )
        attachment.write_text("attachment body\n", encoding="utf-8")
        cls._run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(base), "-i", str(subtitle), "-i", str(chapters),
            "-attach", str(attachment),
            "-map", "0:v", "-map", "0:a", "-map", "1:s",
            "-map_chapters", "2",
            "-metadata:s:t:0", "mimetype=text/plain",
            "-metadata:s:t:0", "filename=note.txt",
            "-metadata", "title=样本 标题",
            "-metadata:s:a:0", "language=eng",
            "-metadata:s:s:0", "language=chi",
            "-c", "copy", "-c:s", "srt",
            str(cls.source),
        ])

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.folder, ignore_errors=True)

    @classmethod
    def _run(cls, command: list[str]) -> None:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=ll.CREATE_NO_WINDOW,
        )
        if result.returncode:
            raise AssertionError(
                f"FFmpeg 命令失败（{result.returncode}）：\n"
                f"{ll.quote_command(command)}\n{result.stderr[-2500:]}"
            )

    def _probe(self, path: str | Path) -> ll.MediaProbe:
        return ll.probe_media(str(FFPROBE), str(path))

    def test_real_probe_reads_streams_chapters_tags_and_dispositions(self) -> None:
        media = self._probe(self.source)
        types = [stream.codec_type for stream in media.streams]
        self.assertEqual(types, ["video", "audio", "subtitle", "attachment"])
        self.assertEqual(media.title, "样本 标题")
        self.assertEqual([chapter.title for chapter in media.chapters], ["第一段", "第二段"])
        self.assertEqual(media.audio_streams[0].language, "eng")
        self.assertEqual(media.subtitle_streams[0].language, "chi")
        self.assertEqual(media.attachment_streams[0].filename, "note.txt")
        self.assertTrue(media.audio_streams[0].is_default)
        self.assertAlmostEqual(media.duration, 3.0, delta=0.2)

    def test_real_keyframe_probe_finds_gop_boundaries(self) -> None:
        index = ll.probe_keyframes(str(FFPROBE), str(self.source))
        self.assertGreaterEqual(len(index.frames), 5)
        self.assertAlmostEqual(index.times[0], 0.0, delta=0.05)
        for expected in (0.5, 1.0, 1.5, 2.0):
            self.assertTrue(
                any(abs(value - expected) < 0.05 for value in index.times),
                f"缺少 {expected}s 关键帧：{index.times}",
            )
        self.assertAlmostEqual(index.max_interval, 0.5, delta=0.05)

    def test_real_stream_copy_cuts_preserve_tracks_metadata_and_names(self) -> None:
        media = self._probe(self.source)
        index = ll.probe_keyframes(str(FFPROBE), str(self.source))
        segments = [
            CutSegment(0.75, 1.75, "一", {"场景": "开场"}),
            CutSegment(1.75, 2.75, "二"),
        ]
        output_dir = self.folder / "输出 目录"
        output_dir.mkdir()
        plan = ll.plan_lossless_export(
            str(FFMPEG), str(self.source), segments,
            mode="separate", output_dir=str(output_dir),
            template="{stem}_片段{index:02d}{ext}",
            probe=media, keyframes=index, overwrite=True,
        )
        self.assertEqual(len(plan.segment_outputs), 2)
        for command in plan.commands:
            self._run(list(command))
        source_types = [stream.codec_type for stream in media.streams]
        for path in plan.segment_outputs:
            self.assertTrue(Path(path).is_file(), path)
            clipped = self._probe(path)
            self.assertEqual([stream.codec_type for stream in clipped.streams], source_types)
            self.assertEqual(clipped.primary_video.codec_name, media.primary_video.codec_name)
            self.assertEqual(clipped.audio_streams[0].codec_name, media.audio_streams[0].codec_name)
            self.assertEqual(clipped.title, "样本 标题")
            self.assertEqual(clipped.attachment_streams[0].filename, "note.txt")
            self.assertGreater(clipped.duration, 1.0)
            self.assertLess(clipped.duration, 1.8)
        self.assertIn("片段01", Path(plan.segment_outputs[0]).name)
        self.assertIn("片段02", Path(plan.segment_outputs[1]).name)

    def test_real_both_mode_exports_segments_and_merged_file(self) -> None:
        media = self._probe(self.source)
        index = ll.probe_keyframes(str(FFPROBE), str(self.source))
        folder = self.folder / "同时 输出"
        folder.mkdir()
        merged = folder / "同时 成片.mkv"
        segments = [CutSegment(0.75, 1.75, "一"), CutSegment(1.75, 2.75, "二")]
        plan = ll.plan_lossless_export(
            str(FFMPEG), str(self.source), segments,
            mode="both", output=str(merged), output_dir=str(folder),
            template="{name}{ext}", probe=media, keyframes=index,
            overwrite=True, chapters=True,
        )
        self.assertEqual(len(plan.commands), 3)
        self.assertEqual(len(plan.segment_outputs), 2)
        plan.write_sidecars()
        for command in plan.commands:
            self._run(list(command))
        for path in (*plan.segment_outputs, str(merged)):
            self.assertTrue(Path(path).is_file(), path)
        result = self._probe(merged)
        self.assertEqual(
            [stream.codec_type for stream in result.streams],
            ["video", "audio", "subtitle", "attachment"],
        )
        self.assertEqual([chapter.title for chapter in result.chapters], ["一", "二"])

    def test_real_ffconcat_merge_preserves_tracks_and_adds_chapters(self) -> None:
        media = self._probe(self.source)
        index = ll.probe_keyframes(str(FFPROBE), str(self.source))
        segments = [CutSegment(0.75, 1.75, "一"), CutSegment(1.75, 2.75, "二")]
        merged = self.folder / "合并 成片.mkv"
        temp_parts = self.folder / "临时 it's 片段"
        plan = ll.plan_lossless_export(
            str(FFMPEG), str(self.source), segments,
            mode="merge", output=str(merged), temp_dir=str(temp_parts),
            probe=media, keyframes=index, overwrite=True, chapters=True,
        )
        self.assertIsNotNone(plan.manifest_path)
        self.assertEqual(len(plan.write_sidecars()), 2)
        for command in plan.commands:
            self._run(list(command))

        self.assertTrue(merged.is_file())
        for part in plan.part_outputs:
            self.assertTrue(Path(part).is_file(), part)
        result = self._probe(merged)
        self.assertEqual(
            [stream.codec_type for stream in result.streams],
            ["video", "audio", "subtitle", "attachment"],
        )
        self.assertEqual([chapter.title for chapter in result.chapters], ["一", "二"])
        self.assertEqual(result.title, "样本 标题")
        self.assertGreater(result.duration, 2.2)
        self.assertLess(result.duration, 3.3)

        packet_output = subprocess.run(
            [
                str(FFPROBE), "-v", "error", "-select_streams", "v:0",
                "-show_packets", "-show_entries", "packet=pts_time",
                "-of", "csv=p=0", str(merged),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=ll.CREATE_NO_WINDOW,
        )
        points = sorted(
            float(line.strip().strip(","))
            for line in packet_output.stdout.splitlines()
            if line.strip()
        )
        gaps = [right - left for left, right in zip(points, points[1:])]
        self.assertLess(max(gaps), 0.5, f"合并后视频时间戳出现大空洞：{max(gaps):.3f}s")
        self.assertAlmostEqual(points[0], 0.0, delta=0.15)


@unittest.skipUnless(HAS_REAL_FFMPEG, "需要 bin/ffmpeg.exe 与 bin/ffprobe.exe")
class RealCfrAndVfrTests(unittest.TestCase):
    """Real FFmpeg checks for the CFR frame cap and VFR pass-through."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.folder = Path(tempfile.mkdtemp(prefix="lossless-cfr-vfr-"))
        cls.cfr = cls.folder / "恒帧 素材.mkv"
        cls.vfr = cls.folder / "可变帧 素材.mkv"
        cls._run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30:duration=1",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-g", "15", "-keyint_min", "15", "-sc_threshold", "0", str(cls.cfr),
        ])
        cls._run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30:duration=2",
            "-vf", "setpts='if(lt(N,10),N/24,10/24+(N-10)/30)/TB'",
            "-fps_mode", "vfr", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-g", "10", str(cls.vfr),
        ])

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.folder, ignore_errors=True)

    @classmethod
    def _run(cls, command: list[str]) -> None:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=ll.CREATE_NO_WINDOW,
        )
        if result.returncode:
            raise AssertionError(
                f"FFmpeg 命令失败（{result.returncode}）：\n"
                f"{ll.quote_command(command)}\n{result.stderr[-2500:]}"
            )

    @classmethod
    def _video_pts(cls, path: str | Path) -> list[float]:
        result = subprocess.run(
            [
                FFPROBE, "-v", "error", "-select_streams", "v:0",
                "-show_packets", "-show_entries", "packet=pts_time",
                "-of", "csv=p=0", str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=ll.CREATE_NO_WINDOW,
        )
        if result.returncode:
            raise AssertionError(result.stderr[-2000:])
        return sorted(
            float(line.strip().strip(","))
            for line in result.stdout.splitlines()
            if line.strip()
        )

    def test_real_cfr_075_seconds_copies_23_frames(self) -> None:
        media = ll.probe_media(str(FFPROBE), str(self.cfr))
        self.assertTrue(ll.media_is_cfr(media))
        self.assertFalse(ll.probe_vfr(str(FFPROBE), str(self.cfr)))
        output = self.folder / "恒帧 0.75 秒.mkv"
        command = ll.build_segment_copy_command(
            str(FFMPEG), str(self.cfr),
            CutSegment(0.0, 0.75, "短段"), str(output),
            probe=media, snap_start=False, overwrite=True,
        )
        self.assertEqual(value_after(command, "-frames:v"), "23")
        self._run(command)
        self.assertTrue(output.is_file())
        self.assertEqual(len(self._video_pts(output)), 23)

    def test_real_vfr_source_has_no_frame_cap_and_keeps_uneven_timestamps(self) -> None:
        media = ll.probe_media(str(FFPROBE), str(self.vfr))
        self.assertTrue(ll.probe_vfr(str(FFPROBE), str(self.vfr)))
        self.assertFalse(ll.media_is_cfr(media))
        output = self.folder / "可变帧 1 秒.mkv"
        command = ll.build_segment_copy_command(
            str(FFMPEG), str(self.vfr),
            CutSegment(0.0, 1.0, "首段"), str(output),
            probe=media, snap_start=False, overwrite=True,
        )
        self.assertNotIn("-frames:v", command)
        self._run(command)
        points = self._video_pts(output)
        self.assertGreater(len(points), 1)
        gaps = {
            round(right - left, 3)
            for left, right in zip(points, points[1:])
        }
        self.assertGreater(len(gaps), 1, f"复制后帧间隔应保持可变：{sorted(gaps)}")


@unittest.skipUnless(CoreCutSegment is not None, "lossless_core 尚未提供 CutSegment")
class CoreCutSegmentIntegrationTests(unittest.TestCase):
    def test_real_lossless_core_cut_segment_is_accepted(self) -> None:
        segment = CoreCutSegment(
            start=0.75, end=1.75, name="一段", tags={"场景": "开场"}, selected=True,
        )
        command = ll.build_segment_copy_command(
            "ffmpeg", "in.mkv", segment, "out.mkv",
            probe=probe(), keyframes=keyframes(), overwrite=True,
        )
        self.assertEqual(value_after(command, "-ss"), "0.500000")
        self.assertEqual(ll.format_filename_time(segment.start), "00-00-00-750")

    def test_real_lossless_core_markers_and_selection_are_respected(self) -> None:
        selected = CoreCutSegment(start=0.75, end=1.75, name="选中", selected=True)
        unselected = CoreCutSegment(start=1.75, end=2.75, name="未选", selected=False)
        marker = CoreCutSegment(start=1.0, end=None, name="标记", selected=True)
        with tempfile.TemporaryDirectory() as folder:
            plan = ll.plan_lossless_export(
                "ffmpeg", "D:/素材/源.mkv",
                [selected, unselected, marker],
                mode="separate", output_dir=folder, template="{name}{ext}",
                probe=probe(), keyframes=keyframes(), overwrite=True,
            )
            self.assertEqual(len(plan.commands), 1)
            self.assertTrue(Path(plan.segment_outputs[0]).name.startswith("选中"))


class UtilityTests(unittest.TestCase):
    def test_frame_rate_normalization(self) -> None:
        self.assertEqual(ll.normalize_frame_rate(29.97), "29.97")
        self.assertEqual(ll.normalize_frame_rate("30000/1001"), "30000/1001")
        self.assertEqual(ll.normalize_frame_rate(60), "60")
        self.assertAlmostEqual(ll.parse_frame_rate("30000/1001"), 29.97002997)
        with self.assertRaises(ValueError):
            ll.normalize_frame_rate(float("nan"))

    def test_selected_segments_handles_missing_and_string_flags(self) -> None:
        self.assertTrue(ll.segment_is_selected(object()))
        self.assertTrue(ll.segment_is_selected(CutSegment(0, 1, selected=None)))  # type: ignore[arg-type]
        self.assertFalse(ll.segment_is_selected({"start": 0, "end": 1, "selected": "no"}))
        self.assertTrue(ll.segment_is_selected({"start": 0, "end": 1, "selected": "yes"}))
        items = ll.selected_segments([CutSegment(0, 1), CutSegment(1, 2, selected=False)])
        self.assertEqual(len(items), 1)

    def test_markers_are_ignored_and_rejected_as_cut_ranges(self) -> None:
        marker = {"start": 1.0, "end": None, "selected": True}
        self.assertTrue(ll.segment_is_marker(marker))
        self.assertEqual(ll.selected_segments([marker, CutSegment(0, 1)]), (CutSegment(0, 1),))
        self.assertEqual(ll.selected_segments([(0.0, 1.0)]), ((0.0, 1.0),))
        with self.assertRaises(ValueError):
            ll.cut_segment_range(marker)

    def test_segment_duck_typing_accepts_mapping_and_tuple(self) -> None:
        self.assertEqual(ll.cut_segment_range({"start": 1, "end": 2}), (1.0, 2.0))
        self.assertEqual(ll.cut_segment_range((1, 2)), (1.0, 2.0))
        with self.assertRaises(TypeError):
            ll.cut_segment_range(object())
        with self.assertRaises(ValueError):
            ll.cut_segment_range((2, 1))
        with self.assertRaises(ValueError):
            ll.cut_segment_range((0, float("inf")))

    def test_quote_command_handles_spaces_and_non_ascii(self) -> None:
        text = ll.quote_command(["ffmpeg", "-i", "D:/素材/源 文件.mkv"])
        self.assertIn("源 文件.mkv", text)
        self.assertTrue(text.startswith("ffmpeg"))


if __name__ == "__main__":
    unittest.main()
