"""Rendered-effect preview uses export pixels without disturbing master time."""
from __future__ import annotations

import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import multicam_player as player_module
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_player import MulticamPlayer
from subtitle_core import SubtitleCue, SubtitleStyle, SubtitleTrack


class _FakeController:
    instances = []
    calls = []

    def __init__(self, executable, window):
        self.window, self.process = window, None
        self.path, self.position, self.paused = "", 0.0, True
        self.anchor = time.monotonic()
        self.subtitle_id, self.subtitle_sequence = None, 0
        self.instances.append(self)

    def _record(self, action, payload=None):
        self.calls.append((self.window, action, payload, threading.get_ident()))

    def _position(self):
        return self.position + (0.0 if self.paused else time.monotonic() - self.anchor)

    def start(self):
        self._record("start")

    def load(self, path):
        self._record("load", path)
        self.path, self.position, self.paused = path, 0.0, True
        self.subtitle_id = None
        self.anchor = time.monotonic()

    def command(self, command, **kwargs):
        self._record("command", tuple(command))
        if command[0] == "loadfile":
            self.path, self.position = command[1], 0.0
            self.anchor = time.monotonic()
        elif command[0] == "seek":
            self.position, self.anchor = command[1], time.monotonic()
        elif command == ["stop"]:
            self.path, self.position, self.paused = "", 0.0, True
        elif command[:2] == ["set_property", "pause"]:
            self.position = self._position()
            self.anchor, self.paused = time.monotonic(), command[2]
        elif command[:2] == ["get_property", "time-pos"]:
            return self._position()
        elif command[:2] == ["get_property", "duration"]:
            return 60.0
        elif command[:2] == ["set_property", "sid"]:
            self.subtitle_id = None if command[2] == "no" else command[2]
        elif command[0] == "sub-add":
            self.subtitle_sequence += 1
            self.subtitle_id = self.subtitle_sequence
        elif command[0] == "sub-remove":
            if self.subtitle_id == command[1]:
                self.subtitle_id = None
        elif command[:2] == ["get_property", "sid"]:
            return self.subtitle_id
        elif command[:2] in (["get_property", "eof-reached"], ["get_property", "idle-active"]):
            return False

    def stop(self):
        self._record("dispose")


