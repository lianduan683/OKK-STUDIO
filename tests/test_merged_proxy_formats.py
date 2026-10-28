from __future__ import annotations

import ast
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from editor_core import (
    DEFAULT_PROXY_FORMAT,
    PROXY_FORMATS,
    build_proxy_encode_command,
    duplicate_proxy_stems,
    make_1080p_proxy_settings,
    proxy_format_labels,
    proxy_formats,
    proxy_output_path,
    proxy_source_conflicts,
    resolve_proxy_format,
)
from exporter_core import Capabilities, MediaInfo, find_binary, probe_media


ROOT = Path(__file__).resolve().parents[1]


def _media(**overrides) -> MediaInfo:
    values = dict(
        path="D:/source/C2745.MOV",
        width=7680,
        height=4320,
        fps=29.97,
        video_codec="hevc",
        pixel_format="yuv420p10le",
        bit_depth=10,
        audio_codec="pcm_s24le",
        has_audio=True,
        color_primaries="bt2020",
        color_transfer="arib-std-b67",
        color_space="bt2020nc",
        timecode="23:33:31:15",
    )
    values.update(overrides)
    return MediaInfo(**values)


def _caps() -> Capabilities:
    return Capabilities(
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        gpu_name="RTX 5090 Laptop GPU",
        nvenc_codecs={"h264_nvenc"},
        cuda_decode=True,
        scale_cuda=True,
        raw_encoder_help={"h264_nvenc": "\n -tune value\n -multipass value\n"},
    )


def _proxy_command(proxy_format: str, media: MediaInfo | None = None) -> tuple[list[str], MediaInfo]:
    media = media or _media()
    output = proxy_output_path(media.path, "D:/proxy", proxy_format)
    command = build_proxy_encode_command(
        "ffmpeg", media.path, str(output), media, _caps(), proxy_format,
    )
    return command, media


def _filter_chain(command: list[str]) -> str:
    return command[command.index("-vf") + 1]


