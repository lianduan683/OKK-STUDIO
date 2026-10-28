"""Real Tk tests for placing audio-synchronized cameras on the edit timeline.

These tests construct and close their own widgets. Media analysis is replaced
with deterministic results; no player, GPU probing, user input, or native window
automation is involved.
"""
from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
import tkinter as tk
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from editor_core import AlignmentResult
from exporter_core import Capabilities
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_ui import MulticamWorkspace


class SyncTimelineTkTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            if "display" in str(exc).lower():
                self.skipTest(f"Tk display unavailable: {exc}")
            raise
        self.root.withdraw()
        self.root.geometry("1280x820+20+20")
        self.root.working = False
        self.root.cancel_enabled = False
        self.root.mpv_path = ""
        self.root.mpv = None
        self.root.cancel_event = threading.Event()
        self.root.caps = Capabilities(ffmpeg_path="test-ffmpeg.exe")
        self.root.progress_var = tk.DoubleVar(self.root, value=0)
        self.root.progress_text_var = tk.StringVar(self.root)
        self.working_changes = []
        self.root._set_working = self._set_working
        self.root._begin_shared_progress = mock.Mock()
        self.root._log = mock.Mock()
        self.callback_errors = []
        self.root.report_callback_exception = lambda *details: self.callback_errors.append(details)
        self.worker_releases = []
        self.workspace = MulticamWorkspace(self.root, self.root)
        self.workspace.pack(fill="both", expand=True)
        self.workspace.project = MulticamProject(
            clips=[CameraClip("a", 1, "camera-one.mp4", 0, 12),
                   CameraClip("b", 2, "camera-two.mp4", 2, 8),
                   CameraClip("c", 3, "camera-three.mp4", 6, 5),
                   CameraClip("d", 1, "camera-one-part-two.mp4", 20, 4)],
            cuts=[Cut(0, 1)], fps=25,
            master_audio="continuous-master.wav", master_duration=40,
        )
        self.workspace._refresh()
        self.root.deiconify()
        self.root.update()
        self.root.update_idletasks()
        self.original_starts = self._starts()
        self.assertEqual(self.callback_errors, [])

    def tearDown(self):
        for release in getattr(self, "worker_releases", []):
            release.set()
        if hasattr(self, "workspace"):
            self.workspace.close()
        if hasattr(self, "root"):
            for pending in self.root.tk.call("after", "info"):
                self.root.tk.call("after", "cancel", pending)
            self.root.destroy()

    def _set_working(self, value):
        self.root.working = bool(value)
        self.root.cancel_enabled = bool(value)
        self.working_changes.append(bool(value))

    def _starts(self):
        return {clip.id: clip.start for clip in self.workspace.project.clips}

    def _alignment_pairs(self):
        # Deliberately return camera 2 before camera 1 to catch index-based placement.
        return [("b", AlignmentResult(1.03, 0.96, 200, "test-fast")),
                ("a", AlignmentResult(-4.07, 0.88, 200, "test-fast")),
                ("c", AlignmentResult(-29.0, 0.12, 200, "test-review"))]

    def _terminal_jobs(self, timeout=5):
        collected = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            item = self.workspace.jobs.get(timeout=max(0.01, deadline - time.monotonic()))
            collected.append(item)
            if item[0] in {"aligned", "align_cancelled", "align_error"}:
                return collected
        self.fail("synchronization worker did not publish a terminal result")

    def _consume_jobs(self, items):
        for item in items:
            self.workspace.jobs.put(item)
        self.workspace._poll()
        self.root.update_idletasks()
        self.assertEqual(self.callback_errors, [])

    def test_finish_alignment_places_signed_offsets_on_correct_camera_and_fits_master(self):
        workspace = self.workspace
        workspace.zoom = 9
        workspace.timeline.xview_moveto(0.5)
        workspace.tools.select(1)
        workspace.toggle_media_pool()
        self._set_working(True)
        workspace.busy = True
        workspace.sync_running = True

        workspace._finish_alignment(self._alignment_pairs(), elapsed=2.5)
        self.root.update()

        self.assertEqual(self._starts(), {"a": 4.08, "b": -1.04, "c": 6, "d": 20})
        self.assertEqual({clip.id: clip.camera for clip in workspace.project.clips},
                         {"a": 1, "b": 2, "c": 3, "d": 1})
        self.assertEqual(workspace.zoom, 1)
        self.assertAlmostEqual(workspace.timeline.xview()[0], 0, places=3)
        self.assertEqual(workspace._timeline_origin(), -1.04)
        self.assertTrue(workspace._media_visible)
        self.assertEqual(workspace.tools.index("current"), 0)
        self.assertEqual(workspace.media_tree.selection(), ("b",))
        self.assertFalse(workspace.busy)
        self.assertFalse(workspace.sync_running)
        self.assertFalse(self.root.working)
        self.assertEqual(self.root.progress_var.get(), 100)

        rectangles = {clip_id: workspace.timeline.coords(item)
                      for item, clip_id in workspace.timeline_clip_items.items()
                      if workspace.timeline.type(item) == "rectangle"}
        self.assertAlmostEqual(rectangles["b"][0], workspace._timeline_left, places=3)
        self.assertLess(rectangles["b"][0], rectangles["a"][0])
        self.assertTrue(all(0 <= coords[0] < coords[2] <= workspace.timeline.winfo_width()
                            for coords in rectangles.values()))
        audio = [item for item in workspace.timeline.find_all()
                 if workspace.timeline.type(item) == "rectangle"
                 and workspace.timeline.itemcget(item, "fill") == "#3d625c"]
        self.assertEqual(len(audio), 1)
        audio_coords = workspace.timeline.coords(audio[0])
        self.assertGreater(audio_coords[0], rectangles["b"][0])
        self.assertLessEqual(audio_coords[2], workspace.timeline.winfo_width())
        self.assertGreaterEqual(audio_coords[1], workspace.timeline.canvasy(0))
        self.assertLessEqual(audio_coords[3], workspace.timeline.canvasy(workspace.timeline.winfo_height()))
        self.assertIn("2.5", workspace.status.get())

    def test_low_confidence_keeps_original_position_and_marks_review(self):
        workspace = self.workspace
        workspace._finish_alignment(self._alignment_pairs())
        self.assertEqual(self._starts()["c"], self.original_starts["c"])
        self.assertIn("review", workspace.media_tree.item("c", "tags"))
        self.assertIn("待检查", workspace.media_tree.item("c", "text"))
        self.assertIn("synced", workspace.media_tree.item("a", "tags"))
        self.assertIn("synced", workspace.media_tree.item("b", "tags"))
        self.assertEqual(workspace.sync_results["c"].confidence, 0.12)
        self.assertEqual(self.callback_errors, [])

    def test_alignment_is_one_undoable_dirty_change(self):
        workspace = self.workspace
        workspace._finish_alignment(self._alignment_pairs())
        placed = self._starts()
        self.assertTrue(workspace.dirty)
        self.assertEqual(len(workspace.undo_stack), 1)
        workspace.undo()
        self.assertEqual(self._starts(), self.original_starts)
        self.assertEqual(workspace.sync_results, {})
        self.assertNotIn("review", workspace.media_tree.item("c", "tags"))
        workspace.redo()
        self.assertEqual(self._starts(), placed)
        self.assertEqual(self.callback_errors, [])

    def _check_worker(self, mode_text, expected_mode, selected=False):
        workspace = self.workspace
        workspace.sync_mode_var.set(mode_text)
        if selected:
            workspace.media_tree.selection_set("b")
        self.root.cancel_event.set()  # A previous cancellation must not cancel this run.

        def analyze(ffmpeg, paths, master, **kwargs):
            kwargs["progress"](37.0, "test analyzing")
            return [AlignmentResult(-3.2, 0.9, 200, "test-worker") for _ in paths]

        with mock.patch("multicam_ui.align_audio_segments", side_effect=analyze) as core:
            workspace.align(selected=selected)
            self.assertTrue(workspace.busy)
            self.assertTrue(workspace.sync_running)
            self.assertTrue(self.root.working)
            self.assertTrue(self.root.cancel_enabled)
            self.assertFalse(self.root.cancel_event.is_set())
            self.root._begin_shared_progress.assert_called_once()
            items = self._terminal_jobs()
            self.assertEqual(items[-1][0], "aligned")
            core.assert_called_once()
            args, kwargs = core.call_args
            self.assertEqual(args[0], "test-ffmpeg.exe")
            self.assertEqual(args[1], ["camera-two.mp4"] if selected else
                             [clip.path for clip in workspace.project.clips])
            self.assertEqual(args[2], "continuous-master.wav")
            self.assertEqual(kwargs["mode"], expected_mode)
            self.assertEqual(kwargs["max_offset_seconds"], 0)
            self.assertIs(kwargs["cancel_event"], self.root.cancel_event)
            self.assertIs(kwargs["continue_on_error"], True)
            self.assertTrue(callable(kwargs["progress"]))
            pairs, elapsed = items[-1][1]
            self.assertEqual([clip_id for clip_id, _ in pairs], ["b"] if selected else ["a", "b", "c", "d"])
            self.assertGreaterEqual(elapsed, 0)
            self.assertEqual(self._starts(), self.original_starts)
            self._consume_jobs(items)
        self.assertFalse(self.root.working)
        self.assertFalse(workspace.sync_running)
        self.assertFalse(workspace.busy)
        self.assertEqual(self.working_changes, [True, False])
        self.assertEqual(self._starts()["b"], 3.2)
        self.assertTrue(workspace.dirty)
        self.assertEqual(self.root.progress_var.get(), 100)

    def test_default_fast_sync_worker_uses_cancellable_batch_and_new_queue_payload(self):
        self._check_worker("快速定位（推荐）", "fast")

    def test_full_sync_worker_passes_selected_clip_and_full_mode(self):
        self._check_worker("完整分析", "full", selected=True)
        self.assertEqual(self._starts()["a"], self.original_starts["a"])
        self.assertEqual(self._starts()["c"], self.original_starts["c"])

    def test_cancellation_after_analysis_does_not_partially_change_timeline(self):
        workspace = self.workspace
        started = threading.Event()
        release = threading.Event()
        self.worker_releases.append(release)

        def analyze(ffmpeg, paths, master, **kwargs):
            started.set()
            if not release.wait(5):
                raise AssertionError("test did not release analysis worker")
            return [AlignmentResult(-8, 0.95, 200, "test-cancel") for _ in paths]

        with mock.patch("multicam_ui.align_audio_segments", side_effect=analyze):
            workspace.align()
            try:
                self.assertTrue(started.wait(5))
                self.assertTrue(self.root.cancel_enabled)
                self.root.cancel_event.set()
            finally:
                release.set()
            items = self._terminal_jobs()
            self.assertEqual(items[-1], ("align_cancelled", None))
            self.assertFalse(any(kind == "aligned" for kind, _ in items))
            self._consume_jobs(items)

        self.assertEqual(self._starts(), self.original_starts)
        self.assertFalse(workspace.dirty)
        self.assertEqual(workspace.undo_stack, [])
        self.assertEqual(workspace.sync_results, {})
        self.assertFalse(workspace.busy)
        self.assertFalse(workspace.sync_running)
        self.assertFalse(self.root.working)
        self.assertFalse(self.root.cancel_enabled)
        self.assertIn("保留原时间线", workspace.status.get())


if __name__ == "__main__":
    unittest.main()
