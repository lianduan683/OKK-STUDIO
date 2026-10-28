"""Regression tests for timeline semantics; CPU encoding substitutes for NVENC.

Run: py -3 -m unittest discover -s tests -p "test_*.py" -v
Real FFmpeg tests require ffmpeg/ffprobe on PATH or in the application's bin.
GPU command tests inspect the CUDA route; they do not benchmark an RTX GPU.
"""
from __future__ import annotations

import array
import ast
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from editor_core import (
    AlignmentResult, build_gap_preserving_concat_encode_command,
    build_join_timeline_model,
    fit_aspect_size,
    format_timeline_ruler_time,
    build_join_mux_command, build_join_video_chunk_command,
    join_uses_cuda_frames, make_join_export_plan, partition_join_video_spans,
    plan_join_video_spans, resolve_join_output_range,
    timeline_frame_zoom_multiplier, write_concat_manifest,
)
from exporter_core import Capabilities, ExportSettings, MediaInfo, RollingProgressRate, find_binary, probe_media
from gpu_profiles import classify_gpu
from rtx5090_exporter import ExporterApp, TIMELINE_MAJOR_TICK_PIXELS, TIMELINE_RULER_BOTTOM


class TkUiSourceTests(unittest.TestCase):
    def test_tk_widget_constructor_padding_is_not_a_tuple(self):
        """Classic Tk widget options accept one screen distance, unlike pack/grid."""
        source_path = Path(__file__).resolve().parents[1] / "rtx5090_exporter.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        invalid: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "tk"
            ):
                continue
            for keyword in node.keywords:
                if keyword.arg in {"padx", "pady"} and isinstance(keyword.value, (ast.Tuple, ast.List)):
                    invalid.append(f"line {node.lineno}: tk.{function.attr} {keyword.arg}")
        self.assertEqual(invalid, [], "Tk 构造参数不能使用成对距离：" + ", ".join(invalid))


class TimelineUiInteractionTests(unittest.TestCase):
    class Canvas:
        def __init__(self):
            self.focused = False
            self.mark = None
            self.drag = None
            self.cursor = "hand2"

        def focus_set(self):
            self.focused = True

        def scan_mark(self, x, y):
            self.mark = (x, y)

        def scan_dragto(self, x, y, gain=10):
            self.drag = (x, y, gain)

        def configure(self, **options):
            self.cursor = options.get("cursor", self.cursor)

    @staticmethod
    def make_app():
        app = ExporterApp.__new__(ExporterApp)
        app.editor_join_timeline_canvas = TimelineUiInteractionTests.Canvas()
        app.editor_join_timeline_drag = None
        app.editor_join_timeline_pan = None
        app._join_timeline_master_at = lambda _event: 125.0
        app.ruler_redraws = 0
        app._draw_join_timeline_ruler = lambda: setattr(
            app, "ruler_redraws", app.ruler_redraws + 1,
        )
        app.playhead_updates = []
        app._update_join_timeline_playhead = app.playhead_updates.append
        return app

    def test_time_labels_are_short_and_spaced_for_high_dpi(self):
        self.assertEqual(format_timeline_ruler_time(200.0, 200.0), "00:03:20")
        self.assertEqual(format_timeline_ruler_time(1.2, 0.1), "00:00:01.2")
        step = ExporterApp._join_timeline_tick_step(TIMELINE_MAJOR_TICK_PIXELS / 0.55)
        self.assertGreaterEqual(step * 0.55, TIMELINE_MAJOR_TICK_PIXELS)

    def test_dragging_time_ruler_pans_without_moving_clip_or_playhead(self):
        app = self.make_app()
        press = SimpleNamespace(x=100, y=TIMELINE_RULER_BOTTOM - 10)
        motion = SimpleNamespace(x=160, y=TIMELINE_RULER_BOTTOM - 10)
        ExporterApp._join_timeline_press(app, press)
        ExporterApp._join_timeline_motion(app, motion)
        ExporterApp._join_timeline_release(app, motion)
        self.assertEqual(app.editor_join_timeline_canvas.mark, (100, 36))
        self.assertEqual(app.editor_join_timeline_canvas.drag, (160, 36, 1))
        self.assertEqual(app.editor_join_timeline_canvas.cursor, "hand2")
        self.assertEqual(app.playhead_updates, [])
        self.assertGreaterEqual(app.ruler_redraws, 2)

    def test_clicking_time_ruler_still_positions_playhead(self):
        app = self.make_app()
        event = SimpleNamespace(x=100, y=TIMELINE_RULER_BOTTOM - 10)
        ExporterApp._join_timeline_press(app, event)
        ExporterApp._join_timeline_release(app, event)
        self.assertEqual(app.playhead_updates, [125.0])


