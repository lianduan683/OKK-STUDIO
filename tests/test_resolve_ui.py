"""Real Tk regressions for the compact editing shell.

Widgets are constructed directly; background media discovery and playback are
disabled. No user input or native window automation is involved.
"""
from __future__ import annotations

from pathlib import Path
import sys
import tkinter as tk
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from multicam_core import CameraClip, Cut, MulticamProject
from rtx5090_exporter import ExporterApp


class ResolveShellTkTests(unittest.TestCase):
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
        try:
            self.app = ExporterApp()
        except tk.TclError as exc:
            if "display" in str(exc).lower():
                self.skipTest(f"Tk display unavailable: {exc}")
            raise
        self.callback_errors = []
        self.app.report_callback_exception = lambda *details: self.callback_errors.append(details)
        self.app.geometry("1280x820+20+20")
        self.app.update()

    def tearDown(self):
        if hasattr(self, "app"):
            self.app.multicam_workspace.close()
            for pending in self.app.tk.call("after", "info"):
                self.app.tk.call("after", "cancel", pending)
            self.app.destroy()

    def _settle(self):
        self.app.update_idletasks()
        self.app.update()
        self.assertEqual(self.callback_errors, [])

    def _assert_inside(self, widget, parent=None, minimum=(1, 1)):
        parent = parent or self.app
        self.assertTrue(widget.winfo_ismapped(), str(widget))
        self.assertGreaterEqual(widget.winfo_width(), minimum[0], str(widget))
        self.assertGreaterEqual(widget.winfo_height(), minimum[1], str(widget))
        x = widget.winfo_rootx() - parent.winfo_rootx()
        y = widget.winfo_rooty() - parent.winfo_rooty()
        self.assertGreaterEqual(x, 0, str(widget))
        self.assertGreaterEqual(y, 0, str(widget))
        self.assertLessEqual(x + widget.winfo_width(), parent.winfo_width() + 2, str(widget))
        self.assertLessEqual(y + widget.winfo_height(), parent.winfo_height() + 2, str(widget))

    def test_bottom_navigation_selects_real_pages_and_pauses_multicam(self):
        app = self.app
        self.assertEqual(set(app.page_frames), {"basic", "editor", "multicam", "log"})
        self.assertEqual(set(app.page_buttons), set(app.page_frames))
        with mock.patch.object(app.multicam_workspace, "pause") as pause:
            for name in ("multicam", "editor", "basic", "log", "multicam"):
                with self.subTest(page=name):
                    app.page_buttons[name].invoke()
                    self._settle()
                    self.assertEqual(app.page_notebook.select(), str(app.page_frames[name]))
                    self._assert_inside(app.page_frames[name])
            self.assertGreaterEqual(pause.call_count, 1)

    def test_footer_export_uses_current_page_and_working_state(self):
        app = self.app
        with mock.patch.object(app, "_start_export") as export, \
                mock.patch.object(app.multicam_workspace, "show_delivery") as delivery:
            app._select_page("multicam")
            self._settle()
            app.export_button.invoke()
            delivery.assert_called_once()
            export.assert_not_called()
            app._select_page("basic")
            self._settle()
            app.export_button.invoke()
            export.assert_called_once()
        app._set_working(True)
        self.assertEqual(str(app.export_button.cget("state")), "disabled")
        self.assertEqual(str(app.cancel_button.cget("state")), "normal")
        app._set_working(False)
        self.assertEqual(str(app.export_button.cget("state")), "normal")
        self.assertEqual(str(app.cancel_button.cget("state")), "disabled")
        app.progress_var.set(37.5)
        self._settle()
        self.assertEqual(app.progress_canvas.itemcget(app.progress_caption, "text"), "37.5%")

    def test_gpu_feedback_still_updates_without_large_metric_cards(self):
        app = self.app
        app.events.put(("gpu_sample", {
            "utilization.gpu": "24", "utilization.encoder": "83",
            "utilization.decoder": "31", "memory.used": "4096",
            "temperature.gpu": "63", "power.draw": "145",
        }))
        app._drain_events()
        self._settle()
        self.assertEqual(app.gpu_metric_var.get(), "24%")
        self.assertEqual(app.encode_metric_var.get(), "83%")
        self.assertEqual(app.decode_metric_var.get(), "31%")
        self.assertIn("4096 MB", app.monitor_var.get())

    def test_nine_camera_previews_timeline_and_navigation_fit_both_window_sizes(self):
        app = self.app
        workspace = app.multicam_workspace
        workspace.project = MulticamProject(
            clips=[CameraClip(str(i), i, f"camera-{i}.mp4", 0, 12) for i in range(1, 10)],
            cuts=[Cut(0, 1), Cut(4, 2), Cut(8, 3)], fps=30,
        )
        workspace._refresh()
        for size in ("1280x820", "1600x1000"):
            with self.subTest(size=size):
                app.geometry(size + "+20+20")
                app._select_page("multicam")
                self._settle()
                self.assertEqual(len(workspace.camera_surfaces), 9)
                for surface in workspace.camera_surfaces.values():
                    self._assert_inside(surface, workspace, minimum=(70, 55))
                self._assert_inside(workspace.program_surface, workspace, minimum=(180, 120))
                self._assert_inside(workspace.timeline, workspace, minimum=(600, 85))
                self._assert_inside(app.export_button)
                self._assert_inside(app.cancel_button)
                for button in app.page_buttons.values():
                    self._assert_inside(button)
                self.assertGreaterEqual(workspace.winfo_height(), 480)

    def test_edit_preview_and_join_timeline_remain_visible(self):
        app = self.app
        for size in ("1280x820", "1600x1000"):
            with self.subTest(size=size):
                app.geometry(size + "+20+20")
                app._select_page("editor")
                self._settle()
                self._assert_inside(app.player_stage, minimum=(220, 90))
                self._assert_inside(app.player_surface, app.player_stage, minimum=(150, 75))
                self._assert_inside(app.editor_timeline, minimum=(100, 15))
                self._assert_inside(app.editor_join_timeline_canvas, minimum=(650, 100))
                self._assert_inside(app.export_button)


if __name__ == "__main__":
    unittest.main()
