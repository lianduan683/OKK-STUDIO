import argparse
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from okk_cli import _safe_delete_resolve_intermediate, build_parser, settings_from_args  # noqa: E402


class OkkCliTests(unittest.TestCase):
    def _args(self, *extra: str) -> argparse.Namespace:
        return build_parser().parse_args([
            "--headless-export", "--input", "master.mov", "--output", "final.mov", *extra,
        ])

    def test_resolve_dnxhr_intermediate_uses_cpu_decode_and_nvenc_encode(self) -> None:
        args = self._args("--codec", "hevc_nvenc", "--bit-depth", "10-bit", "--video-bitrate", "80M")
        settings = settings_from_args(args, media_codec="dnxhd", cuda_available=True, split_value="3")
        self.assertEqual(settings.decode_mode, "cpu")
        self.assertEqual(settings.codec, "hevc_nvenc")
        self.assertEqual(settings.bit_depth, "10-bit")
        self.assertEqual(settings.video_bitrate, "80M")
        self.assertEqual(settings.split_encode, "3")

    def test_nvdec_compatible_source_uses_cuda_in_auto_mode(self) -> None:
        args = self._args("--decode", "auto")
        settings = settings_from_args(args, media_codec="hevc", cuda_available=True, split_value="auto")
        self.assertEqual(settings.decode_mode, "cuda")

    def test_bare_connector_bitrates_are_kbps(self) -> None:
        args = self._args("--video-bitrate", "80000", "--maxrate", "120000", "--audio", "aac", "--audio-bitrate", "320")
        settings = settings_from_args(args, media_codec="dnxhd", cuda_available=True, split_value="disabled")
        self.assertEqual(settings.video_bitrate, "80000k")
        self.assertEqual(settings.maxrate, "120000k")
        self.assertEqual(settings.audio_bitrate, "320k")

    def test_intermediate_delete_requires_connector_marker_and_folder_name(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            unsafe = Path(root) / "camera.mov"
            unsafe.write_bytes(b"video")
            self.assertFalse(_safe_delete_resolve_intermediate(unsafe))
            self.assertTrue(unsafe.exists())

            safe_folder = Path(root) / "OKK_Resolve_test"
            safe_folder.mkdir()
            safe = safe_folder / "timeline.mov"
            safe.write_bytes(b"video")
            (safe_folder / ".okk_resolve_temp").write_text("marker", encoding="utf-8")
            self.assertTrue(_safe_delete_resolve_intermediate(safe))
            self.assertFalse(safe.exists())


if __name__ == "__main__":
    unittest.main()