class RateTests(unittest.TestCase):
    def test_startup_wait_is_excluded_and_fps_matches_speed(self):
        now = [0.0]
        rate = RollingProgressRate(clock=lambda: now[0])
        self.assertEqual(rate.update({"out_time_us": "0", "frame": "0"}, 30), (0.0, 0.0))
        now[0] = 100.0
        self.assertEqual(rate.update({"out_time_us": "100000", "frame": "3"}, 30), (0.0, 0.0))
        now[0] = 101.0
        fps, speed = rate.update({"out_time_us": "2100000", "frame": "63"}, 30)
        self.assertEqual((fps, speed), (60.0, 2.0))
        for step in range(1, 5):
            now[0] = 101.0 + step
            fps, speed = rate.update({"out_time_us": "2100000", "frame": "63"}, 30)
        self.assertEqual((fps, speed), (0.0, 0.0))

    def test_audio_only_and_counter_reset(self):
        now = [1.0]
        rate = RollingProgressRate(clock=lambda: now[0])
        rate.update({"out_time_us": "1000000"})
        now[0] = 2.0
        self.assertEqual(rate.update({"out_time_us": "5000000"}), (0.0, 4.0))
        now[0] = 3.0
        self.assertEqual(rate.update({"out_time_us": "0"}), (0.0, 0.0))


class GpuProfileTests(unittest.TestCase):
    def test_blackwell_multi_decoder_profiles_are_conservative(self):
        laptop = classify_gpu("NVIDIA GeForce RTX 5090 Laptop GPU")
        self.assertEqual((laptop.nvenc_engines, laptop.nvdec_engines), (3, 2))
        self.assertEqual(classify_gpu("NVIDIA GeForce RTX 5080").nvdec_engines, 2)
        self.assertEqual(classify_gpu("NVIDIA GeForce RTX 5070 Ti").nvdec_engines, 1)
        self.assertEqual(classify_gpu("NVIDIA GeForce RTX 4090").nvdec_engines, 1)


