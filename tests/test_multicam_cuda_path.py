"""GPU-resident camera normalization and frame-exact software fallbacks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exporter_core import (CREATE_NO_WINDOW, Capabilities, ExportSettings,
                           MediaInfo, detect_capabilities, find_binary)
from multicam_core import CameraClip, MulticamProject, render_multicam


class CudaCameraCommandTests(unittest.TestCase):
    def commands(self, *, source_size=(3840, 2160), target_size=None, depth=10,
                 target_depth="auto", scale_cuda=True, decode_mode="cuda",
                 pixel_format=None, codec="hevc", output_codec="hevc_nvenc",
                 cuda_decode=True, black_tail=False):
        with tempfile.TemporaryDirectory(prefix="multicam_cuda_args_") as folder:
            root = Path(folder)
            source = root / "camera.mp4"
            source.write_bytes(b"original camera")
            camera = MediaInfo(str(source), .5, *source_size, 60000 / 1001,
                               codec, pixel_format or ("yuv420p10le" if depth == 10 else "yuv420p"), depth)
            clips = [CameraClip("camera", 1, str(source), 0, .5)]
            media = {str(source): camera}
            if target_size:
                reference = root / "reference.mp4"
                reference.write_bytes(b"reference camera")
                clips.insert(0, CameraClip("reference", 2, str(reference), 0, .5))
                media[str(reference)] = MediaInfo(str(reference), .5, *target_size, 30,
                                                "hevc", "yuv420p10le", depth)
            project = MulticamProject(clips, fps=60000 / 1001,
                                      master_duration=.8 if black_tail else 0)
            settings = ExportSettings(codec=output_codec, audio="none", decode_mode=decode_mode,
                                      bit_depth=target_depth)
            caps = Capabilities(cuda_decode=cuda_decode, scale_cuda=scale_cuda)
            commands = []

            def run(command, callback):
                commands.append(command)
                Path(command[-1]).write_bytes(b"rendered")
                return 0, ""

            with patch("multicam_core.probe_media", side_effect=lambda _, path: media[path]):
                render_multicam(project, root / "result.mp4", "ffmpeg", "ffprobe", settings, caps, run)
            return commands

    def test_matching_ten_bit_camera_keeps_cuda_pixels_and_exact_frame_clock(self):
        chunk = self.commands()[0]
        self.assertEqual(chunk[chunk.index("-hwaccel_output_format") + 1], "cuda")
        graph = chunk[chunk.index("-vf") + 1]
        for software_filter in ("scale=", ",pad=", "format=", "hwdownload", "hwupload"):
            self.assertNotIn(software_filter, graph)
        self.assertIn("tpad=stop_mode=clone", graph)
        self.assertIn("trim=end_frame=30", graph)
        self.assertNotIn("-pix_fmt", chunk)
        self.assertEqual(chunk[chunk.index("-profile:v") + 1], "main10")
        self.assertEqual(chunk[chunk.index("-frames:v") + 1], "30")
        self.assertEqual(chunk[chunk.index("-r") + 1], "60000/1001")
        self.assertEqual(chunk[chunk.index("-fps_mode") + 1], "cfr")

    def test_same_aspect_camera_resize_uses_ten_bit_cuda_scaler(self):
        chunk = self.commands(source_size=(1920, 1080), target_size=(3840, 2160))[0]
        graph = chunk[chunk.index("-vf") + 1]
        self.assertIn("scale_cuda=3840:2160:format=p010le", graph)
        self.assertNotIn("hwdownload", graph)
        self.assertNotIn("-pix_fmt", chunk)

    def test_bit_depth_conversion_happens_on_cuda(self):
        chunk = self.commands(depth=8, target_depth="10-bit")[0]
        self.assertIn("scale_cuda=3840:2160:format=p010le", chunk[chunk.index("-vf") + 1])

    def test_ten_bit_letterbox_camera_keeps_software_fallback(self):
        chunk = self.commands(source_size=(1440, 1080), target_size=(3840, 2160))[0]
        graph = chunk[chunk.index("-vf") + 1]
        self.assertNotIn("-hwaccel_output_format", chunk)
        self.assertIn("pad=3840:2160", graph)
        self.assertIn("format=yuv420p10le", graph)
        self.assertEqual(chunk[chunk.index("-pix_fmt") + 1], "yuv420p10le")

    def test_missing_scaler_retains_fallback_only_when_conversion_needed(self):
        for source_size, target_size, fast in (((3840, 2160), None, True),
                                              ((1920, 1080), (3840, 2160), False)):
            with self.subTest(source_size=source_size):
                chunk = self.commands(source_size=source_size, target_size=target_size, scale_cuda=False)[0]
                self.assertEqual("-hwaccel_output_format" in chunk, fast)

    def test_unsupported_inputs_and_cpu_encoding_keep_software_fallback(self):
        for overrides in ({"codec": "prores"}, {"pixel_format": "yuv422p10le"},
                          {"decode_mode": "cpu"}, {"cuda_decode": False},
                          {"output_codec": "libx264"}):
            with self.subTest(overrides=overrides):
                chunk = self.commands(**overrides)[0]
                self.assertNotIn("-hwaccel_output_format", chunk)
                self.assertIn("-pix_fmt", chunk)

    def test_black_gap_keeps_ten_bit_normalization_and_frame_count(self):
        commands = self.commands(black_tail=True)
        camera, black = commands[:2]
        self.assertIn("-hwaccel_output_format", camera)
        self.assertIn("lavfi", black)
        self.assertNotIn("-hwaccel_output_format", black)
        self.assertEqual(black[black.index("-pix_fmt") + 1], "yuv420p10le")
        self.assertEqual(sum(int(chunk[chunk.index("-frames:v") + 1]) for chunk in commands[:-1]), 48)


@unittest.skipUnless(os.environ.get("OKK_TEST_NVENC") == "1", "set OKK_TEST_NVENC=1 for a short CUDA smoke")
class ActualCudaCameraTests(unittest.TestCase):
    def test_cuda_tpad_clone_keeps_short_source_ten_bit_and_black_gap(self):
        app = Path(__file__).resolve().parents[1]
        ffmpeg, ffprobe = find_binary("ffmpeg", app), find_binary("ffprobe", app)
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg unavailable")
        caps = detect_capabilities(app)
        if "hevc_nvenc" not in caps.nvenc_codecs or not caps.cuda_decode:
            self.skipTest("CUDA HEVC unavailable")

        def run(command):
            result = subprocess.run(command, capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace")[-4000:])
            return result.stdout

        def process(command, callback):
            result = subprocess.run(command, capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
            return result.returncode, result.stderr.decode("utf-8", "replace")

        with tempfile.TemporaryDirectory(prefix="multicam_cuda_real_") as folder:
            source, target = Path(folder) / "three_frames.mp4", Path(folder) / "programme.mp4"
            run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=320x180:r=30",
                 "-frames:v", "3", "-pix_fmt", "p010le", "-c:v", "hevc_nvenc", "-preset", "p2",
                 "-profile:v", "main10", "-bf", "0", str(source)])
            project = MulticamProject([CameraClip("one", 1, str(source), 0, .5)],
                                      master_duration=.8, fps=60000 / 1001)
            render_multicam(project, target, ffmpeg, ffprobe,
                            ExportSettings(codec="hevc_nvenc", decode_mode="cuda", audio="none"), caps, process)
            payload = json.loads(run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
                                      "-show_entries", "stream=nb_read_frames,pix_fmt,r_frame_rate", "-of", "json", str(target)]))
            stream = payload["streams"][0]
            self.assertEqual(int(stream["nb_read_frames"]), 48)
            self.assertEqual(stream["pix_fmt"], "yuv420p10le")
            self.assertEqual(stream["r_frame_rate"], "60000/1001")
            # The source ends after 3 frames; frame 25 must still be its cloned
            # red image, and frame 47 must be the separate timeline black gap.
            samples = []
            for frame in (25, 47):
                raw = run([ffmpeg, "-v", "error", "-i", str(target), "-vf",
                           f"select=eq(n\\,{frame}),scale=1:1", "-frames:v", "1",
                           "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
                self.assertEqual(len(raw), 3)
                samples.append(raw)
            self.assertGreater(samples[0][0], 150)
            self.assertLess(max(samples[1]), 10)


if __name__ == "__main__":
    unittest.main()