class CreativePreviewTests(unittest.TestCase):
    def setUp(self):
        _FakeController.instances = []
        _FakeController.calls = []
        self.patches = [patch.object(player_module, "MpvController", _FakeController),
                        patch.object(player_module, "_AudioController", _FakeController),
                        patch.object(MulticamPlayer, "TICK", 0.01)]
        for item in self.patches:
            item.start()
        self.events, self.received = queue.Queue(), []
        self.player = MulticamPlayer("mpv.exe", {1: 101, 2: 102}, 200, 300, self.events)
        self.project = MulticamProject(
            clips=[CameraClip("a", 1, "camera-a.mp4", 0.0, 10.0),
                   CameraClip("b", 2, "camera-b.mp4", 0.0, 10.0)],
            cuts=[Cut(0.0, 1), Cut(8.0, 2)],
            master_audio="master.wav", master_duration=10.0,
        )
        self.player.submit("configure", project=self.project)
        self._wait(lambda: self._last_state().get("ready"))

    def tearDown(self):
        self.player.close()
        stopped = self.player.wait_closed(3.0)
        for item in reversed(self.patches):
            item.stop()
        self.assertTrue(stopped, "preview worker did not close")

    def _wait(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while True:
                try:
                    self.received.append(self.events.get_nowait())
                except queue.Empty:
                    break
            if predicate():
                return
            time.sleep(0.005)
        self.fail(f"preview condition timed out; events={self.received[-8:]}")

    def _last_state(self):
        return next((value for kind, value in reversed(self.received) if kind == "state"), {})

    def _controller(self, window):
        return next(instance for instance in _FakeController.instances if instance.window == window)

    def _commands(self, window):
        return [payload for target, action, payload, _ in list(_FakeController.calls)
                if target == window and action == "command"]

    def _preview(self, **kwargs):
        self.player.submit("render_preview", path="rendered-effects.mp4", start=3.0, end=6.0,
                           play=False, **kwargs)
        self._wait(lambda: self._controller(200).path == "rendered-effects.mp4"
                   and self._last_state().get("time") == 3.0)

    def _seek(self, position):
        self.player.submit("seek", time=position)
        self._wait(lambda: self._last_state().get("time") == position)

    def test_rendered_programme_preserves_camera_audio_sources_and_timeline_offsets(self):
        self._preview()
        self.assertEqual("camera-a.mp4", self._controller(101).path)
        self.assertEqual("camera-b.mp4", self._controller(102).path)
        self.assertEqual("master.wav", self._controller(300).path)
        self.assertIn(("seek", 0.0, "absolute+exact"), self._commands(200))
        self.assertIn(("seek", 3.0, "absolute+exact"), self._commands(300))
        self.assertIn(("set_property", "mute", True), self._commands(200))
        self.assertIn(("set_property", "mute", False), self._commands(300))
        self._seek(4.25)
        self.assertEqual("rendered-effects.mp4", self._controller(200).path)
        self.assertIn(("seek", 1.25, "absolute+exact"), self._commands(200))
        for window in (101, 102, 300):
            self.assertIn(("seek", 4.25, "absolute+exact"), self._commands(window))
        self.assertTrue(all(call[3] == self.player._thread.ident for call in _FakeController.calls))

    def test_preview_range_is_half_open_and_seek_can_reenter_it(self):
        self._preview()
        self._seek(6.0)
        self.assertEqual("camera-a.mp4", self._controller(200).path)
        self.assertIn(("seek", 6.0, "absolute+exact"), self._commands(200))
        self._seek(8.25)
        self.assertEqual("camera-b.mp4", self._controller(200).path)
        self._seek(3.0)
        self.assertEqual("rendered-effects.mp4", self._controller(200).path)
        self._seek(2.99)
        self.assertEqual("camera-a.mp4", self._controller(200).path)

    def test_configure_invalidates_render_without_reloading_unchanged_master_audio(self):
        self._preview()
        self._seek(4.0)
        loads_before = sum(command[0] == "loadfile" for command in self._commands(300))
        self.player.submit("configure", project=self.project)
        self._wait(lambda: self._controller(200).path == "camera-a.mp4")
        self.assertIsNone(self.player._render_preview)
        self.assertEqual(4.0, self.player._position)
        self.assertEqual(loads_before, sum(command[0] == "loadfile" for command in self._commands(300)))

    def test_default_preview_plays_and_false_pauses_at_start(self):
        self.player.submit("render_preview", path="rendered-effects.mp4", start=3.0, end=6.0)
        self._wait(lambda: self._last_state().get("playing") and self._last_state().get("time", 0) > 3.01)
        self.player.submit("render_preview", path="rendered-effects.mp4", start=3.0, end=6.0, play=False)
        self._wait(lambda: not self._last_state().get("playing") and self._last_state().get("time") == 3.0)
        self.assertTrue(self._controller(200).paused)
        self.assertTrue(self._controller(300).paused)

    def test_running_preview_falls_back_at_range_end(self):
        self.player.submit("render_preview", path="rendered-effects.mp4", start=7.9, end=8.05)
        self._wait(lambda: self._controller(200).path == "rendered-effects.mp4")
        self._wait(lambda: self._controller(200).path == "camera-b.mp4"
                   and self._last_state().get("playing")
                   and self._last_state().get("time",0) >= 8.05)
        self.assertGreaterEqual(self._last_state()["time"], 8.05)

    def test_invalid_ranges_do_not_replace_valid_render(self):
        self._preview()
        for start, end in ((-1, 2), (3, 3), (5, 4), (float("nan"), 6), (3, float("inf")), (10, 11)):
            with self.subTest(start=start, end=end):
                # Each distinct invalid input may produce the same deduplicated
                # user-facing message. A following seek is our queue barrier.
                self.player.submit("render_preview", path="invalid.mp4", start=start, end=end)
                self.player.submit("seek", time=4.0)
                self._wait(lambda: self._last_state().get("time") == 4.0)
                self.assertEqual("rendered-effects.mp4", self._controller(200).path)
                self.assertEqual(("rendered-effects.mp4", 3.0, 6.0), self.player._render_preview)
                self._seek(3.0)
        self.assertTrue([value for kind, value in self.received if kind == "error"])

    def test_close_does_not_delete_owner_managed_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "temporary-preview.mp4"
            preview.write_bytes(b"owned by the workspace")
            self.player.submit("render_preview", path=preview, start=3.0, end=6.0, play=False)
            self._wait(lambda: self._controller(200).path == str(preview))
            self.player.close()
            self.assertTrue(self.player.wait_closed(3.0))
            self.assertEqual(b"owned by the workspace", preview.read_bytes())

    def _configure_subtitles(self):
        self.project.clips[0].start = -2.5
        self.project.clips[0].duration = 12.5
        self.project.clips[1].start = 1
        self.project.clips[1].duration = 9
        self.project.subtitles = SubtitleTrack(
            cues=[SubtitleCue("first", 2, 4, "中文字幕"), SubtitleCue("last", 8, 9, "结尾")],
            style=SubtitleStyle(color="#123456", font_size=58, outline_width=3, bold=True),
            offset=.25, burn_in=False)
        self.player.submit("configure", project=self.project)
        self._wait(lambda: bool(self.player._subtitle_ass_path)
                   and any(command[0] == "sub-add" for command in self._commands(200)))

    def test_original_programme_uses_styled_ass_on_global_clock_only(self):
        self._configure_subtitles()
        ass = Path(self.player._subtitle_ass_path)
        text = ass.read_text(encoding="utf-8-sig")
        self.assertIn("PlayResX: 1920", text)
        self.assertIn("PlayResY: 1080", text)
        self.assertIn("Microsoft YaHei,58", text)
        self.assertIn("&H00563412", text)
        self.assertIn("0:00:02.25", text)
        self.assertIn("中文字幕", text)
        self.assertIn(("set_property", "sub-delay", 2.5), self._commands(200))
        self.assertIn(("set_property", "sub-ass-override", "no"), self._commands(200))
        for window in (101, 102, 300):
            self.assertFalse([command for command in self._commands(window) if command[0] == "sub-add"])
        self._seek(8.25)
        self.assertEqual("camera-b.mp4", self._controller(200).path)
        self.assertIn(("set_property", "sub-delay", -1.0), self._commands(200))
        self.assertIn(("seek", 7.25, "absolute+exact"), self._commands(200))

    def test_style_changes_refresh_ass_without_reloading_running_camera(self):
        self._configure_subtitles()
        original_ass = self.player._subtitle_ass_path
        original_video_loads = len([call for call in _FakeController.calls
                                    if call[0] == 200 and call[1] == "load"])
        original_subtitle = self._controller(200).subtitle_id
        self.project.subtitles.style.color = "#ABCDEF"
        self.project.subtitles.style.font_size = 72
        self.player.submit("configure", project=self.project)
        self._wait(lambda: self.player._subtitle_ass_path != original_ass
                   and len([command for command in self._commands(200) if command[0] == "sub-add"]) == 2)
        changed = Path(self.player._subtitle_ass_path).read_text(encoding="utf-8-sig")
        self.assertIn("&H00EFCDAB", changed)
        self.assertIn("Microsoft YaHei,72", changed)
        self.assertIn(("sub-remove", original_subtitle), self._commands(200))
        self.assertEqual(original_video_loads, len([call for call in _FakeController.calls
                                                   if call[0] == 200 and call[1] == "load"]))

    def test_rendered_effect_excerpt_disables_external_subtitle_until_returning_to_raw(self):
        self._configure_subtitles()
        additions = len([command for command in self._commands(200) if command[0] == "sub-add"])
        self._preview()
        self.assertTrue(self._last_state()["preview"])
        self.assertIsNone(self._controller(200).subtitle_id)
        self.assertEqual(additions, len([command for command in self._commands(200) if command[0] == "sub-add"]))
        self.assertIn(("set_property", "sub-delay", 0.0), self._commands(200))
        self._seek(6.0)
        self.assertFalse(self._last_state()["preview"])
        self.assertIsNotNone(self._controller(200).subtitle_id)
        self.assertEqual(additions + 1, len([command for command in self._commands(200) if command[0] == "sub-add"]))

    def test_disabling_subtitles_hides_track_and_close_removes_generated_cache(self):
        self._configure_subtitles()
        ass = Path(self.player._subtitle_ass_path)
        self.project.subtitles.enabled = False
        self.player.submit("configure", project=self.project)
        self._wait(lambda: self.player._subtitle_ass_path == ""
                   and self._controller(200).subtitle_id is None)
        self.assertTrue(ass.exists(), "keep cached subtitle files until MPV is closed")
        self.player.close()
        self.assertTrue(self.player.wait_closed(3.0))
        self.assertFalse(ass.parent.exists())


if __name__ == "__main__":
    unittest.main()