class TimelineTests(unittest.TestCase):
    def test_preview_fits_source_aspect_ratio_and_scale(self):
        width, height = fit_aspect_size(580, 384, 8192, 4320, scale=0.75)
        self.assertLessEqual(width, round(580 * 0.75))
        self.assertLessEqual(height, round(384 * 0.75))
        self.assertAlmostEqual(width / height, 8192 / 4320, places=2)

    def test_frame_zoom_makes_each_ntsc_frame_twelve_pixels_wide(self):
        multiplier = timeline_frame_zoom_multiplier(0.125, 30000 / 1001)
        pixels_per_second = 0.125 * multiplier
        self.assertAlmostEqual(pixels_per_second / (30000 / 1001), 12.0)

    def test_visual_timeline_uses_alignment_offsets_and_finds_black_gap(self):
        paths = ["camera-a.mov", "camera-b.mov"]
        model = build_join_timeline_model(
            paths, [10.0, 5.0],
            {
                paths[0]: AlignmentResult(-3.0, 0.91, 50),
                paths[1]: AlignmentResult(-15.0, 0.84, 50),
            },
            audio_duration=22.0,
        )
        self.assertEqual([(clip.start, clip.end) for clip in model.clips], [(3.0, 13.0), (15.0, 20.0)])
        self.assertEqual([(gap.start, gap.duration) for gap in model.gaps], [(13.0, 2.0)])
        self.assertEqual((model.start, model.end, model.duration), (0.0, 22.0, 22.0))

    def test_visual_timeline_keeps_negative_and_unaligned_clips_visible(self):
        paths = ["early.mov", "pending.mov"]
        model = build_join_timeline_model(
            paths, [4.0, 6.0],
            {paths[0]: AlignmentResult(1.25, 0.72, 50)},
        )
        self.assertAlmostEqual(model.clips[0].start, -1.25)
        self.assertAlmostEqual(model.clips[1].start, 2.75)
        self.assertFalse(model.clips[1].aligned)
        self.assertAlmostEqual(model.start, -1.25)
        self.assertAlmostEqual(model.end, 8.75)

    def test_visual_timeline_reflects_selected_head_and_tail_trims(self):
        paths = ["camera.mov"]
        model = build_join_timeline_model(
            paths, [14.0], {paths[0]: AlignmentResult(2.0, 0.9, 50)},
            audio_duration=10.0, trim_before_audio=True, trim_after_audio=True,
        )
        self.assertEqual((model.clips[0].start, model.clips[0].duration), (0.0, 10.0))
        self.assertEqual((model.start, model.end, model.duration), (0.0, 10.0, 10.0))

    def test_fractional_gaps_do_not_accumulate_rounding_error(self):
        intervals = [(index * 1.013, 1.0) for index in range(100)]
        end = intervals[-1][0] + 1
        spans = plan_join_video_spans([0] * 100, intervals, 0, end, 30, preserve_gaps=True)
        self.assertEqual(sum(span.frames for span in spans), math.floor(end * 30 + 0.5))

    def test_range_discards_unneeded_clips(self):
        spans = plan_join_video_spans([0, 0], [(1, 2), (4, 2)], 3.5, 4.7, 30, preserve_gaps=True)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].source_index, 1)
        self.assertAlmostEqual(spans[0].source_start, 0.5)
        self.assertEqual(spans[0].frames, 36)

    def test_compact_timeline_allows_overlapping_master_slices(self):
        result = resolve_join_output_range([AlignmentResult(-1, 1, 100), AlignmentResult(-2, 1, 100)], [2, 2], preserve_gaps=False)
        self.assertEqual(result, (0, 4, 4))

    def test_parallel_partition_is_exact_and_keeps_one_real_source_per_job(self):
        spans = [
            # Unequal sources and a gap should produce enough queue depth for
            # both workers without changing one frame of the final timeline.
            *plan_join_video_spans([0, 0], [(1, 7), (9, 3)], 0, 11, 30, preserve_gaps=True),
        ]
        chunks = partition_join_video_spans(spans, 2, 30)
        self.assertGreaterEqual(len(chunks), 4)
        self.assertEqual(sum(item.frames for chunk in chunks for item in chunk), 330)
        for chunk in chunks:
            self.assertLessEqual(len({item.source_index for item in chunk if item.source_index is not None}), 1)


