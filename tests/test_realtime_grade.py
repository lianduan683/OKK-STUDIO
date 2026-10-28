"""Live shader controls and project-layer routing, independent of Tk widgets."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from creative_core import ColorGrade, grade_filter
from multicam_core import CameraClip, Cut, MulticamProject
import multicam_player as player_module
from multicam_player import MulticamPlayer
from program_edits import set_grade_on_range
from realtime_grade import CONTROLS, RealtimeGrade, grade_uniforms, shader_source, validate_preview_target
from test_multicam_player import _Trace, _controller_type


class _Controller:
    def __init__(self):
        self.calls = []

    def command(self, command):
        self.calls.append(command)


class RealtimeShaderTests(unittest.TestCase):
    def test_fixed_dynamic_shader_and_updated_uniforms_never_seek_or_reload(self):
        controller = _Controller()
        engine = RealtimeGrade()
        self.addCleanup(engine.close)
        engine.apply(controller, [ColorGrade()])
        self.assertFalse(controller.calls)
        engine.apply(controller, [ColorGrade(exposure=-1)])
        shader_path = Path(controller.calls[-1][-1])
        initial_stat = shader_path.stat().st_mtime_ns
        for amount in (-.2, .2, .4, .8):
            engine.apply(controller, [ColorGrade(exposure=amount)])
        self.assertEqual(shader_path.stat().st_mtime_ns, initial_stat)
        self.assertEqual(sum(command[0] == "change-list" for command in controller.calls), 1)
        self.assertTrue(all(command[0] in {"set_property", "change-list"} for command in controller.calls))
        self.assertIn("//!TYPE DYNAMIC float", shader_path.read_text(encoding="utf-8"))
        before = len(controller.calls)
        engine.apply(controller, [ColorGrade(exposure=.8)])
        self.assertEqual(len(controller.calls), before)
        engine.apply(controller, [ColorGrade(exposure=.8)], bypass=True)
        self.assertEqual(controller.calls[-1][2]["g0_enabled"], "0")

    def test_all_basic_and_wheel_controls_have_distinct_three_layer_uniforms(self):
        self.assertEqual(len(CONTROLS), 24)
        values, notices = grade_uniforms([ColorGrade(exposure=.2), ColorGrade(gamma_y=.1), ColorGrade(saturation=.5)])
        self.assertEqual((values["g0_exposure"], values["g1_gamma_y"], values["g2_saturation"]), (.2, .1, .5))
        self.assertFalse(notices)
        text = shader_source()
        for layer in range(3):
            for name in CONTROLS:
                self.assertIn(f"//!PARAM g{layer}_{name}\n", text)
        values, notices = grade_uniforms([ColorGrade(lut_path="look.cube")])
        self.assertIn("LUT", notices[0])
        _, notices = grade_uniforms([ColorGrade(lut_path="look.cube", enabled=False)])
        self.assertFalse(notices)

    def test_bad_targets_and_grades_are_rejected(self):
        for target in (("camera", True), ("selection", 2, 1), ("selection", False, 1), ("global", 1), None):
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_preview_target(target)
        with self.assertRaises(ValueError):
            grade_uniforms([ColorGrade(gain_y=5)])

    def test_curve_knots_become_dynamic_coefficients_in_each_layer(self):
        curve = ((0., 0.), (.3, .5), (.7, .8), (1., 1.))
        grade = ColorGrade(curve_r=curve)
        values, _ = grade_uniforms([grade])
        self.assertEqual(values["g0_enabled"], 1)
        self.assertEqual(values["g0_custom_curves"], 1)
        self.assertEqual(values["g0_curve_r_count"], 3)
        self.assertEqual(values["g1_custom_curves"], 0)
        controller, engine = _Controller(), RealtimeGrade()
        self.addCleanup(engine.close)
        for amount in (.35, .4, .45, .5):
            engine.apply(controller, [ColorGrade(curve_r=((0, 0), (.3, amount), (1, 1)))])
        self.assertEqual(sum(command[0] == "change-list" for command in controller.calls), 1)


class RealtimePlayerTests(unittest.TestCase):
    def setUp(self):
        self.trace = _Trace()
        fake = _controller_type(self.trace)
        self.patches = [patch.object(player_module, "MpvController", fake),
                        patch.object(player_module, "_AudioController", fake),
                        patch.object(MulticamPlayer, "TICK", .01)]
        for item in self.patches:
            item.start()
        self.events = queue.Queue()
        self.player = MulticamPlayer("mpv", {1: 101, 2: 102}, 200, 300, self.events)
        self.project = MulticamProject(clips=[CameraClip("a", 1, "a.mp4", 0, 8),
                                             CameraClip("b", 2, "b.mp4", 0, 8)],
                                       cuts=[Cut(0, 1), Cut(4, 2)], fps=30)
        self.player.submit("configure", project=self.project)
        self.wait(lambda: self.player._program.active and all(slot.active for slot in self.player._slots.values())
                  and len([call for call in self.trace.snapshot() if call[0] == "load"]) == 3)

    def tearDown(self):
        self.player.close()
        self.assertTrue(self.player.wait_closed(3))
        for item in reversed(self.patches):
            item.stop()

    def wait(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.005)
        self.fail("player condition timed out")

    def uniforms(self, window):
        commands = [payload for kind, target, payload, _ in self.trace.snapshot()
                    if kind == "command" and target == window and payload[:2] == ("set_property", "glsl-shader-opts")]
        return commands[-1][2] if commands else {}

    def movement(self):
        return [(kind, target, payload) for kind, target, payload, _ in self.trace.snapshot()
                if kind == "load" or kind == "command" and payload[0] in {"seek", "loadfile"}]

    def test_camera_draft_changes_only_that_camera_and_its_current_program(self):
        before = self.movement()
        self.player.submit("grade_preview", target=("camera", 1), grade=ColorGrade(exposure=-1))
        self.wait(lambda: self.uniforms(200).get("g0_exposure") == "-1")
        self.assertEqual(self.uniforms(101)["g0_exposure"], "-1")
        self.assertFalse(self.uniforms(102))
        self.assertEqual(self.movement(), before)
        self.assertEqual(self.project.camera_grades, {}, "draft mutated caller's project")
        self.player.submit("grade_preview", target=("camera", 1), grade=None)
        self.wait(lambda: self.uniforms(101).get("g0_enabled") == "0")

    def test_selection_draft_and_saved_grade_stay_inside_half_open_range(self):
        self.player.submit("seek", time=1)
        self.wait(lambda: any(call[0] == "command" and call[1] == 200
                             and call[2] == ("seek", 1.0, "absolute+exact") for call in self.trace.snapshot()))
        before = self.movement()
        self.player.submit("grade_preview", target=("selection", 0, 2), grade=ColorGrade(gain_r=.2))
        self.wait(lambda: self.uniforms(200).get("g1_gain_r") == "0.2")
        self.assertEqual(self.movement(), before)
        self.assertFalse(self.uniforms(101))
        self.assertFalse(self.uniforms(102))
        self.player.submit("seek", time=2)
        self.wait(lambda: self.uniforms(200).get("g1_enabled") == "0")
        set_grade_on_range(self.project, 0, 2, ColorGrade(exposure=-.5))
        self.player.submit("configure", project=self.project)
        self.player.submit("seek", time=1)
        self.wait(lambda: self.uniforms(200).get("g1_exposure") == "-0.5")
        self.assertEqual(self.uniforms(200)["g1_gain_r"], "0", "configure failed to clear draft")

    def test_rendered_preview_bypasses_baked_grades_and_first_draft_returns_to_raw_once(self):
        self.project.global_grade = ColorGrade(exposure=-1)
        self.player.submit("configure", project=self.project)
        self.wait(lambda: self.uniforms(200).get("g2_enabled") == "1")
        self.player.submit("render_preview", path="baked.mp4", start=0, end=3, play=False)
        self.wait(lambda: self.uniforms(200).get("g2_enabled") == "0")
        self.player.submit("grade_preview", target=("global",), grade=ColorGrade(exposure=1))
        self.wait(lambda: self.uniforms(200).get("g2_exposure") == "1")
        self.assertIsNone(self.player._render_preview)
        self.assertEqual(self.player._position, 0)
        before = self.movement()
        for value in (.5, .3, .2):
            self.player.submit("grade_preview", target=("global",), grade=ColorGrade(exposure=value))
        self.wait(lambda: self.uniforms(200).get("g2_exposure") == "0.2")
        self.assertEqual(self.movement(), before, "continuous draft reloaded the raw decoder")

    def test_drag_queue_coalesces_and_reaches_last_value_without_reload(self):
        before = self.movement()
        for number in range(100):
            self.player.submit("grade_preview", target=("selection", 0, 2), grade=ColorGrade(exposure=number / 100))
        self.wait(lambda: self.uniforms(200).get("g1_exposure") == "0.99")
        self.assertEqual(self.movement(), before)
        updates = [call for call in self.trace.snapshot() if call[0] == "command"
                   and call[1] == 200 and call[2][:2] == ("set_property", "glsl-shader-opts")]
        self.assertLess(len(updates), 20, "stale drag events were not coalesced")


@unittest.skipUnless(os.environ.get("OKK_TEST_MPV_GPU") == "1", "set OKK_TEST_MPV_GPU=1 for paused GPU pixels")
class RealtimeMpvPixelsTests(unittest.TestCase):
    def test_paused_gpu_frame_responds_to_all_controls_without_reload_seek_or_decode_fallback(self):
        import tkinter as tk
        from mpv_controller import MpvController
        folder = Path(__file__).resolve().parents[1]
        ffmpeg, mpv = folder / "bin" / "ffmpeg.exe", folder / "bin" / "mpv.exe"
        if not ffmpeg.is_file() or not mpv.is_file():
            self.skipTest("bundled MPV/FFmpeg unavailable")
        root = tk.Tk()
        root.title("GPU 调色暂停帧验证")
        root.geometry("340x230+20+20")
        self.addCleanup(root.destroy)
        frame = tk.Frame(root, width=320, height=180, bg="black")
        frame.pack()
        root.update()
        with tempfile.TemporaryDirectory(prefix="realtime_pixels_") as temporary:
            path = Path(temporary)
            source = path / "source.mp4"

            def command(args):
                result = subprocess.run(args, capture_output=True, timeout=25)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace")[-2000:])
                return result.stdout

            command([str(ffmpeg), "-v", "error", "-y", "-f", "lavfi", "-i",
                     "color=c=0x3c5064:s=320x180:r=30:d=2", "-c:v", "libx264", "-preset", "ultrafast", str(source)])
            controller = MpvController(str(mpv), frame.winfo_id())
            engine = RealtimeGrade()
            try:
                controller.start()
                backend = controller.load(str(source))
                self.assertIn(backend["hwdec"], {"nvdec", "d3d11va"})
                position = controller.command(["get_property", "time-pos"])
                calls = []
                original_command = controller.command

                def traced(payload, **kwargs):
                    calls.append(payload)
                    return original_command(payload, **kwargs)

                controller.command = traced
                screenshot = path / "frame.png"

                def pixel():
                    root.update()
                    time.sleep(.035)
                    controller.command(["screenshot-to-file", str(screenshot), "video"])
                    raw = command([str(ffmpeg), "-v", "error", "-i", str(screenshot), "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"])
                    offset = (90 * 320 + 160) * 3
                    return tuple(raw[offset:offset + 3])

                neutral = pixel()
                settings = {"exposure": -.7, "contrast": 1.4, "saturation": .3, "temperature": .6,
                            "tint": .5, "gamma": 1.4, "shadows": .5, "highlights": -.5}
                settings.update({name: .15 for name in CONTROLS if name not in settings})
                maximum_error = 0
                for name, value in settings.items():
                    with self.subTest(control=name):
                        grade = ColorGrade(**{name: value})
                        engine.apply(controller, [grade])
                        actual = pixel()
                        self.assertNotEqual(actual, neutral, f"{name} did not change the paused GPU frame")
                        raw = command([str(ffmpeg), "-v", "error", "-i", str(source), "-frames:v", "1",
                                       "-vf", grade_filter(grade), "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"])
                        offset = (90 * 320 + 160) * 3
                        expected = tuple(raw[offset:offset + 3])
                        error = max(abs(a - b) for a, b in zip(actual, expected))
                        maximum_error = max(maximum_error, error)
                        self.assertLessEqual(error, 4, (name, actual, expected))
                curve_a = ((0., 0.), (.3, .45), (.7, .8), (1., 1.))
                curve_b = ((0., 0.), (.4, .25), (1., 1.))
                curve_grades = [ColorGrade(curve_master=curve_a), ColorGrade(curve_r=curve_b),
                                ColorGrade(curve_g=curve_a, curve_b=curve_b, curve_master=curve_a)]
                curve_error = 0
                for index, grade in enumerate(curve_grades):
                    with self.subTest(curves=index):
                        engine.apply(controller, [grade])
                        actual = pixel()
                        raw = command([str(ffmpeg), "-v", "error", "-i", str(source), "-frames:v", "1",
                                       "-vf", grade_filter(grade), "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"])
                        offset = (90 * 320 + 160) * 3
                        expected = tuple(raw[offset:offset + 3])
                        error = max(abs(a - b) for a, b in zip(actual, expected))
                        curve_error = max(curve_error, error)
                        self.assertLessEqual(error, 4, (index, actual, expected))
                self.assertFalse(any(payload[0] in {"seek", "loadfile"} for payload in calls))
                self.assertEqual(sum(payload[0] == "change-list" for payload in calls), 1)
                self.assertTrue(controller.command(["get_property", "pause"]))
                self.assertEqual(controller.command(["get_property", "time-pos"]), position)
                self.assertEqual(controller.command(["get_property", "hwdec-current"]), backend["hwdec"])
                ipc_ms, visible_ms = [], []
                previous_pixel = actual
                for index in range(20):
                    began = time.perf_counter()
                    engine.apply(controller, [ColorGrade(exposure=-.5 + index * .05)])
                    ipc_ms.append((time.perf_counter() - began) * 1000)
                    deadline = time.perf_counter() + .4
                    while time.perf_counter() < deadline:
                        controller.command(["screenshot-to-file", str(screenshot), "video"])
                        image = tk.PhotoImage(master=root, file=str(screenshot))
                        current_pixel = tuple(image.get(160, 90))
                        if current_pixel != previous_pixel:
                            break
                    self.assertNotEqual(current_pixel, previous_pixel)
                    visible_ms.append((time.perf_counter() - began) * 1000)
                    previous_pixel = current_pixel

                def percentile(samples, value):
                    return round(sorted(samples)[round((len(samples) - 1) * value)], 3)

                result = {"mpv_version": controller.command(["get_property", "mpv-version"]),
                          "backend": backend, "controls_compared": len(settings),
                          "max_rgb_error_vs_ffmpeg": maximum_error,
                          "curve_cases_compared": len(curve_grades), "max_curve_rgb_error_vs_ffmpeg": curve_error,
                          "warm_updates": 20, "warm_ipc_p50_ms": percentile(ipc_ms, .5),
                          "warm_ipc_p95_ms": percentile(ipc_ms, .95),
                          "paused_pixel_readback_p50_ms": percentile(visible_ms, .5),
                          "paused_pixel_readback_p95_ms": percentile(visible_ms, .95),
                          "time_pos_unchanged": controller.command(["get_property", "time-pos"]) == position,
                          "pause_retained": controller.command(["get_property", "pause"]),
                          "seek_or_reload_during_updates": any(payload[0] in {"seek", "loadfile"} for payload in calls)}
                (folder.parent / "realtime_grade_gpu_validation.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print("\nPaused GPU grading:", json.dumps(result, ensure_ascii=False))
            finally:
                process = controller.process
                controller.stop()
                if process is not None:
                    process.wait(timeout=3)
                engine.close()


if __name__ == "__main__":
    unittest.main()
