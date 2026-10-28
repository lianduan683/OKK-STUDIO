from __future__ import annotations

import random
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from editor_core import (
    AlignmentResult,
    _normalized_fft_correlation,
    _normalized_fft_correlation_python,
    aligned_audio_intervals,
    aligned_clip_timeline,
    build_aligned_concat_copy_command,
    build_audio_encode_args,
    build_concat_encode_command,
    build_gap_preserving_concat_encode_command,
    build_concat_copy_command,
    build_extract_audio_command,
    build_extract_video_command,
    build_frame_capture_command,
    build_replace_audio_command,
    build_trim_copy_command,
    concat_compatibility_issues,
    duplicate_proxy_stems,
    format_time,
    format_signed_time,
    gap_preserving_timeline,
    make_1080p_proxy_settings,
    normalize_audio_bitrate,
    normalize_audio_sample_rate,
    normalize_video_bitrate,
    parse_timecode,
    parse_signed_timecode,
    proxy_output_path,
    proxy_source_conflicts,
    resolve_join_output_range,
    write_concat_manifest,
)
from exporter_core import Capabilities, ExportSettings, MediaInfo, build_encode_command


def input_window(command, path):
    """Return the seek/read limits attached to this input, not the output."""
    end = command.index(path) - 1
    assert command[end] == '-i'
    previous_inputs = [index for index, value in enumerate(command[:end]) if value == '-i']
    start = previous_inputs[-1] + 2 if previous_inputs else 1
    options = command[start:end]
    return float(options[options.index('-ss') + 1]), float(options[options.index('-t') + 1])


def picture_span_frames(command):
    graph = command[command.index('-filter_complex') + 1]
    # The final frame trim of each camera/black span defines its programme
    # duration. A generated black span may first trim to one reusable frame.
    return [int(re.findall(r'trim=end_frame=(\d+)', chain)[-1])
            for chain in graph.split(';') if re.search(r'\[vspan\d+\]$', chain)]


