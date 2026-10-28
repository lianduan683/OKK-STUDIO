from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exporter_core import (  # noqa: E402
    Capabilities,
    ExportSettings,
    MediaInfo,
    build_encode_command,
    detect_capabilities,
    parse_speed_multiplier,
    parse_fps_value,
    probe_media,
    progress_seconds,
    progress_fps_value,
    split_ranges,
)
from gpu_profiles import classify_gpu  # noqa: E402


def sample_media() -> MediaInfo:
    return MediaInfo(
        path="input.mp4", duration=120.0, width=7680, height=4320,
        fps=30.0, video_codec="hevc", pixel_format="yuv420p10le", bit_depth=10,
        audio_codec="aac", has_audio=True,
    )


def sample_caps(gpu_name: str = "RTX 5090") -> Capabilities:
    help_text = "\n -tune value\n -multipass value\n -spatial_aq value\n -temporal_aq value\n -split_encode_mode value\n"
    return Capabilities(
        ffmpeg_path="ffmpeg", ffprobe_path="ffprobe", gpu_name=gpu_name,
        gpu_profile=classify_gpu(gpu_name),
        nvenc_codecs={"hevc_nvenc", "av1_nvenc", "h264_nvenc"}, cuda_decode=True,
        scale_cuda=True, split_encode={"hevc_nvenc", "av1_nvenc"},
        raw_encoder_help={"hevc_nvenc": help_text, "av1_nvenc": help_text, "h264_nvenc": help_text},
    )