def _method(tree: ast.Module, class_name: str, method_name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name:
                    return item
    raise AssertionError(f"缺少 {class_name}.{method_name}")


def _calls(method: ast.AST, function_name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == function_name
    ]


def _is_self_attribute(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr == name
    )


class ProxyFormatCatalogTests(unittest.TestCase):
    def test_default_format_is_still_h264_nvenc_mp4(self) -> None:
        self.assertEqual(DEFAULT_PROXY_FORMAT, "h264_nvenc")
        self.assertEqual(proxy_formats()[0].key, DEFAULT_PROXY_FORMAT)

        legacy = make_1080p_proxy_settings(_media(), True)
        explicit = make_1080p_proxy_settings(_media(), True, "h264_nvenc")

        for settings in (legacy, explicit):
            self.assertEqual(settings.codec, "h264_nvenc")
            self.assertEqual(settings.resolution, "1080p")
            self.assertEqual(settings.decode_mode, "cuda")
            self.assertEqual(settings.audio, "aac")
            self.assertEqual(settings.audio_bitrate, "320k")
            self.assertEqual(settings.pixel_format, "nv12")
        self.assertEqual(legacy, explicit)
        self.assertEqual(
            proxy_output_path("D:/source/C2745.MOV", "D:/proxy").name,
            "C2745.mp4",
        )

    def test_each_dnxhr_profile_targets_mov_with_expected_depth(self) -> None:
        expected = {
            "dnxhr_lb": ("dnxhr_lb", 8, "yuv422p"),
            "dnxhr_sq": ("dnxhr_sq", 8, "yuv422p"),
            "dnxhr_hq": ("dnxhr_hq", 8, "yuv422p"),
            "dnxhr_hqx": ("dnxhr_hqx", 10, "yuv422p10le"),
        }
        for key, (profile, depth, pixel_format) in expected.items():
            with self.subTest(proxy_format=key):
                fmt = resolve_proxy_format(key)
                self.assertEqual(fmt.codec, "dnxhd")
                self.assertEqual(fmt.profile, profile)
                self.assertEqual(fmt.extension, ".mov")
                self.assertEqual(fmt.bit_depth, depth)
                self.assertEqual(fmt.pixel_format, pixel_format)
                self.assertFalse(fmt.uses_nvenc)
                self.assertEqual(fmt.audio, "pcm_s24le")

                settings = make_1080p_proxy_settings(_media(), True, key)
                self.assertEqual(settings.codec, "dnxhd")
                self.assertEqual(settings.profile, profile)
                self.assertEqual(settings.pixel_format, pixel_format)
                self.assertEqual(settings.bit_depth, "10-bit" if depth == 10 else "8-bit")
                self.assertEqual(settings.audio, "pcm_s24le")
                self.assertEqual(settings.decode_mode, "cpu")
                self.assertEqual(settings.resolution, "1080p")
                self.assertEqual(
                    proxy_output_path("D:/source/C2745.MOV", "D:/proxy", key).suffix.lower(),
                    ".mov",
                )

    def test_prores_profiles_target_mov_10bit_422(self) -> None:
        expected = {
            "prores_proxy": "proxy",
            "prores_lt": "lt",
            "prores_hq": "hq",
        }
        for key, profile in expected.items():
            with self.subTest(proxy_format=key):
                fmt = resolve_proxy_format(key)
                self.assertEqual(fmt.codec, "prores_ks")
                self.assertEqual(fmt.profile, profile)
                self.assertEqual(fmt.extension, ".mov")
                self.assertEqual(fmt.bit_depth, 10)
                self.assertEqual(fmt.pixel_format, "yuv422p10le")
                self.assertEqual(fmt.audio, "pcm_s24le")

                settings = make_1080p_proxy_settings(_media(), True, key)
                self.assertEqual(settings.codec, "prores_ks")
                self.assertEqual(settings.profile, profile)
                self.assertEqual(settings.pixel_format, "yuv422p10le")
                self.assertEqual(settings.bit_depth, "10-bit")
                self.assertEqual(settings.audio, "pcm_s24le")
                self.assertEqual(settings.decode_mode, "cpu")
                self.assertEqual(
                    proxy_output_path("D:/source/C2745.MOV", "D:/proxy", key).name,
                    "C2745.mov",
                )

    def test_format_keys_and_ui_labels_resolve(self) -> None:
        for fmt in proxy_formats():
            with self.subTest(proxy_format=fmt.key):
                self.assertEqual(resolve_proxy_format(fmt.key), fmt)
                self.assertEqual(resolve_proxy_format(fmt.label), fmt)
        for alias, key in (
            ("h264", "h264_nvenc"),
            ("lb", "dnxhr_lb"),
            ("sq", "dnxhr_sq"),
            ("hqx", "dnxhr_hqx"),
            ("prores", "prores_proxy"),
            ("prores_422_hq", "prores_hq"),
        ):
            with self.subTest(alias=alias):
                self.assertEqual(resolve_proxy_format(alias).key, key)
        with self.assertRaises(ValueError):
            resolve_proxy_format("不存在的代理格式")

    def test_hyphenated_dnxhr_aliases_resolve(self) -> None:
        # _PROXY_FORMAT_ALIASES advertises these hyphenated spellings.
        for alias, key in (
            ("dnxhr-lb", "dnxhr_lb"),
            ("dnxhr-sq", "dnxhr_sq"),
            ("dnxhr-hq", "dnxhr_hq"),
            ("dnxhr-hqx", "dnxhr_hqx"),
        ):
            with self.subTest(alias=alias):
                self.assertEqual(resolve_proxy_format(alias).key, key)

    def test_format_labels_are_unique_and_visible(self) -> None:
        labels = proxy_format_labels()
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(labels, [fmt.label for fmt in PROXY_FORMATS])
        self.assertIn("H.264", labels[0])


class ProxyCommandTests(unittest.TestCase):
    def test_software_proxy_commands_never_use_nvenc_presets_or_cuda_decode(self) -> None:
        for key in ("dnxhr_lb", "dnxhr_sq", "dnxhr_hq", "dnxhr_hqx", "prores_proxy", "prores_lt", "prores_hq"):
            with self.subTest(proxy_format=key):
                fmt = resolve_proxy_format(key)
                command, _media_info = _proxy_command(key)
                joined = " ".join(command)
                self.assertIn(f"-c:v {fmt.codec}", joined)
                self.assertIn(f"-profile:v {fmt.profile}", joined)
                self.assertNotIn("nvenc", joined.casefold())
                self.assertNotIn("cuda", joined.casefold())
                self.assertNotIn("-hwaccel", command)
                self.assertNotIn("-preset", command)
                self.assertNotIn("-rc", command)
                self.assertNotIn("-cq", command)
                self.assertNotIn("-multipass", command)
                self.assertNotIn("-split_encode_mode", command)

    def test_pixel_format_matches_selected_bit_depth(self) -> None:
        for key in ("dnxhr_lb", "dnxhr_sq", "dnxhr_hq"):
            with self.subTest(proxy_format=key):
                chain = _filter_chain(_proxy_command(key)[0])
                self.assertTrue(chain.endswith("format=yuv422p"), chain)
                self.assertNotIn("yuv422p10le", chain)
        for key in ("dnxhr_hqx", "prores_proxy", "prores_lt", "prores_hq"):
            with self.subTest(proxy_format=key):
                chain = _filter_chain(_proxy_command(key)[0])
                self.assertTrue(chain.endswith("format=yuv422p10le"), chain)

    def test_proxy_scaling_is_1080p_and_keeps_even_aspect_width(self) -> None:
        widescreen = _media(width=7680, height=4320)
        chain = _filter_chain(_proxy_command("dnxhr_hq", widescreen)[0])
        self.assertIn("scale=1920:1080:flags=lanczos", chain)
        self.assertIn("setsar=1", chain)
        self.assertNotIn("scale_cuda", chain)

        four_three = _media(width=2880, height=2160)
        chain = _filter_chain(_proxy_command("dnxhr_hq", four_three)[0])
        self.assertIn("scale=1440:1080:flags=lanczos", chain)

        ultra_wide = _media(width=3840, height=1600)
        chain = _filter_chain(_proxy_command("prores_hq", ultra_wide)[0])
        self.assertIn("scale=2592:1080:flags=lanczos", chain)

        settings = make_1080p_proxy_settings(four_three, True, "dnxhr_hq")
        self.assertEqual(settings.resolution, "1080p")

    def test_color_metadata_and_timecode_survive_every_proxy_format(self) -> None:
        for fmt in proxy_formats():
            with self.subTest(proxy_format=fmt.key):
                command, _media_info = _proxy_command(fmt.key)
                joined = " ".join(command)
                self.assertIn("-color_primaries bt2020", joined)
                self.assertIn("-color_trc arib-std-b67", joined)
                self.assertIn("-colorspace bt2020nc", joined)
                self.assertEqual(command[command.index("-timecode") + 1], "23:33:31:15")

    def test_missing_timecode_does_not_emit_a_fake_tag(self) -> None:
        media = _media(timecode=None)
        for key in ("h264_nvenc", "dnxhr_hqx", "prores_hq"):
            with self.subTest(proxy_format=key):
                command, _media_info = _proxy_command(key, media)
                self.assertNotIn("-timecode", command)

    def test_audio_matches_the_selected_container(self) -> None:
        for key in ("dnxhr_lb", "dnxhr_sq", "dnxhr_hq", "dnxhr_hqx", "prores_proxy", "prores_lt", "prores_hq"):
            with self.subTest(proxy_format=key):
                command, _media_info = _proxy_command(key)
                joined = " ".join(command)
                self.assertIn("-c:a pcm_s24le", joined)
                self.assertNotIn("-c:a aac", joined)
                self.assertNotIn("-b:a", command)

        command, _media_info = _proxy_command("h264_nvenc")
        joined = " ".join(command)
        self.assertIn("-c:a aac -b:a 320k", joined)
        self.assertNotIn("pcm_s24le", joined)

    def test_default_h264_proxy_keeps_the_original_nvenc_pipeline(self) -> None:
        command, _media_info = _proxy_command("h264_nvenc")
        joined = " ".join(command)
        self.assertIn("-hwaccel cuda", joined)
        self.assertIn("-hwaccel_output_format cuda", joined)
        self.assertIn("scale_cuda=-2:1080:format=nv12:passthrough=0", joined)
        self.assertIn("-c:v h264_nvenc", joined)
        self.assertIn("-preset p2", joined)
        self.assertIn("-tune hq", joined)
        self.assertIn("-rc vbr -cq 19 -b:v 0", joined)
        self.assertIn("-multipass disabled", joined)
        self.assertIn("-bf 0", joined)
        self.assertEqual(Path(command[-1]).name, "C2745.mp4")

    def test_default_h264_proxy_falls_back_to_cpu_decode(self) -> None:
        media = _media()
        output = proxy_output_path(media.path, "D:/proxy", "h264_nvenc")
        settings = make_1080p_proxy_settings(media, False, "h264_nvenc")
        command = build_proxy_encode_command(
            "ffmpeg", media.path, str(output), media, _caps(), "h264_nvenc",
            cuda_available=False,
        )
        self.assertEqual(settings.decode_mode, "cpu")
        self.assertNotIn("-hwaccel", command)
        self.assertIn("scale=-2:1080,format=nv12", _filter_chain(command))
        self.assertIn("-c:v h264_nvenc", " ".join(command))

    def test_build_proxy_encode_command_refuses_to_overwrite_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "C2745.mov"
            source.touch()
            media = _media(path=str(source), width=1920, height=1080)
            with self.assertRaisesRegex(ValueError, "不能覆盖原片"):
                build_proxy_encode_command(
                    "ffmpeg", str(source), str(source), media, _caps(), "dnxhr_hq",
                )


class ProxyNamingAndConflictTests(unittest.TestCase):
    def test_proxy_extension_follows_selected_format(self) -> None:
        source = "D:/source/C2745.MOV"
        self.assertEqual(proxy_output_path(source, "D:/proxy", "h264_nvenc").name, "C2745.mp4")
        for key in ("dnxhr_lb", "dnxhr_sq", "dnxhr_hq", "dnxhr_hqx", "prores_proxy", "prores_lt", "prores_hq"):
            with self.subTest(proxy_format=key):
                self.assertEqual(proxy_output_path(source, "D:/proxy", key).name, "C2745.mov")
        self.assertEqual(
            proxy_output_path("D:/source/C2745.raw.MOV", "D:/proxy", "prores_hq").name,
            "C2745.raw.mov",
        )

    def test_duplicate_stems_are_detected_case_insensitively(self) -> None:
        collisions = duplicate_proxy_stems([
            "D:/机位A/C2745.MOV",
            "E:/机位B/c2745.mp4",
            "D:/机位A/C2746.MOV",
        ])
        self.assertEqual([name.casefold() for name in collisions], ["c2745"])

    def test_dnxhr_and_prores_refuse_to_overwrite_a_mov_source(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "C2745.mov"
            source.touch()
            for key in ("dnxhr_lb", "dnxhr_sq", "dnxhr_hq", "dnxhr_hqx", "prores_proxy", "prores_lt", "prores_hq"):
                with self.subTest(proxy_format=key):
                    self.assertEqual(
                        proxy_source_conflicts([str(source)], folder, key),
                        [str(source)],
                    )

    def test_source_conflict_is_container_specific(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            mov_source = Path(folder) / "C2745.mov"
            mp4_source = Path(folder) / "C2746.mp4"
            mov_source.touch()
            mp4_source.touch()

            self.assertEqual(
                proxy_source_conflicts([str(mov_source)], folder, "dnxhr_hq"),
                [str(mov_source)],
            )
            self.assertEqual(
                proxy_source_conflicts([str(mp4_source)], folder, "h264_nvenc"),
                [str(mp4_source)],
            )
            self.assertEqual(
                proxy_source_conflicts([str(mov_source)], folder, "h264_nvenc"),
                [],
            )
            self.assertEqual(
                proxy_source_conflicts([str(mp4_source)], folder, "prores_hq"),
                [],
            )

    def test_proxy_subfolder_never_conflicts_with_a_source(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "C2745.mov"
            source.touch()
            self.assertEqual(
                proxy_source_conflicts([str(source)], Path(folder) / "ProxyMedia", "dnxhr_hq"),
                [],
            )

    def test_conflicts_cover_every_source_not_only_the_same_named_one(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / "cameraA" / "C2746.mov"
            first.parent.mkdir()
            second = Path(folder) / "C2746.mov"
            second.touch()
            conflicts = proxy_source_conflicts([str(first), str(second)], folder, "dnxhr_hq")
            self.assertIn(str(first), conflicts)
            self.assertIn(str(second), conflicts)


class RealProxyEncodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ffmpeg = find_binary("ffmpeg", ROOT)
        cls.ffprobe = find_binary("ffprobe", ROOT)
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest("FFmpeg/ffprobe unavailable")
        encoders = subprocess.run(
            [cls.ffmpeg, "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        ).stdout.decode("utf-8", "replace")
        if "dnxhd" not in encoders or "prores_ks" not in encoders:
            raise unittest.SkipTest("FFmpeg build lacks dnxhd/prores_ks")

        cls.temp = tempfile.TemporaryDirectory(prefix="okk_proxy_formats_")
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "C2745.MOV"
        creation = subprocess.run(
            [
                cls.ffmpeg, "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=0.4",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.4",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                str(cls.source),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        if creation.returncode:
            cls.temp.cleanup()
            raise unittest.SkipTest("libx264 unavailable for proxy source generation")

        cls.media = probe_media(cls.ffprobe, str(cls.source))
        cls.media.timecode = "01:02:03:04"
        cls.caps = Capabilities(ffmpeg_path=cls.ffmpeg, ffprobe_path=cls.ffprobe)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    def _encode(self, key: str) -> tuple[Path, dict]:
        fmt = resolve_proxy_format(key)
        output = self.root / f"proxy_{key}{fmt.extension}"
        command = build_proxy_encode_command(
            self.ffmpeg, str(self.source), str(output), self.media, self.caps, key,
        )
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace")[-4000:])
        self.assertTrue(output.is_file())
        self.assertGreater(output.stat().st_size, 0)
        probe = subprocess.run(
            [
                self.ffprobe, "-v", "error",
                "-show_streams", "-show_format", "-of", "json", str(output),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        self.assertEqual(probe.returncode, 0)
        return output, json.loads(probe.stdout.decode("utf-8", "replace"))

    def test_each_dnxhr_profile_produces_1080p_mov_with_depth_timecode_and_pcm(self) -> None:
        expected = {
            "dnxhr_lb": ("DNXHR LB", "yuv422p"),
            "dnxhr_sq": ("DNXHR SQ", "yuv422p"),
            "dnxhr_hq": ("DNXHR HQ", "yuv422p"),
            "dnxhr_hqx": ("DNXHR HQX", "yuv422p10le"),
        }
        for key, (profile, pixel_format) in expected.items():
            with self.subTest(proxy_format=key):
                output, payload = self._encode(key)
                self.assertEqual(output.suffix.lower(), ".mov")
                video = next(
                    stream for stream in payload["streams"] if stream.get("codec_type") == "video"
                )
                self.assertEqual(video["codec_name"], "dnxhd")
                self.assertEqual(video["profile"].upper(), profile)
                self.assertEqual(video["pix_fmt"], pixel_format)
                self.assertEqual((video["width"], video["height"]), (1920, 1080))
                self.assertEqual(video["tags"]["timecode"], "01:02:03:04")
                audio = next(
                    stream for stream in payload["streams"] if stream.get("codec_type") == "audio"
                )
                self.assertEqual(audio["codec_name"], "pcm_s24le")

    def test_prores_profiles_produce_1080p_mov_10bit_with_pcm(self) -> None:
        expected = {
            "prores_proxy": "PROXY",
            "prores_lt": "LT",
            "prores_hq": "HQ",
        }
        for key, profile in expected.items():
            with self.subTest(proxy_format=key):
                output, payload = self._encode(key)
                self.assertEqual(output.suffix.lower(), ".mov")
                video = next(
                    stream for stream in payload["streams"] if stream.get("codec_type") == "video"
                )
                self.assertEqual(video["codec_name"], "prores")
                self.assertEqual(video["profile"].upper(), profile)
                self.assertEqual(video["pix_fmt"], "yuv422p10le")
                self.assertEqual((video["width"], video["height"]), (1920, 1080))
                self.assertEqual(video["tags"]["timecode"], "01:02:03:04")
                audio = next(
                    stream for stream in payload["streams"] if stream.get("codec_type") == "audio"
                )
                self.assertEqual(audio["codec_name"], "pcm_s24le")


if __name__ == "__main__":
    unittest.main()