class EditorCoreTests(unittest.TestCase):
    def test_format_time(self) -> None:
        self.assertEqual(format_time(3661.25), "01:01:01.250")

    def test_parse_timecode_supports_unified_and_seconds_formats(self) -> None:
        self.assertAlmostEqual(parse_timecode("01:02:03.456"), 3723.456)
        self.assertAlmostEqual(parse_timecode("02:03.500"), 123.5)
        self.assertAlmostEqual(parse_timecode("12.25"), 12.25)

    def test_parse_timecode_rejects_invalid_seconds(self) -> None:
        with self.assertRaises(ValueError):
            parse_timecode("00:00:61.000")

    def test_signed_timecode_supports_video_before_master_audio(self) -> None:
        self.assertAlmostEqual(parse_signed_timecode("-00:03:10.620"), -190.62)
        self.assertEqual(format_signed_time(-190.62), "-00:03:10.620")

    def test_frame_capture_is_single_frame(self) -> None:
        command = build_frame_capture_command("ffmpeg", "input.mov", 12.3456789, "frame.png")
        self.assertIn("-frames:v", command)
        self.assertEqual(command[command.index("-frames:v") + 1], "1")
        self.assertEqual(command[command.index("-ss") + 1], "12.345679")

    def test_lossless_trim_uses_duration_and_stream_copy(self) -> None:
        command = build_trim_copy_command("ffmpeg", "input.mov", "trim.mov", 10.0, 25.5)
        self.assertEqual(command[command.index("-t") + 1], "15.500000")
        self.assertEqual(command[command.index("-c") + 1], "copy")
        self.assertEqual(command[command.index("-progress") + 1], "pipe:1")

    def test_concat_manifest_and_copy_command(self) -> None:
        with TemporaryDirectory() as folder:
            first = Path(folder) / "第一段.mp4"
            second = Path(folder) / "第二段.mp4"
            manifest = Path(folder) / "segments.ffconcat"
            write_concat_manifest([str(first), str(second)], manifest)
            text = manifest.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("ffconcat version 1.0\n"))
            self.assertIn("第一段.mp4", text)
            command = build_concat_copy_command("ffmpeg", str(manifest), "joined.mp4")
            self.assertEqual(command[command.index("-f") + 1], "concat")
            self.assertEqual(command[command.index("-c") + 1], "copy")
            self.assertEqual(command[command.index("-progress") + 1], "pipe:1")

    def test_concat_compatibility_reports_resolution_difference(self) -> None:
        first = MediaInfo("a.mp4", width=3840, height=2160, fps=30, video_codec="hevc", pixel_format="yuv420p10le")
        second = MediaInfo("b.mp4", width=1920, height=1080, fps=30, video_codec="hevc", pixel_format="yuv420p10le")
        issues = concat_compatibility_issues([first, second])
        self.assertIn("分辨率", issues[0])

    def test_concat_can_ignore_camera_audio_when_master_audio_replaces_it(self) -> None:
        first = MediaInfo("a.mov", width=3840, height=2160, fps=30, video_codec="hevc", pixel_format="yuv420p10le", has_audio=True, audio_codec="pcm_s24le")
        second = MediaInfo("b.mov", width=3840, height=2160, fps=30, video_codec="hevc", pixel_format="yuv420p10le", has_audio=True, audio_codec="aac")
        self.assertTrue(concat_compatibility_issues([first, second]))
        self.assertEqual(concat_compatibility_issues([first, second], include_audio=False), [])

    def test_alignment_intervals_remove_camera_file_gap_from_master_audio(self) -> None:
        results = [
            AlignmentResult(0.0, 0.95, 50),
            AlignmentResult(-7505.0, 0.93, 50),
        ]
        intervals = aligned_audio_intervals(results, [7500.0, 3600.0])
        self.assertEqual(intervals, [(0.0, 7500.0), (7505.0, 3600.0)])

    def test_video_before_audio_can_be_trimmed_or_kept_silent(self) -> None:
        results = [AlignmentResult(190.62, 0.95, 50)]
        trims, trimmed_intervals = aligned_clip_timeline(
            results, [1000.0], trim_before_audio=True,
        )
        self.assertEqual(trims, [190.62])
        self.assertAlmostEqual(trimmed_intervals[0][0], 0.0)
        self.assertAlmostEqual(trimmed_intervals[0][1], 809.38)
        kept_trims, silent_intervals = aligned_clip_timeline(
            results, [1000.0], keep_before_audio=True,
        )
        self.assertEqual(kept_trims, [0.0])
        self.assertAlmostEqual(silent_intervals[0][0], -190.62)
        self.assertAlmostEqual(silent_intervals[0][1], 1000.0)

    def test_gap_timeline_keeps_camera_off_time_for_continuous_audio(self) -> None:
        results = [AlignmentResult(0.0, 0.95, 50), AlignmentResult(-7505.0, 0.93, 50)]
        starts, gaps, audio_start, total = gap_preserving_timeline(results, [7500.0, 3600.0])
        self.assertEqual(starts, [0.0, 7505.0])
        self.assertEqual(gaps, [5.0, 0.0])
        self.assertEqual(audio_start, 0.0)
        self.assertEqual(total, 11105.0)

    def test_output_range_can_end_with_master_audio_or_keep_silent_video(self) -> None:
        results = [AlignmentResult(0.0, 0.95, 50), AlignmentResult(-105.0, 0.93, 50)]
        cropped = resolve_join_output_range(
            results, [100.0, 100.0], preserve_gaps=True,
            master_audio_duration=180.0, trim_after_audio=True,
        )
        kept = resolve_join_output_range(
            results, [100.0, 100.0], preserve_gaps=True,
            master_audio_duration=180.0, trim_after_audio=False,
        )
        self.assertEqual(cropped, (0.0, 180.0, 205.0))
        self.assertEqual(kept, (0.0, 205.0, 205.0))

    def test_manual_output_range_overrides_automatic_boundaries(self) -> None:
        results = [AlignmentResult(0.0, 0.95, 50), AlignmentResult(-105.0, 0.93, 50)]
        start, end, total = resolve_join_output_range(
            results, [100.0, 100.0], preserve_gaps=True,
            master_audio_duration=180.0, trim_after_audio=True,
            custom_start=12.5, custom_end=190.0,
        )
        self.assertEqual((start, end, total), (12.5, 190.0, 205.0))
        with self.assertRaisesRegex(ValueError, "超过可用画面时长"):
            resolve_join_output_range(
                results, [100.0, 100.0], custom_end=250.0,
            )

    def test_bare_video_bitrate_means_kbps(self) -> None:
        self.assertEqual(normalize_video_bitrate("40000"), "40000k")
        self.assertEqual(normalize_video_bitrate("80M"), "80M")
        with self.assertRaises(ValueError):
            normalize_video_bitrate("fast")

    def test_editable_audio_bitrate_and_sample_rate_are_normalized(self) -> None:
        self.assertEqual(normalize_audio_bitrate("96"), "96k")
        self.assertEqual(normalize_audio_bitrate("320k"), "320k")
        self.assertEqual(normalize_audio_sample_rate("48k"), 48000)
        self.assertEqual(normalize_audio_sample_rate("96000"), 96000)
        self.assertIsNone(normalize_audio_sample_rate("保留原采样率"))

    def test_pcm_bit_depths_require_mov_or_mkv(self) -> None:
        for codec in ("pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le"):
            command = build_audio_encode_args(codec, "320k", 96000, "concert.mov")
            self.assertEqual(command[:2], ["-c:a", codec])
            self.assertIn("-ar", command)
            self.assertNotIn("-b:a", command)
            with self.assertRaises(ValueError):
                build_audio_encode_args(codec, "320k", 48000, "concert.mp4")

    def test_aac_supports_manual_bitrate_but_rejects_ultrahigh_sample_rate(self) -> None:
        command = build_audio_encode_args("aac", "48", 48000, "concert.mp4")
        self.assertIn("48k", command)
        self.assertIn("48000", command)
        with self.assertRaises(ValueError):
            build_audio_encode_args("aac", "320k", 192000, "concert.mp4")

    def test_aligned_lossless_concat_is_one_ffmpeg_job(self) -> None:
        command = build_aligned_concat_copy_command(
            "ffmpeg", "segments.ffconcat", "master.flac",
            [(0.0, 7500.0), (7505.0, 3600.0)], "concert.mp4", "320k",
        )
        joined = " ".join(command)
        self.assertIn("-f concat", joined)
        self.assertIn("atrim=start=7505.000000:duration=3600.000000", joined)
        self.assertIn("concat=n=2:v=0:a=1", joined)
        self.assertIn("-c:v copy", joined)
        self.assertIn("-c:a aac -b:a 320k", joined)

    def test_aligned_concat_can_write_32_bit_float_pcm(self) -> None:
        command = build_aligned_concat_copy_command(
            "ffmpeg", "segments.ffconcat", "master.flac",
            [(0.0, 10.0), (12.0, 8.0)], "concert.mov",
            audio_codec="pcm_f32le", audio_sample_rate=96000,
        )
        joined = " ".join(command)
        self.assertIn("-c:a pcm_f32le -ar 96000", joined)
        self.assertNotIn("-b:a", joined)

    def test_aligned_nvenc_concat_encodes_directly_at_target_bitrate(self) -> None:
        media = MediaInfo("a.mov", width=3840, height=2160, fps=30, video_codec="hevc", pixel_format="yuv420p10le", bit_depth=10)
        settings = ExportSettings(codec="hevc_nvenc", resolution="original", bit_depth="10-bit", video_bitrate="80M", decode_mode="cuda")
        caps = Capabilities(
            ffmpeg_path="ffmpeg", ffprobe_path="ffprobe", gpu_name="RTX 5090 Laptop GPU",
            nvenc_codecs={"hevc_nvenc"}, cuda_decode=True, scale_cuda=True,
            raw_encoder_help={"hevc_nvenc": "\n -tune value\n -multipass value\n"},
        )
        command = build_concat_encode_command(
            "ffmpeg", "segments.ffconcat", "concert.mp4", settings, media, caps,
            external_audio="master.flac", intervals=[(0.0, 7500.0), (7505.0, 3600.0)],
        )
        joined = " ".join(command)
        self.assertIn("-f concat", joined)
        self.assertIn("-c:v hevc_nvenc", joined)
        self.assertIn("-b:v 80M", joined)
        self.assertNotIn("-cq", command)
        self.assertEqual(command[-1], "concert.mp4")

    def test_gap_preserving_export_adds_black_frames_and_keeps_master_audio_continuous(self) -> None:
        media = [
            MediaInfo("a.mov", duration=7500.0, width=3840, height=2160, fps=30, video_codec="hevc", pixel_format="yuv420p10le", bit_depth=10),
            MediaInfo("b.mov", duration=3600.0, width=3840, height=2160, fps=30, video_codec="hevc", pixel_format="yuv420p10le", bit_depth=10),
        ]
        results = [AlignmentResult(0.0, 0.95, 50), AlignmentResult(-7505.0, 0.93, 50)]
        settings = ExportSettings(codec="hevc_nvenc", resolution="original", bit_depth="10-bit", video_bitrate="80M", decode_mode="cuda")
        caps = Capabilities(
            ffmpeg_path="ffmpeg", ffprobe_path="ffprobe", gpu_name="RTX 5090 Laptop GPU",
            nvenc_codecs={"hevc_nvenc"}, cuda_decode=True,
            raw_encoder_help={"hevc_nvenc": "\n -tune value\n -multipass value\n"},
        )
        command = build_gap_preserving_concat_encode_command(
            "ffmpeg", ["a.mov", "b.mov"], "master.flac", results, media,
            "concert.mp4", settings, caps,
        )
        joined = " ".join(command)
        self.assertEqual(picture_span_frames(command), [7500*30, 5*30, 3600*30])
        self.assertIn("color=c=black:s=3840x2160:r=30/1", joined)
        self.assertIn("loop=loop=149:size=1:start=0", joined)
        self.assertNotIn("hwdownload", joined)
        self.assertEqual(input_window(command, "master.flac"), (0.0, 11105.0))
        self.assertIn("atrim=duration=11105.000000", joined)
        self.assertEqual(float(command[-2]), 11105.0)
        self.assertIn("-c:v hevc_nvenc", joined)
        self.assertIn("-b:v 80M", joined)
        self.assertNotIn("-f concat", joined)

    def test_aligned_export_can_trim_video_to_master_audio_start(self) -> None:
        media = [
            MediaInfo("a.mov", duration=1000.0, width=3840, height=2160, fps=30, video_codec="hevc", bit_depth=10),
            MediaInfo("b.mov", duration=500.0, width=3840, height=2160, fps=30, video_codec="hevc", bit_depth=10),
        ]
        results = [AlignmentResult(190.62, 0.95, 50), AlignmentResult(-814.38, 0.93, 50)]
        settings = ExportSettings(codec="hevc_nvenc", resolution="original", bit_depth="10-bit", video_bitrate="80M")
        caps = Capabilities(nvenc_codecs={"hevc_nvenc"})
        command = build_gap_preserving_concat_encode_command(
            "ffmpeg", ["a.mov", "b.mov"], "master.wav", results, media,
            "concert.mp4", settings, caps, trim_before_audio=True,
        )
        joined = " ".join(command)
        first_seek, first_read = input_window(command, "a.mov")
        self.assertAlmostEqual(first_seek, 190.62)
        self.assertAlmostEqual(first_read, 24281/30 + 1/30, places=5)
        self.assertEqual(picture_span_frames(command), [24281, 150, 15000])
        audio_seek, audio_read = input_window(command, "master.wav")
        self.assertEqual(audio_seek, 0)
        self.assertAlmostEqual(audio_read, (24281+150+15000)/30, places=5)
        self.assertNotIn("trim=start=190.620000", joined)
        self.assertNotIn("adelay=190620", joined)

    def test_aligned_export_can_keep_early_video_with_silent_lead_in(self) -> None:
        media = [
            MediaInfo("a.mov", duration=1000.0, width=3840, height=2160, fps=30, video_codec="hevc", bit_depth=10),
            MediaInfo("b.mov", duration=500.0, width=3840, height=2160, fps=30, video_codec="hevc", bit_depth=10),
        ]
        results = [AlignmentResult(190.62, 0.95, 50), AlignmentResult(-814.38, 0.93, 50)]
        settings = ExportSettings(codec="hevc_nvenc", resolution="original", bit_depth="10-bit", video_bitrate="80M")
        caps = Capabilities(nvenc_codecs={"hevc_nvenc"})
        command = build_gap_preserving_concat_encode_command(
            "ffmpeg", ["a.mov", "b.mov"], "master.wav", results, media,
            "concert.mp4", settings, caps, keep_before_audio=True,
        )
        joined = " ".join(command)
        self.assertNotIn("trim=start=190.620000", joined)
        self.assertEqual(input_window(command, "a.mov")[0], 0)
        self.assertEqual(picture_span_frames(command), [30000, 150, 15000])
        delay = re.search(r'adelay=([0-9.]+):all=1', joined)
        self.assertIsNotNone(delay)
        self.assertAlmostEqual(float(delay.group(1)), 190620)
        self.assertEqual(input_window(command, "master.wav"), (0.0, 1314.38))
        self.assertEqual(float(command[-2]), 1505.0)

    def test_aligned_export_trims_picture_and_audio_at_master_audio_end(self) -> None:
        media = [
            MediaInfo("a.mov", duration=100.0, width=3840, height=2160, fps=30, video_codec="hevc", bit_depth=10),
            MediaInfo("b.mov", duration=100.0, width=3840, height=2160, fps=30, video_codec="hevc", bit_depth=10),
        ]
        results = [AlignmentResult(0.0, 0.95, 50), AlignmentResult(-105.0, 0.93, 50)]
        settings = ExportSettings(codec="hevc_nvenc", resolution="original", bit_depth="10-bit", video_bitrate="80M")
        caps = Capabilities(nvenc_codecs={"hevc_nvenc"})
        command = build_gap_preserving_concat_encode_command(
            "ffmpeg", ["a.mov", "b.mov"], "master.wav", results, media,
            "concert.mov", settings, caps, master_audio_duration=180.0,
            trim_after_audio=True,
        )
        joined = " ".join(command)
        self.assertEqual(picture_span_frames(command), [100*30, 5*30, 75*30])
        second_seek, second_read = input_window(command, "b.mov")
        self.assertEqual(second_seek, 0)
        self.assertAlmostEqual(second_read, 75+1/30, places=5)
        self.assertEqual(input_window(command, "master.wav"), (0.0, 180.0))
        self.assertIn("atrim=duration=180.000000", joined)
        self.assertEqual(float(command[-2]), 180.0)

    def test_aligned_export_applies_manual_final_file_range(self) -> None:
        media = [
            MediaInfo("a.mov", duration=100.0, width=3840, height=2160, fps=30, video_codec="hevc", bit_depth=10),
            MediaInfo("b.mov", duration=100.0, width=3840, height=2160, fps=30, video_codec="hevc", bit_depth=10),
        ]
        results = [AlignmentResult(0.0, 0.95, 50), AlignmentResult(-105.0, 0.93, 50)]
        settings = ExportSettings(codec="hevc_nvenc", resolution="original", bit_depth="10-bit", video_bitrate="80M")
        caps = Capabilities(nvenc_codecs={"hevc_nvenc"})
        command = build_gap_preserving_concat_encode_command(
            "ffmpeg", ["a.mov", "b.mov"], "master.wav", results, media,
            "concert.mov", settings, caps, master_audio_duration=180.0,
            trim_after_audio=True, custom_start=10.0, custom_end=190.0,
        )
        joined = " ".join(command)
        self.assertEqual(picture_span_frames(command), [90*30, 5*30, 85*30])
        self.assertEqual(input_window(command, "a.mov")[0], 10.0)
        self.assertAlmostEqual(input_window(command, "a.mov")[1], 90+1/30, places=5)
        self.assertEqual(input_window(command, "b.mov")[0], 0)
        self.assertAlmostEqual(input_window(command, "b.mov")[1], 85+1/30, places=5)
        self.assertEqual(input_window(command, "master.wav"), (10.0, 180.0))
        self.assertIn("atrim=duration=180.000000", joined)
        self.assertEqual(float(command[-2]), 180.0)

    def test_stream_separation_preserves_video_and_supports_lossless_audio(self) -> None:
        video = build_extract_video_command("ffmpeg", "input.mov", "video.mov")
        audio = build_extract_audio_command("ffmpeg", "input.mov", "audio.flac", "flac")
        self.assertEqual(video[video.index("-c:v") + 1], "copy")
        self.assertIn("-an", video)
        self.assertEqual(audio[audio.index("-c:a") + 1], "flac")
        self.assertIn("-vn", audio)
        self.assertEqual(video[video.index("-progress") + 1], "pipe:1")
        self.assertEqual(audio[audio.index("-progress") + 1], "pipe:1")

    def test_1080p_proxy_uses_cuda_and_h264_nvenc(self) -> None:
        media = MediaInfo(
            "input.mp4", width=7680, height=4320, fps=29.97,
            video_codec="hevc", bit_depth=10,
            color_primaries="bt2020", color_transfer="arib-std-b67", color_space="bt2020nc",
            timecode="23:33:31:15",
        )
        settings = make_1080p_proxy_settings(media, cuda_available=True)
        self.assertEqual(settings.codec, "h264_nvenc")
        self.assertEqual(settings.resolution, "1080p")
        self.assertEqual(settings.decode_mode, "cuda")
        self.assertEqual(settings.audio, "aac")
        self.assertIn("-color_primaries", settings.extra_args)
        self.assertEqual(settings.extra_args[settings.extra_args.index("-timecode") + 1], "23:33:31:15")
        caps = Capabilities(
            ffmpeg_path="ffmpeg", ffprobe_path="ffprobe", gpu_name="RTX 5090 Laptop GPU",
            nvenc_codecs={"h264_nvenc"}, cuda_decode=True, scale_cuda=True,
            raw_encoder_help={"h264_nvenc": "\n -tune value\n -multipass value\n"},
        )
        command = build_encode_command("ffmpeg", media.path, "proxy.mp4", settings, media, caps)
        joined = " ".join(command)
        self.assertIn("-hwaccel cuda", joined)
        self.assertIn("-hwaccel_output_format cuda", joined)
        self.assertIn("scale_cuda=-2:1080:format=nv12:passthrough=0", joined)
        self.assertIn("-c:v h264_nvenc", joined)

    def test_proxy_output_uses_resolve_compatible_basename_and_detects_collisions(self) -> None:
        output = proxy_output_path("D:/演唱会/C2745.MOV", "D:/演唱会/ProxyMedia")
        self.assertEqual(output.name, "C2745.mp4")
        collisions = duplicate_proxy_stems([
            "D:/机位A/C2745.MOV",
            "E:/机位B/c2745.mp4",
            "D:/机位A/C2746.MOV",
        ])
        self.assertEqual([name.casefold() for name in collisions], ["c2745"])

    def test_proxy_output_cannot_overwrite_mp4_source(self) -> None:
        with TemporaryDirectory() as folder:
            source = Path(folder) / "C2745.mp4"
            source.touch()
            self.assertEqual(proxy_source_conflicts([str(source)], folder), [str(source)])
            proxy_folder = Path(folder) / "ProxyMedia"
            self.assertEqual(proxy_source_conflicts([str(source)], proxy_folder), [])

    def test_positive_audio_offset_delays_external_audio(self) -> None:
        command = build_replace_audio_command("ffmpeg", "video.mov", "audio.wav", "out.mp4", 2.5)
        self.assertEqual(command[command.index("-itsoffset") + 1], "2.500000")
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertIn("apad", command)
        self.assertEqual(command[command.index("-progress") + 1], "pipe:1")

    def test_negative_audio_offset_trims_external_audio(self) -> None:
        command = build_replace_audio_command("ffmpeg", "video.mov", "audio.wav", "out.mkv", -1.25)
        self.assertEqual(command[command.index("-ss") + 1], "1.250000")
        self.assertNotIn("-itsoffset", command)

    def test_fft_alignment_offset_sign(self) -> None:
        try:
            import numpy as np
        except ImportError:
            self.skipTest("NumPy unavailable")
        rng = np.random.default_rng(42)
        event = rng.normal(size=1600)
        reference = event[:1000]
        candidate = event[125:1125]
        lag, score = _normalized_fft_correlation(reference, candidate, 50, 20)
        self.assertEqual(lag, 125)
        self.assertGreater(score, 0.95)

    def test_dependency_free_fft_alignment_offset_sign(self) -> None:
        rng = random.Random(42)
        event = [rng.gauss(0.0, 1.0) for _ in range(1600)]
        reference = event[:1000]
        candidate = event[125:1125]
        lag, score = _normalized_fft_correlation_python(reference, candidate, 20, 20)
        self.assertEqual(lag, 125)
        self.assertGreater(score, 0.95)


if __name__ == "__main__":
    unittest.main()
