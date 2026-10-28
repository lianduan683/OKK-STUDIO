"""Real Tk multicamera workspace state and layout regressions.

These tests create widgets directly. They never automate the user's keyboard,
mouse, or application windows, and they do not launch MPV or GPU detection.
"""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import threading
import tkinter as tk
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exporter_core import Capabilities, find_binary
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_ui import MulticamWorkspace


class MulticamTkTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.root.geometry("1280x640+20+20")
        self.root.working = False
        self.root.mpv_path = ""
        self.root.mpv = None
        self.root.cancel_event = threading.Event()
        self.root.caps = Capabilities(ffprobe_path=find_binary("ffprobe", Path(__file__).resolve().parents[1]))
        self.root._log = lambda text: None
        self.callback_errors = []
        self.root.report_callback_exception = lambda *details: self.callback_errors.append(details)
        self.workspace = MulticamWorkspace(self.root, self.root)
        self.workspace.pack(fill="both", expand=True)
        self.workspace.project = MulticamProject(
            clips=[CameraClip("a", 1, "a.mp4", 0, 10), CameraClip("b", 2, "b.mp4", 0, 10)],
            cuts=[Cut(0, 1)], fps=30, range_start=0, range_end=None,
        )
        self.workspace._refresh()

    def tearDown(self):
        if hasattr(self, "workspace"):
            self.workspace.close()
        if hasattr(self, "root"):
            for pending in self.root.tk.call("after", "info"):
                self.root.tk.call("after", "cancel", pending)
            self.root.destroy()

    def test_nine_camera_layout_keeps_program_transport_and_timeline_available(self):
        workspace = self.workspace
        workspace.project.clips = [CameraClip(str(camera), camera, f"camera-{camera}.mp4", 0, 10)
                                   for camera in range(1, 10)]
        workspace._refresh()
        self.root.deiconify()
        self.root.update()
        self.root.update_idletasks()
        self.assertEqual(len(workspace.camera_surfaces), 9)
        self.assertTrue(all(surface.winfo_ismapped() for surface in workspace.camera_surfaces.values()))
        self.assertTrue(workspace.program_surface.winfo_ismapped())
        self.assertTrue(workspace.timeline.winfo_ismapped())
        self.assertLessEqual(workspace.timeline.winfo_y() + workspace.timeline.winfo_height(), workspace.winfo_height())
        self.assertGreater(workspace.tools.winfo_width(), 280)
        self.assertEqual(self.callback_errors, [])

    def test_recorded_camera_switch_can_be_undone_and_redone(self):
        workspace = self.workspace
        workspace.current = 1
        workspace.switch(2)
        self.assertEqual(workspace.project.camera_at(1), 2)
        self.assertTrue(workspace.dirty)
        workspace.undo()
        self.assertEqual(workspace.project.camera_at(1), 1)
        workspace.redo()
        self.assertEqual(workspace.project.camera_at(1), 2)
        self.assertEqual(self.callback_errors, [])

    def test_audio_only_wav_can_be_selected_as_continuous_master(self):
        if not self.root.caps.ffprobe_path:
            self.skipTest("FFprobe unavailable")
        with tempfile.TemporaryDirectory(prefix="multicam_ui_master_") as folder:
            path = Path(folder) / "master.wav"
            with wave.open(str(path), "wb") as stream:
                stream.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                stream.writeframes(bytes(48000))
            self.workspace._load_master(str(path))
            kind, payload = self.workspace.jobs.get(timeout=15)
            self.assertEqual(kind, "master", payload)
            self.assertEqual(Path(payload[0]), path)
            self.assertAlmostEqual(payload[1], 0.5, places=3)

    def test_changing_saved_range_or_fps_marks_project_dirty(self):
        workspace = self.workspace
        with tempfile.TemporaryDirectory(prefix="multicam_ui_save_") as folder:
            workspace.project_path = str(Path(folder) / "saved.multicam.json")
            for variable, changed in [(workspace.start_var, "1"), (workspace.end_var, "8"), (workspace.fps_var, "25")]:
                with self.subTest(value=changed):
                    self.assertTrue(workspace.save())
                    self.assertFalse(workspace.dirty)
                    variable.set(changed)
                    self.assertTrue(workspace.dirty, "an edited project setting could be discarded without a save prompt")

    def test_queued_state_from_closed_monitor_cannot_change_new_project(self):
        workspace = self.workspace

        class ClosingPlayer:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        old_player = ClosingPlayer()
        workspace.player = old_player
        workspace.player_queue.put(("state", {"time": 8.5, "playing": True, "camera": 2}))
        workspace.dirty = False
        workspace.new()
        self.assertTrue(old_player.closed)
        workspace._poll()
        self.assertEqual(workspace.current, 0)
        self.assertFalse(workspace.playing)
        self.assertEqual(workspace.project.clips, [])


if __name__ == "__main__":
    unittest.main()