class CoreTests(unittest.TestCase):
    def test_cuda_hevc_command_keeps_frames_on_gpu(self) -> None:
        settings = ExportSettings(codec="hevc_nvenc", resolution="8k", bit_depth="10-bit", split_encode="3")
        cmd = build_encode_command("ffmpeg", "input.mp4", "output.mp4", settings, sample_media(), sample_caps())
        joined = " ".join(cmd)
        self.assertIn("-hwaccel cuda", joined)
        self.assertIn("-hwaccel_output_format cuda", joined)
        self.assertNotIn("scale_cuda", joined)
        self.assertIn("-c:v hevc_nvenc", joined)
        self.assertIn("-profile:v main10", joined)
        self.assertIn("-split_encode_mode 3", joined)

    def test_stale_three_engine_setting_is_safe_on_rtx_4060(self) -> None:
        settings = ExportSettings(codec="hevc_nvenc", split_encode="3")
        cmd = build_encode_command(
            "ffmpeg", "input.mp4", "output.mp4", settings, sample_media(), sample_caps("RTX 4060")
        )
        joined = " ".join(cmd)
        self.assertIn("-split_encode_mode disabled", joined)
        self.assertNotIn("-split_encode_mode 3", joined)

    def test_capability_detection_builds_laptop_profile_and_reads_vram(self) -> None:
        def fake_run(args, timeout=15):
            joined = " ".join(args)
            if "--query-gpu=" in joined:
                return 0, "NVIDIA GeForce RTX 5080 Laptop GPU, 600.12, 16376\n"
            if "-encoders" in args:
                return 0, "h264_nvenc hevc_nvenc av1_nvenc"
            if "encoder=" in joined:
                return 0, "\n -split_encode_mode value\n -tune value\n"
            if "-hwaccels" in args:
                return 0, "Hardware acceleration methods:\ncuda\n"
            if "-filters" in args:
                return 0, "scale_cuda"
            return 1, ""

        with patch("exporter_core.find_binary", side_effect=lambda name, app_dir=None: name), patch(
            "exporter_core._run_text", side_effect=fake_run
        ):
            caps = detect_capabilities("app")

        self.assertEqual(caps.gpu_memory_mb, 16376)
        self.assertEqual(caps.gpu_profile.form_factor, "Laptop")
        self.assertEqual(caps.gpu_profile.architecture, "Blackwell")
        self.assertEqual(caps.gpu_profile.nvenc_engines, 2)
        self.assertEqual(caps.nvenc_codecs, {"h264_nvenc", "hevc_nvenc", "av1_nvenc"})
        self.assertTrue(caps.cuda_decode)
        self.assertTrue(caps.scale_cuda)

    def test_rtx_20_and_30_do_not_expose_unsupported_av1_encoder(self) -> None:
        def fake_run(args, timeout=15):
            joined = " ".join(args)
            if "--query-gpu=" in joined:
                return 0, "NVIDIA GeForce RTX 3070 Laptop GPU, 600.12, 8192\n"
            if "-encoders" in args:
                return 0, "h264_nvenc hevc_nvenc av1_nvenc"
            if "encoder=" in joined:
                return 0, "\n -tune value\n"
            if "-hwaccels" in args:
                return 0, "Hardware acceleration methods:\ncuda\n"
            if "-filters" in args:
                return 0, "scale_cuda"
            return 1, ""

        with patch("exporter_core.find_binary", side_effect=lambda name, app_dir=None: name), patch(
            "exporter_core._run_text", side_effect=fake_run
        ):
            caps = detect_capabilities("app")

        self.assertEqual(caps.gpu_profile.architecture, "Ampere")
        self.assertTrue(caps.gpu_profile.in_supported_range)
        self.assertEqual(caps.nvenc_codecs, {"h264_nvenc", "hevc_nvenc"})
        self.assertNotIn("av1_nvenc", caps.raw_encoder_help)

    def test_h264_does_not_use_split_encode_or_main10(self) -> None:
        settings = ExportSettings(codec="h264_nvenc", bit_depth="auto", split_encode="forced")
        cmd = build_encode_command("ffmpeg", "in.mov", "out.mp4", settings, sample_media(), sample_caps())
        joined = " ".join(cmd)
        self.assertNotIn("split_encode_mode", joined)
        self.assertNotIn("main10", joined)
        self.assertIn("scale_cuda=format=nv12:passthrough=0", joined)

    def test_fast_presets_disable_quality_features_that_limit_throughput(self) -> None:
        settings = ExportSettings(codec="hevc_nvenc", preset="p2")
        cmd = build_encode_command("ffmpeg", "in.mov", "out.mp4", settings, sample_media(), sample_caps())
        joined = " ".join(cmd)
        self.assertNotIn("spatial_aq", joined)
        self.assertNotIn("temporal_aq", joined)
        self.assertIn("-bf 0", joined)

    def test_cpu_fallback_uploads_frames_for_nvenc(self) -> None:
        settings = ExportSettings(codec="hevc_nvenc", decode_mode="cpu", resolution="1080p")
        cmd = build_encode_command("ffmpeg", "in.mkv", "out.mkv", settings, sample_media(), sample_caps())
        joined = " ".join(cmd)
        self.assertNotIn("-hwaccel cuda", joined)
        self.assertIn("scale=-2:1080", joined)
        self.assertIn("format=p010le", joined)

    def test_split_ranges_cover_full_duration(self) -> None:
        ranges = split_ranges(100.0, 3)
        self.assertEqual(len(ranges), 3)
        self.assertEqual(ranges[0][0], 0)
        self.assertLess(abs(sum(duration for _, duration in ranges) - 100.0), 1e-8)
        self.assertLess(abs(ranges[-1][0] + ranges[-1][1] - 100.0), 1e-8)

    def test_progress_seconds_handles_ffmpeg_microseconds(self) -> None:
        self.assertEqual(progress_seconds({"out_time_us": "2500000"}), 2.5)

    def test_parallel_speed_text_can_be_summed(self) -> None:
        self.assertEqual(parse_speed_multiplier("3.12x"), 3.12)
        self.assertEqual(parse_speed_multiplier("N/A"), 0.0)
        self.assertEqual(parse_fps_value("69.94"), 69.94)
        self.assertEqual(parse_fps_value("N/A"), 0.0)
        self.assertEqual(progress_fps_value({"fps": "0.00", "speed": "1.50x"}, 60.0), 90.0)
        self.assertEqual(progress_fps_value({"fps": "72.25", "speed": "1.50x"}, 60.0), 72.25)

    def test_probe_media_reads_timecode_from_tmcd_stream(self) -> None:
        payload = {
            "streams": [
                {
                    "codec_type": "video", "codec_name": "hevc", "width": 7680, "height": 4320,
                    "avg_frame_rate": "30000/1001", "pix_fmt": "yuv420p10le",
                },
                {"codec_type": "data", "tags": {"timecode": "23:33:31:15"}},
            ],
            "format": {"duration": "60.0"},
        }
        with patch("exporter_core._run_text", return_value=(0, json.dumps(payload))) as run_text:
            media = probe_media("ffprobe", "input.mov")
        self.assertEqual(media.timecode, "23:33:31:15")
        arguments = run_text.call_args.args[0]
        self.assertIn("stream_tags=timecode", arguments[arguments.index("-show_entries") + 1])


if __name__ == "__main__":
    unittest.main()
