"""Real Tk delivery settings, render dispatch, and export telemetry regressions.

The tests create their own widgets and synthesize render snapshots. They do not
control the user's app, keyboard, or mouse, and never encode the user's media.
"""
from __future__ import annotations

from pathlib import Path
import queue
import sys
import tempfile
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delivery_panel import make_delivery_settings
from exporter_core import Capabilities
from multicam_core import CameraClip, Cut, MulticamProject
from rtx5090_exporter import ExporterApp


def descendants(parent):
    for child in parent.winfo_children():
        yield child
        yield from descendants(child)


class DeliveryTkTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(ExporterApp, "_detect_async"),
            mock.patch.object(ExporterApp, "_poll_editor_player"),
            mock.patch.object(ExporterApp, "_poll_gpu"),
            mock.patch.object(ExporterApp, "state", return_value="normal"),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self._create_app()

    def _create_app(self, scale=1.0):
        original = tk.Misc.winfo_fpixels

        def pixels(widget, number):
            return 96.0 * scale if number == "1i" else original(widget, number)

        try:
            with mock.patch.object(tk.Misc, "winfo_fpixels", pixels):
                self.app = ExporterApp()
        except tk.TclError as exc:
            if "display" in str(exc).lower():
                self.skipTest(f"Tk display unavailable: {exc}")
            raise
        self.workspace = self.app.multicam_workspace
        self.app.caps = Capabilities(
            ffmpeg_path="test-ffmpeg", ffprobe_path="test-ffprobe",
            nvenc_codecs={"h264_nvenc", "hevc_nvenc", "av1_nvenc"},
            cuda_decode=True,
        )
        self.app.mpv_path = ""
        self.callback_errors = []
        self.app.report_callback_exception = lambda *details: self.callback_errors.append(details)
        # Tk's default wm maxsize follows the host monitor, which can be smaller
        # than the off-screen 4K viewport used by this rendering regression.
        self.app.maxsize(7680, 4320)
        # Right-align the simulated 4K viewport so the delivery inspector gets
        # real expose events even when the test host has a smaller monitor.
        self.app.geometry("3840x2160-0+0" if scale > 1 else "1280x820+20+20")
        self.app._select_page("multicam")
        self.workspace.show_delivery()
        self._settle()

    def tearDown(self):
        self._destroy_app()

    def _destroy_app(self):
        if hasattr(self, "app"):
            self.app.multicam_workspace.close()
            for pending in self.app.tk.call("after", "info"):
                self.app.tk.call("after", "cancel", pending)
            self.app.destroy()
            del self.app

    def _settle(self):
        self.app.update_idletasks()
        self.app.update()
        self.assertEqual(self.callback_errors, [])

    def _bound_widgets(self, variable):
        result = []
        for widget in descendants(self.workspace.delivery_panel):
            if "textvariable" in widget.keys() and str(widget.cget("textvariable")) == str(variable):
                result.append(widget)
            elif "variable" in widget.keys() and str(widget.cget("variable")) == str(variable):
                result.append(widget)
        return result

    def _assert_inside(self, widget, parent=None, minimum=(1, 1)):
        parent = parent or self.app
        self.assertTrue(widget.winfo_ismapped(), str(widget))
        self.assertGreaterEqual(widget.winfo_width(), minimum[0], str(widget))
        self.assertGreaterEqual(widget.winfo_height(), minimum[1], str(widget))
        x = widget.winfo_rootx() - parent.winfo_rootx()
        y = widget.winfo_rooty() - parent.winfo_rooty()
        self.assertGreaterEqual(x, -2, str(widget))
        self.assertGreaterEqual(y, -2, str(widget))
        self.assertLessEqual(x + widget.winfo_width(), parent.winfo_width() + 2, str(widget))
        self.assertLessEqual(y + widget.winfo_height(), parent.winfo_height() + 2, str(widget))

    def _set_nondefault_settings(self):
        app, workspace = self.app, self.workspace
        values = [
            (app.codec_var, "AV1 (NVENC)"),
            (app.preset_var, "高质量 P6"),
            (app.quality_var, 17),
            (app.resolution_var, "4K"),
            (app.depth_var, "10-bit"),
            (app.maxrate_var, "95M"),
            (app.decode_var, "CPU 解码（兼容模式）"),
            (app.split_var, "强制 3 路"),
            (app.parallel_var, 3),
            (workspace.bitrate_var, "47M"),
            (workspace.delivery_container_var, "MOV"),
            (workspace.delivery_audio_format_var, "PCM 24-bit"),
            (workspace.delivery_audio_bitrate_var, "256k"),
            (workspace.delivery_audio_sample_rate_var, "44100"),
            (workspace.delivery_gop_var, "72"),
            (workspace.delivery_bframes_var, "3"),
            (workspace.delivery_tune_var, "ll"),
            (workspace.delivery_multipass_var, "qres"),
            (workspace.delivery_gpuindex_var, "2"),
        ]
        for variable, value in values:
            variable.set(value)

    def _assert_nondefault_settings(self, settings):
        expected = {
            "codec": "av1_nvenc", "quality": 17, "preset": "p6",
            "resolution": "4k", "bit_depth": "10-bit", "video_bitrate": "47M",
            "maxrate": "95M", "decode_mode": "cpu", "split_encode": "3",
            "parallel_workers": 3, "audio": "pcm_s24le", "audio_bitrate": "",
            "audio_sample_rate": 44100, "gop": 72, "bframes": 3,
            "tune": "ll", "multipass": "qres", "gpu_index": 2,
        }
        for name, value in expected.items():
            with self.subTest(setting=name):
                self.assertEqual(getattr(settings, name), value)

    def test_delivery_fields_and_shared_rtx_settings_reach_render_settings(self):
        self._set_nondefault_settings()
        settings, suffix = make_delivery_settings(self.workspace)
        self._assert_nondefault_settings(settings)
        self.assertEqual(suffix, ".mov")
        # Each shared value has an actual editable delivery control bound to it.
        for variable in (self.app.codec_var, self.app.preset_var, self.app.quality_var,
                         self.app.resolution_var, self.app.depth_var, self.app.maxrate_var,
                         self.app.decode_var, self.app.split_var, self.app.parallel_var):
            with self.subTest(variable=str(variable)):
                self.assertTrue(self._bound_widgets(variable))
        self.app.depth_var.set("8-bit")
        for label, codec in (("H.264 (NVENC)", "h264_nvenc"),
                             ("HEVC / H.265 (NVENC)", "hevc_nvenc"),
                             ("AV1 (NVENC)", "av1_nvenc")):
            self.app.codec_var.set(label)
            with self.subTest(codec=codec):
                self.assertEqual(make_delivery_settings(self.workspace)[0].codec, codec)

    def test_audio_choices_and_sample_rate_are_not_silently_replaced_with_aac_48000(self):
        workspace = self.workspace
        workspace.delivery_container_var.set("MKV")
        workspace.delivery_audio_bitrate_var.set("256")
        workspace.delivery_audio_sample_rate_var.set("96000")
        choices = {
            "AAC": "aac", "复制主音频": "copy", "无音频": "none",
            "PCM 16-bit": "pcm_s16le", "PCM 24-bit": "pcm_s24le",
            "PCM 32-bit": "pcm_s32le", "PCM Float32": "pcm_f32le", "FLAC": "flac",
        }
        for label, codec in choices.items():
            with self.subTest(audio=label):
                workspace.delivery_audio_format_var.set(label)
                settings, suffix = make_delivery_settings(workspace)
                self.assertEqual(settings.audio, codec)
                self.assertEqual(settings.audio_bitrate, "256k" if codec == "aac" else "")
                self.assertEqual(settings.audio_sample_rate, None if codec in {"copy", "none"} else 96000)
                self.assertEqual(suffix, ".mkv")
                bitrate_control = self._bound_widgets(workspace.delivery_audio_bitrate_var)[0]
                rate_control = self._bound_widgets(workspace.delivery_audio_sample_rate_var)[0]
                self.assertEqual(str(bitrate_control.cget("state")) == "disabled", codec != "aac")
                self.assertEqual(str(rate_control.cget("state")) == "disabled", codec in {"copy", "none"})

    def test_incompatible_audio_and_invalid_numeric_fields_are_rejected_before_export(self):
        workspace = self.workspace
        for container, audio in (("MP4", "PCM 24-bit"), ("MP4", "FLAC"), ("MOV", "FLAC")):
            with self.subTest(container=container, audio=audio):
                workspace.delivery_container_var.set(container)
                workspace.delivery_audio_format_var.set(audio)
                with self.assertRaises(ValueError):
                    make_delivery_settings(workspace)
        workspace.delivery_container_var.set("MKV")
        workspace.delivery_audio_format_var.set("AAC")
        for variable, bad in ((workspace.delivery_audio_sample_rate_var, "invalid"),
                              (workspace.delivery_gop_var, "-1"),
                              (workspace.delivery_bframes_var, "-1"),
                              (workspace.delivery_gpuindex_var, "-1")):
            with self.subTest(variable=str(variable)):
                previous = variable.get()
                variable.set(bad)
                try:
                    with self.assertRaises(ValueError):
                        make_delivery_settings(workspace)
                finally:
                    variable.set(previous)

    def test_container_selection_synchronizes_existing_output_suffix(self):
        workspace = self.workspace
        workspace.output_var.set(str(Path(tempfile.gettempdir()) / "delivery-output.MP4"))
        for container, suffix in (("MOV", ".mov"), ("MKV", ".mkv"), ("MP4", ".mp4")):
            with self.subTest(container=container):
                workspace.delivery_container_var.set(container)
                self._settle()
                self.assertEqual(Path(workspace.output_var.get()).suffix, suffix)
                self.assertEqual(make_delivery_settings(workspace)[1], suffix)

    def test_cpu_choice_preserves_quality_resolution_and_audio_settings(self):
        self._set_nondefault_settings()
        self.workspace.export_mode.set("CPU H.264（兼容模式）")
        settings, suffix = make_delivery_settings(self.workspace)
        self.assertEqual(settings.codec, "libx264")
        self.assertEqual(settings.decode_mode, "cpu")
        self.assertEqual(settings.quality, 17)
        self.assertEqual(settings.resolution, "4k")
        self.assertEqual(settings.bit_depth, "10-bit")
        self.assertEqual(settings.audio, "pcm_s24le")
        self.assertEqual(settings.audio_sample_rate, 44100)
        self.assertEqual(suffix, ".mov")

    def test_export_worker_receives_selected_settings_and_normalized_container(self):
        self._set_nondefault_settings()
        workspace = self.workspace
        captured = {}

        def render(project, output, ffmpeg, ffprobe, settings, caps, process, **kwargs):
            captured.update(project=project, output=output, settings=settings, kwargs=kwargs)

        with tempfile.TemporaryDirectory(prefix="delivery-ui-dispatch-") as folder:
            source = Path(folder) / "source.mp4"
            source.write_bytes(b"mock source")
            workspace.project = MulticamProject(
                clips=[CameraClip("a", 1, str(source), 0, 8)], cuts=[Cut(0, 1)], fps=25,
            )
            workspace.start_var.set("1")
            workspace.end_var.set("7")
            workspace.fps_var.set("25")
            workspace.output_var.set(str(Path(folder) / "program"))
            with mock.patch("multicam_ui.render_multicam", side_effect=render), \
                    mock.patch("multicam_ui.messagebox.showerror") as error:
                workspace.export()
                deadline = time.monotonic() + 5
                jobs = []
                while time.monotonic() < deadline:
                    try:
                        kind, payload = workspace.jobs.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    jobs.append((kind, payload))
                    if kind in {"exported", "export_error"}:
                        break
                error.assert_not_called()
                self.assertTrue(jobs, "export worker never published a result")
                self.assertEqual(jobs[-1][0], "exported", jobs)
            self._assert_nondefault_settings(captured["settings"])
            self.assertEqual(Path(captured["output"]).suffix, ".mov")
            self.assertEqual((captured["project"].range_start, captured["project"].range_end,
                              captured["project"].fps), (1.0, 7.0, 25.0))
            self.assertTrue(callable(captured["kwargs"]["metrics"]))
            self.assertIs(captured["kwargs"]["cancel_event"], self.app.cancel_event)
        workspace.busy = False
        self.app._set_working(False)

    def _snapshot(self, **overrides):
        snapshot = {
            "phase": "编码 2 / 6", "stage": "encoding", "progress_percent": 42.5,
            "processed_seconds": 425.0, "total_seconds": 1000.0,
            "encoded_frames": 12750, "fps": 184.5, "speed": 6.15,
            "elapsed": 85.0, "eta": 93.5, "active_workers": 3,
        }
        snapshot.update(overrides)
        return snapshot

    def test_parallel_encoding_snapshot_updates_delivery_and_footer_with_fps_speed_and_eta(self):
        workspace = self.workspace
        workspace.jobs.put(("export_metrics", self._snapshot()))
        workspace._poll()
        self._settle()
        self.assertAlmostEqual(workspace.export_progress_var.get(), 42.5)
        self.assertAlmostEqual(self.app.progress_var.get(), 42.5)
        speed = workspace.export_speed_var.get()
        self.assertIn("184.5", speed)
        self.assertIn("6.15", speed)
        self.assertIn("fps", speed.lower())
        self.assertRegex(speed, r"[×x]")
        self.assertIn("01:25", workspace.export_elapsed_var.get())
        self.assertIn("01:3", workspace.export_eta_var.get())
        self.assertIn("3", workspace.export_metrics_var.get())
        self.assertIn("184.5", self.app.progress_text_var.get())
        # Legacy phase events must not erase the live speed summary.
        workspace.jobs.put(("progress", (43.0, "编码 3 / 6")))
        workspace._poll()
        self.assertIn("184.5", self.app.progress_text_var.get())

    def test_mux_snapshot_labels_container_speed_without_fake_encoding_fps(self):
        workspace = self.workspace
        workspace._update_export_metrics(self._snapshot(
            stage="muxing", phase="封装主音频", fps=0, speed=32.4,
            progress_percent=97, processed_seconds=1000, encoded_frames=30000,
            eta=None, active_workers=1,
        ))
        self._settle()
        speed = workspace.export_speed_var.get()
        self.assertIn("封装", speed)
        self.assertIn("32.4", speed)
        self.assertNotRegex(speed.lower(), r"\d+(?:\.\d+)?\s*fps")
        self.assertNotIn("184.5", workspace.export_metrics_var.get())
        self.assertTrue("—" in workspace.export_eta_var.get() or "--" in workspace.export_eta_var.get())

    def test_completion_keeps_100_percent_and_elapsed_in_persistent_panel(self):
        workspace = self.workspace
        workspace._update_export_metrics(self._snapshot(
            stage="done", phase="完成", progress_percent=100, processed_seconds=1000,
            encoded_frames=30000, elapsed=163.2, eta=0, active_workers=0, fps=0, speed=0,
        ))
        workspace.jobs.put(("exported", "mock-finished.mkv"))
        with mock.patch("multicam_ui.messagebox.showinfo"):
            workspace._poll()
        self._settle()
        self.assertEqual(workspace.export_progress_var.get(), 100)
        self.assertEqual(self.app.progress_var.get(), 100)
        self.assertIn("02:43", workspace.export_elapsed_var.get())
        self.assertTrue(workspace.export_metrics_var.get())
        self.assertFalse(workspace.busy)
        self.assertFalse(self.app.working)

    def test_cancelled_or_failed_job_keeps_measured_progress(self):
        workspace = self.workspace
        for cancelled in (True, False):
            with self.subTest(cancelled=cancelled):
                workspace._update_export_metrics(self._snapshot(progress_percent=42.5))
                workspace.jobs.put(("export_error", ("synthetic render failure", cancelled)))
                with mock.patch("multicam_ui.messagebox.showerror"):
                    workspace._poll()
                self.assertEqual(workspace.export_progress_var.get(), 42.5)
                self.assertEqual(self.app.progress_var.get(), 42.5)
                self.assertFalse(workspace.busy)
                self.assertFalse(self.app.working)

    def _check_delivery_layout(self):
        workspace = self.workspace
        workspace._update_export_metrics(self._snapshot())
        self._settle()
        self.assertEqual(len(workspace.delivery_tabs.tabs()), 4)
        self._assert_inside(workspace.delivery_tabs, minimum=(180, 100))
        self._assert_inside(workspace.delivery_export_button, minimum=(100, 20))
        self._assert_inside(workspace.delivery_progressbar, minimum=(100, 5))
        self._assert_inside(workspace.export_monitor, minimum=(300, 35))
        self._assert_inside(workspace.export_speed_label, minimum=(180, 15))
        self._assert_inside(self.app.cancel_button, minimum=(35, 20))
        canvases = workspace.delivery_canvases
        canvases = list(canvases.values()) if isinstance(canvases, dict) else list(canvases)
        self.assertEqual(len(canvases), 4)
        for tab, canvas in zip(workspace.delivery_tabs.tabs(), canvases):
            with self.subTest(tab=tab):
                workspace.delivery_tabs.select(tab)
                self._settle()
                # On a host smaller than the simulated viewport, Windows does
                # not expose the off-screen canvas; realize its window item as
                # a real 4K monitor would when this tab is first displayed.
                canvas.event_generate("<Expose>")
                self._settle()
                self._assert_inside(canvas, minimum=(160, 80))
                canvas.yview_moveto(1)
                self._settle()
                # Scrolling advanced settings never scrolls away render feedback.
                self._assert_inside(workspace.delivery_export_button)
                self._assert_inside(workspace.delivery_progressbar)
                self._assert_inside(self.app.cancel_button)
        advanced_toggle = next(widget for widget in descendants(workspace.delivery_panel)
                               if isinstance(widget, tk.Checkbutton)
                               and widget.cget("text") == "高级编码设置")
        advanced_toggle.invoke()
        self._settle()
        for variable in (workspace.start_var, workspace.fps_var, workspace.output_var,
                         self.app.codec_var, self.app.quality_var,
                         workspace.delivery_audio_format_var,
                         workspace.delivery_audio_sample_rate_var, self.app.parallel_var,
                         workspace.delivery_gop_var, workspace.delivery_gpuindex_var):
            control = self._bound_widgets(variable)[0]
            canvas_index = next(index for index, canvas in enumerate(canvases)
                                if str(control).startswith(str(canvas) + "."))
            canvas = canvases[canvas_index]
            workspace.delivery_tabs.select(workspace.delivery_tabs.tabs()[canvas_index])
            canvas.yview_moveto(0)
            self._settle()
            canvas.event_generate("<Expose>")
            self._settle()
            inner = canvas.winfo_children()[0]
            target_y = control.winfo_rooty() - inner.winfo_rooty()
            bounds = canvas.bbox("all")
            canvas.yview_moveto(max(0, (target_y - 8) / max(1, bounds[3] - bounds[1])))
            self._settle()
            canvas.event_generate("<Expose>")
            self._settle()
            with self.subTest(control=str(variable)):
                self._assert_inside(control, canvas, minimum=(100, 12))
        workspace.delivery_tabs.select(workspace.delivery_tabs.tabs()[0])

    def test_delivery_primary_controls_remain_visible_at_1280(self):
        self._check_delivery_layout()

    def test_delivery_primary_controls_remain_visible_at_4k_with_200_percent_scaling(self):
        self._destroy_app()
        self._create_app(scale=2)
        self.assertEqual(self.workspace.ui_scale, 2)
        self._check_delivery_layout()


if __name__ == "__main__":
    unittest.main()
