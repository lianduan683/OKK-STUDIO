"""Preview orchestration tests without a GPU, window handles or MPV process.

The fake preserves pause/seek/time semantics, rather than asserting private
implementation steps.  Public commands still run on the real player worker.
"""
from __future__ import annotations

import copy
import queue
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import multicam_player as player_module
from multicam_core import CameraClip, Cut, MulticamProject
from multicam_player import MulticamPlayer


class _Trace:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.instances = []
        self.stop_gate = threading.Event()
        self.stop_gate.set()
        self.cleanup_started = threading.Event()

    def record(self, kind, window, payload=None):
        with self.lock:
            self.calls.append((kind, window, payload, threading.get_ident()))

    def snapshot(self):
        with self.lock:
            return list(self.calls)


def _controller_type(trace):
    class FakeMpv:
        def __init__(self, executable, window_id):
            self.window = window_id
            self.process = None
            self.path = ""
            self.position = 0.0
            self.paused = True
            self.speed = 1.0
            self.anchor = time.monotonic()
            with trace.lock:
                trace.instances.append(self)
            trace.record("create", self.window)

        def _position(self):
            return self.position + (0.0 if self.paused else (time.monotonic() - self.anchor)*self.speed)

        def start(self):
            trace.record("start", self.window)

        def load(self, path):
            trace.record("load", self.window, path)
            if Path(path).name.startswith("bad"):
                raise RuntimeError("GPU decoder unavailable; generate a proxy")
            self.path = path
            self.position = 0.0
            self.anchor = time.monotonic()
            self.paused = True

        def command(self, command, **kwargs):
            trace.record("command", self.window, tuple(command))
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
            elif command[:2] == ["set_property", "speed"]:
                self.position = self._position()
                self.anchor, self.speed = time.monotonic(), float(command[2])
            elif command[:2] == ["get_property", "time-pos"]:
                return self._position()
            elif command[:2] == ["get_property", "duration"]:
                return 60.0
            elif command[:2] == ["get_property", "eof-reached"]:
                return False
            elif command[:2] == ["get_property", "idle-active"]:
                return False
            return None

        def stop(self):
            trace.record("dispose", self.window)
            trace.cleanup_started.set()
            trace.stop_gate.wait(timeout=3.0)

    return FakeMpv


