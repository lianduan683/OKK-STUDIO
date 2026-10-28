"""Programme cuts retain the visible decoder until the replacement is ready."""
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from creative_core import ColorGrade
from multicam_core import CameraClip, Cut, MulticamProject
import multicam_player as player_module
from multicam_player import MulticamPlayer
try:
    from .test_multicam_player import _Trace, _controller_type
except ImportError:
    from test_multicam_player import _Trace, _controller_type


class DoubleBufferTests(unittest.TestCase):
    def setUp(self):
        self.trace = _Trace()
        self.gate = threading.Event()
        self.gate.set()
        trace, gate = self.trace, self.gate
        base = _controller_type(trace)

        class Controller(base):
            def wait_for_frame(self, target, **kwargs):
                trace.record("frame_wait", self.window, target)
                while not gate.wait(.01):
                    if kwargs.get("cancel_event") is not None and kwargs["cancel_event"].is_set():
                        return
                trace.record("frame_ready", self.window, target)

        self.patches = [patch.object(player_module, "MpvController", Controller),
                        patch.object(player_module, "_AudioController", Controller),
                        patch.object(MulticamPlayer, "TICK", .01)]
        for item in self.patches:
            item.start()
        self.events = queue.Queue()
        self.player = MulticamPlayer("mpv", {1: 101, 2: 102}, 200, 300, self.events,
                                     program_standby_window=201)
        self.project = MulticamProject(clips=[CameraClip("a", 1, "red.mp4", 0, 8),
                                             CameraClip("b", 2, "blue.mp4", 0, 8)],
                                       cuts=[Cut(0, 1), Cut(4, 2)], master_audio="master.wav", master_duration=8)
        self.player.submit("configure", project=self.project)
        self.wait(lambda: self.player._program.active)
        self.drain()

    def tearDown(self):
        self.gate.set()
        self.player.close()
        self.assertTrue(self.player.wait_closed(3))
        for item in reversed(self.patches):
            item.stop()

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.005)
        self.fail("double-buffer condition timed out")

    def drain(self):
        values = []
        while not self.events.empty():
            values.append(self.events.get_nowait())
        for kind, value in values:
            if kind == "program_swap":
                self.player.submit("program_swap_ack", window=value)
        return values

    def test_paused_audition_keeps_old_picture_until_new_frame_ready(self):
        previous = self.player._program
        before = len(self.trace.snapshot())
        self.gate.clear()
        self.player.submit("audition", camera=2)
        self.wait(lambda: any(call[0] == "frame_wait" and call[1] == 201 for call in self.trace.snapshot()))
        self.assertIs(self.player._program, previous)
        self.assertFalse(any(kind == "program_swap" for kind, _ in self.drain()))
        calls = self.trace.snapshot()[before:]
        self.assertFalse(any(kind == "load" and window == 200 for kind, window, _, _ in calls))
        self.assertFalse(any(kind == "command" and window == 200 and payload[0] == "stop"
                             for kind, window, payload, _ in calls))
        self.gate.set()
        self.wait(lambda: self.player._program.window == 201)
        self.assertIn(("program_swap", 201), self.drain())
        self.assertTrue(self.player._program.controller.paused)
        self.assertEqual(self.player._program.controller.path, "blue.mp4")

    def test_running_switch_does_not_pause_reload_or_seek_master_audio(self):
        self.player.submit("play")
        self.wait(lambda: self.player._playing and not self.player._audio.controller.paused)
        time.sleep(.03)
        before = len(self.trace.snapshot())
        previous = self.player._program
        self.gate.clear()
        self.player.submit("audition", camera=2)
        self.wait(lambda: any(call[0] == "frame_wait" and call[1] == 201 for call in self.trace.snapshot()))
        self.assertFalse(previous.controller.paused)
        self.assertFalse(self.player._audio.controller.paused)
        time.sleep(.12)
        self.gate.set()
        self.wait(lambda: self.player._program.window == 201)
        # The first rendered frame is published immediately; only the video
        # then catches up. It must converge without repeating a GOP seek.
        deadline = time.monotonic()+1.5
        self.wait(lambda: abs(self.player._program.controller._position() - self.player._audio.controller._position()) < .1
                  or time.monotonic() >= deadline)
        calls = self.trace.snapshot()[before:]
        audio_commands = [payload for kind, window, payload, _ in calls if kind == "command" and window == 300]
        self.assertFalse(any(payload[0] in {"seek", "loadfile", "stop"} or payload == ("set_property", "pause", True)
                             for payload in audio_commands), audio_commands)
        self.assertEqual(len([payload for kind, window, payload, _ in calls
                              if kind == 'command' and window == 201 and payload[0] == 'seek']), 1)
        self.assertLess(abs(self.player._program.controller._position() - self.player._audio.controller._position()), .1)

    def test_failed_replacement_preserves_working_programme(self):
        self.project.clips[1].path = "bad-camera.mp4"
        self.player.submit("configure", project=self.project)
        self.player.submit("audition", camera=2)
        self.wait(lambda: self.player._program_standby.failed_key is not None)
        self.assertEqual(self.player._program.controller.path, "red.mp4")
        self.assertTrue(self.player._program.active)
        self.assertFalse(any(kind == "program_swap" for kind, _ in self.drain()))

    def test_close_cleans_both_programme_controllers(self):
        self.player.submit("audition", camera=2)
        self.wait(lambda: self.player._program.window == 201)
        self.player.close()
        self.assertTrue(self.player.wait_closed(3))
        disposed = {window for kind, window, _, _ in self.trace.snapshot() if kind == "dispose"}
        self.assertTrue({200, 201}.issubset(disposed))

    def test_slow_nine_camera_page_follows_cut_before_all_thumbnails_finish(self):
        self.project.clips.extend(CameraClip(f'camera-{camera}', camera, f'camera-{camera}.mp4', 0., 8.)
                                  for camera in range(3, 21))
        self.player.submit('configure', project=self.project)
        self.player.submit('seek', time=3.8)
        self.wait(lambda: abs(self.player._position-3.8) < .001)
        self.drain()
        self.player.submit('play')
        self.wait(lambda: self.player._playing and not self.player._audio.controller.paused)
        before = len(self.trace.snapshot())
        controller_type = type(self.player._program.controller)
        original_load = controller_type.load
        def slow_thumbnail(controller, path):
            if controller.window >= 1100:
                time.sleep(.08)
            return original_load(controller, path)
        with patch.object(controller_type, 'load', slow_thumbnail):
            self.player.submit('camera_windows', windows={camera: 1100+camera for camera in range(10, 19)})
            self.wait(lambda: self.player._program.controller.path == 'blue.mp4')
            self.assertLess(sum(slot.active for slot in self.player._slots.values()), 9,
                            'programme cut waited for every camera on the new page')
            self.assertFalse(self.player._audio.controller.paused)
            self.drain()
            self.wait(lambda: len(self.player._slots) == 9 and all(slot.active for slot in self.player._slots.values()))
        audio_commands = [payload for kind, window, payload, _ in self.trace.snapshot()[before:]
                          if kind == 'command' and window == 300]
        self.assertFalse(any(payload[0] in {'seek', 'loadfile', 'stop'} or
                             payload == ('set_property', 'pause', True)
                             for payload in audio_commands), audio_commands)
        self.assertEqual(self.project.cuts, [Cut(0, 1), Cut(4, 2)])

    def test_unacknowledged_ui_swap_never_reuses_still_visible_old_buffer(self):
        self.player.submit("audition", camera=2)
        self.wait(lambda: self.player._program.window == 201)
        before = len(self.trace.snapshot())
        self.player.submit("audition", camera=1)
        time.sleep(.08)
        self.assertEqual(self.player._program.window, 201)
        self.assertFalse(any(kind == "load" and window == 200
                             for kind, window, _, _ in self.trace.snapshot()[before:]))
        self.drain()  # UI raises 201 and acknowledges that the old one is free.
        self.wait(lambda: self.player._program.window == 200)