class CudaCommandTests(unittest.TestCase):
    def test_cuda_frames_do_not_download_and_black_uploads_once(self):
        media = [MediaInfo(path, 2, 7680, 4320, 30, "hevc", "yuv420p10le", 10) for path in ("a.mov", "b.mov")]
        command = build_gap_preserving_concat_encode_command(
            "ffmpeg", [m.path for m in media], "master.wav",
            [AlignmentResult(-1, 1, 100), AlignmentResult(-4, 1, 100)], media,
            "out.mov", ExportSettings(), Capabilities(), keep_before_audio=True,
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertEqual(command.count("-hwaccel_output_format"), 2)
        self.assertNotIn("hwdownload", graph)
        self.assertNotIn("tpad", graph)
        self.assertIn("trim=end_frame=1,setsar=1/1,hwupload,loop=loop=29", graph)
        self.assertNotIn("+faststart", command)
        self.assertEqual(command.count("-c:v"), 1)
        self.assertNotIn("scale_cuda", graph)

    def test_seeks_only_selected_second_clip_and_master(self):
        media = [MediaInfo(path, 2, 160, 90, 30, "h264", "yuv420p", 8) for path in ("a.mov", "b.mov")]
        command = build_gap_preserving_concat_encode_command(
            "ffmpeg", [m.path for m in media], "master.wav",
            [AlignmentResult(-1, 1, 100), AlignmentResult(-4, 1, 100)], media,
            "out.mov", ExportSettings(), Capabilities(), custom_start=3.5, custom_end=4.7,
        )
        self.assertNotIn("a.mov", command)
        self.assertIn("b.mov", command)
        seek_values = [command[i + 1] for i, value in enumerate(command) if value == "-ss"]
        self.assertEqual(seek_values, ["0.500000", "4.500000"])

    def test_missing_cuda_scaler_uses_explicit_compatibility_path(self):
        reference = MediaInfo("a.mov", 2, 7680, 4320, 30, "hevc", "yuv420p10le", 10)
        settings = ExportSettings(resolution="1080p")
        self.assertFalse(join_uses_cuda_frames(settings, reference, Capabilities(scale_cuda=False)))
        self.assertTrue(join_uses_cuda_frames(settings, reference, Capabilities(scale_cuda=True)))

    def test_parallel_chunk_keeps_cuda_and_forced_multi_nvenc(self):
        media = [MediaInfo(path, 120, 8192, 4320, 30000 / 1001, "hevc", "p010le", 10) for path in ("a.mov", "b.mov")]
        plan = make_join_export_plan(
            [AlignmentResult(-1, 1, 100), AlignmentResult(-122, 1, 100)], media,
            preserve_gaps=True,
        )
        chunk = partition_join_video_spans(plan.spans, 2, plan.fps)[0]
        caps = Capabilities(
            gpu_profile=classify_gpu("NVIDIA GeForce RTX 5090 Laptop GPU"),
            split_encode={"hevc_nvenc"},
        )
        command = build_join_video_chunk_command(
            "ffmpeg", [m.path for m in media], chunk, media, "chunk.mkv",
            ExportSettings(split_encode="3", bit_depth="10-bit"), caps,
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("-hwaccel_output_format", command)
        self.assertNotIn("hwdownload", graph)
        self.assertEqual(command[command.index("-split_encode_mode") + 1], "3")
        self.assertEqual(int(command[command.index("-frames:v") + 1]), sum(span.frames for span in chunk))

    def test_final_parallel_mux_copies_video_and_has_no_faststart_rewrite(self):
        reference = MediaInfo("a.mov", 10, 160, 90, 30, "h264", "yuv420p", 8)
        command = build_join_mux_command(
            "ffmpeg", "parts.ffconcat", "master.wav", [(1, 5)], 5,
            "out.mov", reference,
        )
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertNotIn("+faststart", command)
        self.assertEqual(command.count("master.wav"), 1)


class FFmpegTimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_dir = Path(__file__).resolve().parents[1]
        cls.ffmpeg = find_binary("ffmpeg", app_dir)
        cls.ffprobe = find_binary("ffprobe", app_dir)
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest("FFmpeg/ffprobe unavailable")
        cls.temp = tempfile.TemporaryDirectory(prefix="okk_direct_test_")
        cls.root = Path(cls.temp.name)
        cls.paths = [str(cls.root / "camera1.mov"), str(cls.root / "camera2.mov")]
        for output, colors in zip(cls.paths, [("red", "green"), ("blue", "yellow")]):
            cls.run_command([
                cls.ffmpeg, "-v", "error", "-y", "-filter_complex_threads", "1",
                "-f", "lavfi", "-i", f"color={colors[0]}:s=160x90:r=30:d=1",
                "-f", "lavfi", "-i", f"color={colors[1]}:s=160x90:r=30:d=1",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-threads", "1", output,
            ])
        cls.media = [probe_media(cls.ffprobe, path) for path in cls.paths]
        cls.master = str(cls.root / "master.wav")
        cls.master_samples = array.array("h", (int(12000 * math.sin(2 * math.pi * (230 * n / 48000 + 17 * (n / 48000) ** 2))) for n in range(8 * 48000)))
        with wave.open(cls.master, "wb") as stream:
            stream.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            stream.writeframes(cls.master_samples.tobytes())

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    @staticmethod
    def run_command(command):
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", "replace")[-6000:])
        return result.stdout

    def render(self, name, starts=(1.0, 4.0), **options):
        output = str(self.root / f"{name}.mov")
        results = [AlignmentResult(-start, 1, 100) for start in starts]
        # Exercise the actual timeline/seek/audio graph with an available CPU
        # encoder. No NVIDIA hardware or performance is simulated by this test.
        with patch("editor_core.build_video_args", return_value=["-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-threads", "1", "-fps_mode", "passthrough"]):
            command = build_gap_preserving_concat_encode_command(
                self.ffmpeg, self.paths, self.master, results, self.media, output,
                ExportSettings(decode_mode="cpu"), Capabilities(), audio_codec="pcm_s16le", audio_sample_rate=48000,
                **options,
            )
        command[1:1] = ["-filter_complex_threads", "1"]
        self.run_command(command)
        return output

    def render_parallel(self, name, starts=(1.0, 4.0), **options):
        output = str(self.root / f"{name}.mov")
        results = [AlignmentResult(-start, 1, 100) for start in starts]
        plan = make_join_export_plan(results, self.media, **options)
        chunks = partition_join_video_spans(plan.spans, 2, plan.fps)
        folder = self.root / f"{name}_chunks"
        folder.mkdir(exist_ok=True)
        chunk_paths = [folder / f"chunk_{index:03d}.mov" for index in range(len(chunks))]
        cpu_video_args = [
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
            "-threads", "1", "-fps_mode", "passthrough",
        ]
        for chunk, chunk_path in zip(chunks, chunk_paths):
            with patch("editor_core.build_video_args", return_value=cpu_video_args):
                command = build_join_video_chunk_command(
                    self.ffmpeg, self.paths, chunk, self.media, str(chunk_path),
                    ExportSettings(decode_mode="cpu"), Capabilities(),
                )
            command[1:1] = ["-filter_complex_threads", "1"]
            self.run_command(command)
        manifest = folder / "chunks.ffconcat"
        write_concat_manifest((str(path) for path in chunk_paths), manifest)
        command = build_join_mux_command(
            self.ffmpeg, str(manifest), self.master, plan.audio_intervals,
            plan.duration, output, self.media[0],
            audio_codec="pcm_s16le", audio_sample_rate=48000,
        )
        command[1:1] = ["-filter_complex_threads", "1"]
        self.run_command(command)
        return output

    def assert_audio_slice(self, output, start, duration, output_start=0.0):
        pcm = self.run_command([self.ffmpeg, "-v", "error", "-i", output, "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le", "-"])
        actual = array.array("h")
        actual.frombytes(pcm)
        offset, count = round(output_start * 48000), round(duration * 48000)
        expected_start = round(start * 48000)
        self.assertEqual(actual[offset:offset + count], self.master_samples[expected_start:expected_start + count])

    def assert_color(self, output, timestamp, expected):
        raw = self.run_command([self.ffmpeg, "-v", "error", "-ss", str(timestamp), "-i", output,
                                "-frames:v", "1", "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
        self.assertEqual(len(raw), 3)
        self.assertTrue(all(abs(a - b) < 12 for a, b in zip(raw, expected)), (timestamp, list(raw), expected))

    def test_black_gap_with_continuous_exact_master_audio(self):
        output = self.render("gap")
        self.assertAlmostEqual(probe_media(self.ffprobe, output).duration, 5.0, places=3)
        self.assert_color(output, 0.4, (255, 0, 0))
        self.assert_color(output, 2.4, (0, 0, 0))
        self.assert_color(output, 3.4, (0, 0, 255))
        self.assert_audio_slice(output, 1.0, 5.0)

    def test_parallel_chunks_preserve_black_gap_and_exact_master_audio(self):
        output = self.render_parallel("parallel_gap")
        self.assertAlmostEqual(probe_media(self.ffprobe, output).duration, 5.0, places=3)
        self.assert_color(output, 0.4, (255, 0, 0))
        self.assert_color(output, 2.4, (0, 0, 0))
        self.assert_color(output, 3.4, (0, 0, 255))
        self.assert_audio_slice(output, 1.0, 5.0)

    def test_parallel_compact_join_rebuilds_master_slices(self):
        output = self.render_parallel("parallel_compact", preserve_gaps=False)
        self.assertAlmostEqual(probe_media(self.ffprobe, output).duration, 4.0, places=3)
        self.assert_color(output, 2.4, (0, 0, 255))
        self.assert_audio_slice(output, 1.0, 2.0)
        self.assert_audio_slice(output, 4.0, 2.0, output_start=2.0)

    def test_compact_join_rebuilds_master_slices(self):
        output = self.render("compact", preserve_gaps=False)
        self.assertAlmostEqual(probe_media(self.ffprobe, output).duration, 4.0, places=3)
        self.assert_color(output, 2.4, (0, 0, 255))
        self.assert_audio_slice(output, 1.0, 2.0)
        self.assert_audio_slice(output, 4.0, 2.0, output_start=2.0)

    def test_range_starting_inside_black_gap(self):
        output = self.render("gap_cut", custom_start=2.2, custom_end=4.4)
        self.assertAlmostEqual(probe_media(self.ffprobe, output).duration, 2.2, places=3)
        self.assert_color(output, 0.2, (0, 0, 0))
        self.assert_color(output, 1.0, (0, 0, 255))
        self.assert_audio_slice(output, 3.2, 2.2)

    def test_only_black_selected(self):
        output = self.render("only_black", custom_start=2.2, custom_end=2.8)
        self.assertAlmostEqual(probe_media(self.ffprobe, output).duration, 0.6, places=3)
        self.assert_color(output, 0.2, (0, 0, 0))
        self.assert_audio_slice(output, 3.2, 0.6)

    def test_only_second_clip_selected_and_input_seek_is_precise(self):
        output = self.render("second_cut", custom_start=3.5, custom_end=4.7)
        self.assertAlmostEqual(probe_media(self.ffprobe, output).duration, 1.2, places=3)
        self.assert_color(output, 0.1, (0, 0, 255))
        self.assert_color(output, 0.8, (255, 255, 0))
        self.assert_audio_slice(output, 4.5, 1.2)

    def test_trim_head_to_audio_zero(self):
        output = self.render("leading_trim", starts=(-1.2, 1.8), trim_before_audio=True)
        self.assertAlmostEqual(probe_media(self.ffprobe, output).duration, 3.8, places=3)
        self.assert_color(output, 0.1, (0, 128, 0))
        self.assert_color(output, 1.0, (0, 0, 0))
        self.assert_audio_slice(output, 0, 3.8)

    def test_keep_head_with_silence(self):
        output = self.render("leading_silence", starts=(-1.2, 1.8), keep_before_audio=True)
        pcm = self.run_command([self.ffmpeg, "-v", "error", "-i", output, "-map", "0:a:0", "-f", "s16le", "-"])
        self.assertEqual(pcm[:round(1.2 * 48000) * 2], bytes(round(1.2 * 48000) * 2))
        self.assert_audio_slice(output, 0, 3.8, output_start=1.2)

    def test_end_at_master_end_and_pad_when_requested(self):
        trimmed = self.render("tail_trim", starts=(4, 7), master_audio_duration=8, trim_after_audio=True)
        self.assertAlmostEqual(probe_media(self.ffprobe, trimmed).duration, 4.0, places=3)
        padded = self.render("tail_pad", starts=(4, 7))
        pcm = self.run_command([self.ffmpeg, "-v", "error", "-i", padded, "-map", "0:a:0", "-f", "s16le", "-"])
        self.assertEqual(pcm[4 * 48000 * 2:], bytes(48000 * 2))

    def test_selected_range_wholly_before_audio_is_silent(self):
        output = self.render("before_audio", starts=(-4, 1), keep_before_audio=True, custom_end=1.0)
        pcm = self.run_command([self.ffmpeg, "-v", "error", "-i", output, "-map", "0:a:0", "-f", "s16le", "-"])
        self.assertEqual(pcm, bytes(48000 * 2))

    def test_selected_range_wholly_after_audio_is_silent(self):
        output = self.render("after_audio", starts=(9, 12), custom_end=1.0)
        pcm = self.run_command([self.ffmpeg, "-v", "error", "-i", output, "-map", "0:a:0", "-f", "s16le", "-"])
        self.assertEqual(pcm, bytes(48000 * 2))


if __name__ == "__main__":
    unittest.main()
