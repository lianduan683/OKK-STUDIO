"""NVENC frame-rate regression; actual GPU smoke test is opt-in.

Run the hardware test with OKK_TEST_NVENC=1 on an NVIDIA workstation.
"""
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
from multicam_core import CameraClip, Cut, MulticamProject, render_multicam


class NvencFrameRateTests(unittest.TestCase):
    def test_all_nvenc_chunk_commands_use_cfr_with_the_project_rate(self):
        """Catch the -r plus fps_mode=passthrough failure without GPU hardware."""
        with tempfile.TemporaryDirectory(prefix="multicam_nvenc_args_") as folder:
            root = Path(folder)
            source = root / "camera.mp4"
            source.write_bytes(b"mock media")
            project = MulticamProject([CameraClip("one", 1, str(source), 0, 1)], fps=30000 / 1001)
            reference = MediaInfo(str(source), 1, 320, 180, 25, "h264", "yuv420p", 8)
            for codec in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
                commands = []

                def process(command, callback=None):
                    commands.append(command)
                    Path(command[-1]).write_bytes(b"encoded result")
                    return 0, ""

                with self.subTest(codec=codec), patch("multicam_core.probe_media", return_value=reference):
                    render_multicam(project, root / f"{codec}.mp4", "ffmpeg", "ffprobe",
                                    ExportSettings(codec=codec, audio="none"), Capabilities(), process)
                    chunk = commands[0]
                    self.assertEqual(chunk[chunk.index("-fps_mode") + 1], "cfr")
                    self.assertNotIn("passthrough", chunk)
                    self.assertEqual(chunk[chunk.index("-r") + 1], "30000/1001")
                    self.assertEqual(chunk[chunk.index("-video_track_timescale") + 1], "30000")


@unittest.skipUnless(os.environ.get("OKK_TEST_NVENC") == "1", "set OKK_TEST_NVENC=1 for real RTX validation")
class ActualNvencTests(unittest.TestCase):
    def test_mixed_camera_chunks_encode_on_nvenc_and_keep_all_frames(self):
        app = Path(__file__).resolve().parents[1]
        ffmpeg, ffprobe = find_binary("ffmpeg", app), find_binary("ffprobe", app)
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg unavailable")
        caps = detect_capabilities(app)
        if not caps.nvenc_codecs or "not detected" in caps.gpu_name:
            self.skipTest("NVIDIA GPU unavailable")

        def run(command):
            result = subprocess.run(command, capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace")[-4000:])
            return result.stdout

        def process(command, callback=None):
            result = subprocess.run(command, capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
            return result.returncode, result.stderr.decode("utf-8", "replace")

        with tempfile.TemporaryDirectory(prefix="multicam_nvenc_real_") as folder:
            root = Path(folder)
            red, blue = root / "red.mp4", root / "blue.mp4"
            for source, color, size, fps in ((red, "red", "320x180", 25), (blue, "blue", "192x128", 30)):
                run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", f"color={color}:s={size}:r={fps}:d=1",
                     "-c:v", "libx264", "-preset", "ultrafast", str(source)])
            project = MulticamProject([CameraClip("r", 1, str(red), 0, 1), CameraClip("b", 2, str(blue), 0, 1)],
                                      cuts=[Cut(0, 1), Cut(0.5, 2)], fps=30)
            for codec in sorted(caps.nvenc_codecs & {"h264_nvenc", "hevc_nvenc", "av1_nvenc"}):
                with self.subTest(codec=codec):
                    target = root / f"{codec}.mp4"
                    render_multicam(project, target, ffmpeg, ffprobe,
                                    ExportSettings(codec=codec, preset="p2", decode_mode="cuda", audio="none"), caps, process)
                    payload = json.loads(run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
                                              "-show_entries", "stream=nb_read_frames,r_frame_rate,width,height", "-of", "json", str(target)]))
                    video = payload["streams"][0]
                    # Some bundled FFprobe builds have no software AV1
                    # decoder. Count actual frames through CUDA as well, so
                    # all three codecs are verified on the real GPU.
                    decoded = run([ffmpeg, "-v", "error", "-hwaccel", "cuda", "-i", str(target),
                                   "-map", "0:v:0", "-an", "-fps_mode", "passthrough",
                                   "-progress", "pipe:1", "-nostats", "-f", "null", "-"]).decode("utf-8", "replace")
                    counts = [int(line.split("=", 1)[1]) for line in decoded.splitlines() if line.startswith("frame=")]
                    self.assertTrue(counts, decoded)
                    self.assertEqual(counts[-1], 30)
                    if "nb_read_frames" in video:
                        self.assertEqual(int(video["nb_read_frames"]), 30)
                    self.assertEqual(video["r_frame_rate"], "30/1")
                    self.assertEqual((video["width"], video["height"]), (320, 180))


if __name__ == "__main__":
    unittest.main()