@unittest.skipUnless(os.environ.get("OKK_TEST_MPV_GPU") == "1", "set OKK_TEST_MPV_GPU=1 for real GPU handoff")
class DoubleBufferGpuTests(unittest.TestCase):
    def test_pause_and_play_switch_only_publish_coloured_rendered_frames(self):
        import tkinter as tk
        app = Path(__file__).resolve().parents[1]
        ffmpeg, mpv = app / "bin" / "ffmpeg.exe", app / "bin" / "mpv.exe"
        root = tk.Tk()
        root.title("双缓冲 GPU 切换验证")
        root.geometry("340x230+20+20")
        surface = tk.Frame(root, width=320, height=180, bg="black")
        surface.pack()
        fronts = [tk.Frame(surface, bg="black") for _ in range(2)]
        for item in fronts:
            item.place(x=0, y=0, relwidth=1, relheight=1)
        fronts[0].lift()
        root.update()
        events = queue.Queue()
        samples = []
        with tempfile.TemporaryDirectory(prefix="double_buffer_pixels_") as folder:
            folder = Path(folder)
            sources = []
            for color in ("red", "blue"):
                path = folder / f"{color}.mp4"
                subprocess.run([str(ffmpeg), "-v", "error", "-y", "-f", "lavfi", "-i",
                                f"color={color}:s=320x180:r=30:d=4", "-c:v", "libx264", "-preset", "ultrafast", str(path)],
                               capture_output=True, check=True, timeout=20)
                sources.append(path)
            player = MulticamPlayer(str(mpv), {}, fronts[0].winfo_id(), 0, events,
                                    program_standby_window=fronts[1].winfo_id())
            try:
                project = MulticamProject(clips=[CameraClip("a", 1, str(sources[0]), 0, 4),
                                                 CameraClip("b", 2, str(sources[1]), 0, 4)],
                                           cuts=[Cut(0, 1), Cut(1.5, 2)], fps=30)

                def poll_until(predicate, limit=8):
                    deadline = time.monotonic() + limit
                    while time.monotonic() < deadline:
                        root.update()
                        while not events.empty():
                            kind, value = events.get_nowait()
                            if kind == "program_swap":
                                slot = player._program
                                self.assertEqual(slot.window, value)
                                image = folder / f"swap{len(samples)}.png"
                                slot.controller.command(["screenshot-to-file", str(image), "video"])
                                pixels = tk.PhotoImage(master=root, file=str(image))
                                pixel = tuple(pixels.get(160, 90))
                                samples.append({"window": value, "pixel": pixel,
                                                "time": player._position,
                                                "hwdec": slot.controller.command(["get_property", "hwdec-current"])})
                                self.assertGreater(max(pixel), 150, "unrendered black frame was published")
                                next(item for item in fronts if item.winfo_id() == value).lift()
                                player.submit("program_swap_ack", window=value)
                        if predicate():
                            return
                        time.sleep(.005)
                    self.fail("real GPU handoff timed out")

                player.submit("configure", project=project)
                poll_until(lambda: len(samples) == 1)
                self.assertGreater(samples[-1]["pixel"][0], 200)
                first = player._program
                player.submit("audition", camera=2)
                poll_until(lambda: len(samples) == 2)
                self.assertGreater(samples[-1]["pixel"][2], 200)
                self.assertTrue(first.active, "old frame was discarded during handoff")
                player.submit("audition", camera=1)
                poll_until(lambda: len(samples) == 3)
                player.submit("play")
                poll_until(lambda: player._playing)
                player.submit("audition", camera=2)
                poll_until(lambda: len(samples) == 4)
                self.assertGreater(samples[-1]["pixel"][2], 200)
                player.submit("audition", camera=None)
                poll_until(lambda: len(samples) == 5)
                poll_until(lambda: len(samples) == 6)
                self.assertGreater(samples[-1]["pixel"][2], 200, "automatic programme cut failed")
                self.assertTrue(all(sample["hwdec"] in {"d3d11va", "nvdec"} for sample in samples))
                (app.parent / "program_double_buffer_gpu_validation.json").write_text(
                    json.dumps({"swaps": samples, "black_frames_published": 0,
                                "paused_and_playing_checked": True, "automatic_cut_checked": True}, ensure_ascii=False, indent=2), encoding="utf-8")
            finally:
                player.close()
                player.wait_closed(5)
                root.destroy()


if __name__ == "__main__":
    unittest.main()