class MulticamPlayerTests(unittest.TestCase):
    def setUp(self):
        self.trace = _Trace()
        fake = _controller_type(self.trace)
        self.patches = [patch.object(player_module, "MpvController", fake),
                        patch.object(player_module, "_AudioController", fake),
                        patch.object(MulticamPlayer, "TICK", 0.01)]
        for item in self.patches:
            item.start()
        self.events = queue.Queue()
        self.received = []
        self.player = MulticamPlayer("mpv.exe", {1: 101, 2: 102}, 200, 300, self.events)

    def tearDown(self):
        self.trace.stop_gate.set()
        self.player.close()
        stopped = self.player.wait_closed(3.0)
        for item in reversed(self.patches):
            item.stop()
        self.assertTrue(stopped, "preview worker leaked after closing")

    def _wait(self, predicate, message="condition did not become true", timeout=2.0):
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
        self.fail(f"{message}; events={self.received[-8:]}")

    def _state(self, predicate):
        before = len(self.received)
        self._wait(lambda: any(kind == "state" and predicate(value)
                              for kind, value in self.received[before:]),
                   "expected preview state was not emitted")

    def _loads(self, window=None):
        return [call for call in self.trace.snapshot()
                if call[0] == "load" and (window is None or call[1] == window)]

    def _commands(self, window=None):
        return [call for call in self.trace.snapshot()
                if call[0] == "command" and (window is None or call[1] == window)]

    def _configure(self, project, **kwargs):
        self.player.submit("configure", project=project, **kwargs)
        self._state(lambda state: state["ready"])

    @staticmethod
    def _project():
        return MulticamProject(
            clips=[CameraClip("a", 1, "camera-a.mp4", 0.0, 10.0),
                   CameraClip("b", 2, "camera-b.mp4", 0.0, 10.0)],
            cuts=[Cut(0.0, 1), Cut(8.0, 2)],
            master_audio="master.wav", master_duration=10.0,
        )

    def test_reconfigure_preserves_running_clock_and_master_audio(self):
        project = self._project()
        self._configure(project)
        self.player.submit("play")
        self._state(lambda state: state["playing"] and state["time"] > 0.02)
        before = len(self.trace.snapshot())
        updated = copy.deepcopy(project)
        updated.cuts[-1].time = 9.0
        self.player.submit("configure", project=updated)
        self._wait(lambda: self.player._project.cuts[-1].time == 9.0)
        self._state(lambda state: state["playing"] and state["time"] > 0.06)
        after = self.trace.snapshot()[before:]
        self.assertFalse([call for call in after if call[0] == "load"])
        self.assertFalse([call for call in after
                          if call[0] == "command" and call[2] == ("set_property", "pause", True)])
        self.assertEqual(1, len([call for call in self._commands(300)
                                 if call[2][0] == "loadfile"]))
        self.assertFalse(self._loads(300), "master sound must not use GPU video load()")
        self.assertIn(("set_property", "vid", "no"), [call[2] for call in self._commands(300)])
        for window in (101, 102, 200):
            self.assertIn(("set_property", "mute", True), [call[2] for call in self._commands(window)])
        self.assertIn(("set_property", "mute", False), [call[2] for call in self._commands(300)])
        worker = self.player._thread.ident
        self.assertTrue(all(call[3] == worker for call in self.trace.snapshot()),
                        "MPV creation or communication escaped the worker")

    def test_seek_follows_cut_and_recovers_camera_after_black_gap(self):
        project = self._project()
        project.clips[0].duration = 1.0
        project.cuts = [Cut(0.0, 1), Cut(0.5, 2)]
        self._configure(project)
        first_camera = next(instance for instance in self.trace.instances if instance.window == 101)
        self.player.submit("seek", time=0.75)
        self._state(lambda state: state["camera"] == 2 and state["time"] == 0.75)
        self.assertEqual("camera-b.mp4", self._loads(200)[-1][2])
        self.assertIn(("seek", 0.75, "absolute+exact"), [call[2] for call in self._commands(200)])
        self.player.submit("seek", time=1.25)
        self._state(lambda state: state["time"] == 1.25)
        self.assertIn(("stop",), [call[2] for call in self._commands(101)])
        self.assertFalse([call for call in self.trace.snapshot() if call[0] == "dispose"])
        self.player.submit("seek", time=0.25)
        self._state(lambda state: state["camera"] == 1 and state["time"] == 0.25)
        self.assertEqual(2, len(self._loads(101)))
        self.assertIs(first_camera, next(instance for instance in self.trace.instances if instance.window == 101))
        self.assertEqual("camera-a.mp4", self._loads(200)[-1][2])

    def test_running_program_switches_at_cut_and_audition_can_return_to_follow(self):
        project = self._project()
        project.cuts = [Cut(0.0, 1), Cut(0.15, 2)]
        self._configure(project)
        self.player.submit("play")
        self._state(lambda state: state["playing"] and state["camera"] == 2)
        self.assertEqual("camera-b.mp4", self._loads(200)[-1][2])
        self.player.submit("audition", camera=1)
        self._state(lambda state: state["camera"] == 1 and state["playing"])
        self.assertEqual("camera-a.mp4", self._loads(200)[-1][2])
        self.player.submit("audition", camera=None)
        self._state(lambda state: state["camera"] == 2 and state["playing"])
        self.assertEqual("camera-b.mp4", self._loads(200)[-1][2])

    def test_auto_proxy_discovery_preserves_source_time_and_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "camera-a.MOV"
            proxy = source.parent / "ProxyMedia" / "camera-a.mp4"
            proxy.parent.mkdir()
            proxy.write_bytes(b"fake proxy")
            project = self._project()
            project.clips[0].path = str(source)
            project.clips[0].start = -1.5
            self._configure(project)
            self.assertEqual(str(proxy), self._loads(101)[-1][2])
            self.player.submit("seek", time=0.4)
            self._state(lambda state: state["time"] == 0.4)
            source_seeks = [call[2][1] for call in self._commands(101) if call[2][0] == "seek"]
            self.assertAlmostEqual(1.9, source_seeks[-1])
            self.player.submit("configure", project=project, use_proxies=False)
            self._wait(lambda: self._loads(101)[-1][2] == str(source))
            explicit = Path(directory) / "explicit-proxy.mp4"
            explicit.write_bytes(b"explicit proxy")
            project.clips[0].proxy_path = str(explicit)
            self.player.submit("configure", project=project, use_proxies=True)
            self._wait(lambda: self._loads(101)[-1][2] == str(explicit))
            # An explicitly selected missing proxy falls back to its original;
            # it does not silently substitute the automatically found proxy.
            project.clips[0].proxy_path = str(Path(directory) / "missing.mp4")
            self.player.submit("configure", project=project)
            self._wait(lambda: self._loads(101)[-1][2] == str(source))

    def test_failed_video_is_reported_once_per_window_and_not_reloaded_each_tick(self):
        project = self._project()
        project.clips[0].path = "bad-codec.mp4"
        self.player.submit("configure", project=project)
        self._wait(lambda: len([call for call in self._loads() if call[2] == "bad-codec.mp4"]) == 2)
        self._state(lambda state: not state["ready"])
        before = len(self._loads())
        self.player.submit("configure", project=project)
        self.player.submit("play")
        self._state(lambda state: state["playing"] and state["time"] > 0.05)
        self.assertEqual(before, len(self._loads()))
        errors = [value for kind, value in self.received if kind == "error"]
        self.assertEqual(2, len(errors))
        self.assertTrue(all("GPU decoder unavailable" in error for error in errors))

    def test_close_returns_immediately_while_worker_finishes_process_cleanup(self):
        self.assertFalse(self.player._thread.daemon)
        self._configure(self._project())
        self.trace.stop_gate.clear()
        began = time.monotonic()
        self.player.close()
        self.assertLess(time.monotonic() - began, 0.05)
        self.assertTrue(self.trace.cleanup_started.wait(1.0))
        self.assertFalse(self.player.wait_closed(0.01), "close unexpectedly waited for blocked MPV cleanup")
        self.trace.stop_gate.set()
        self.assertTrue(self.player.wait_closed(2.0))
        disposed = [call[1] for call in self.trace.snapshot() if call[0] == "dispose"]
        self.assertCountEqual([101, 102, 200, 300], disposed)
        calls = len(self.trace.snapshot())
        self.player.submit("seek", time=1.0)
        self.assertEqual(calls, len(self.trace.snapshot()))

    def test_python_exit_waits_for_signalled_worker_cleanup(self):
        # The child deliberately does not call wait_closed().  Its main thread
        # exits immediately after close(), as the real Tk shutdown path does.
        script = textwrap.dedent("""\
            import queue, sys, time
            import multicam_player as module
            from multicam_core import CameraClip, MulticamProject

            class FakeMpv:
                def __init__(self, executable, window):
                    self.window, self.process = window, None
                def start(self):
                    pass
                def load(self, path):
                    pass
                def command(self, command, **kwargs):
                    return 0.0 if command[:2] == ['get_property', 'time-pos'] else None
                def stop(self):
                    time.sleep(0.1)
                    with open(sys.argv[1], 'a', encoding='utf-8') as output:
                        output.write(str(self.window) + '\\n')

            module.MpvController = FakeMpv
            events = queue.Queue()
            player = module.MulticamPlayer('mpv.exe', {1: 101}, 200, 300, events)
            player.submit('configure', project=MulticamProject(
                clips=[CameraClip('a', 1, 'camera.mp4', 0.0, 10.0)]))
            while True:
                kind, state = events.get(timeout=2.0)
                if kind == 'state' and state['ready']:
                    break
            player.close()
            """)
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "cleanup.txt"
            result = subprocess.run(
                [sys.executable, "-c", script, str(marker)],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True, timeout=5.0,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(marker.is_file(), "Python exited before MPV cleanup ran")
            self.assertCountEqual(["101", "200"], marker.read_text(encoding="utf-8").splitlines())


if __name__ == "__main__":
    unittest.main()
