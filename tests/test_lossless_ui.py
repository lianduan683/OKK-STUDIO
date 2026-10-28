from __future__ import annotations

import ast
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lossless_detect as detect  # noqa: E402
import lossless_edl as edl  # noqa: E402
import lossless_ffmpeg as lf  # noqa: E402
import lossless_runner as lr  # noqa: E402
import lossless_workspace as lw  # noqa: E402
from lossless_core import CutSegment, SegmentModel  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


class _Var:
    def __init__(self, _root=None, value="", **_kwargs) -> None:
        self.value = value

    def get(self):
        return self.value

    def set(self, value) -> None:
        self.value = value


class _App:
    def __init__(self) -> None:
        self.caps = SimpleNamespace(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe")
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.working = False
        self.editor_video_var = _Var()
        self.editor_paused = True
        self.progress_var = _Var(0.0)
        self.progress_text_var = _Var("就绪")
        self.mpv = None
        self.logs: list[str] = []
        self.progress_labels: list[str] = []
        self.process_result = (0, "")
        self.process_hook = None
        self.commands: list[list[str]] = []

    def _set_working(self, working: bool) -> None:
        self.working = working

    def _begin_shared_progress(self, label: str) -> None:
        self.progress_labels.append(label)
        self.progress_text_var.set(label)

    def _run_editor_process(self, command, report=None):
        command = list(command)
        self.commands.append(command)
        if self.process_hook is not None:
            result = self.process_hook(command, report)
            if result is not None:
                return result
        return self.process_result

    def _log(self, detail: str) -> None:
        self.logs.append(detail)

    def _mpv_command_async(self, _command, **_kwargs) -> None:
        return None

    def _open_folder(self, _path: str) -> None:
        return None


def _workspace(app: _App) -> lw.LosslessWorkspace:
    with (
        patch.object(lw.tk, "StringVar", _Var),
        patch.object(lw.tk, "BooleanVar", _Var),
    ):
        return lw.LosslessWorkspace(app)


def _probe(path: str, duration: float = 10.0) -> lf.MediaProbe:
    return lf.parse_probe_json(
        path,
        {
            "streams": [
                {
                    "index": 0,
                    "codec_name": "h264",
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "30/1",
                    "duration": str(duration),
                },
                {
                    "index": 1,
                    "codec_name": "aac",
                    "codec_type": "audio",
                    "channels": 2,
                    "sample_rate": "48000",
                    "tags": {"language": "zho"},
                },
                {"index": 2, "codec_name": "subrip", "codec_type": "subtitle"},
                {
                    "index": 3,
                    "codec_name": "ttf",
                    "codec_type": "attachment",
                    "tags": {"filename": "font.ttf"},
                },
            ],
            "format": {"filename": path, "duration": str(duration)},
        },
    )


def _wait_for_event(app: _App, kind: str, timeout: float = 5.0) -> tuple[list[tuple[str, object]], object]:
    events: list[tuple[str, object]] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = app.events.get(timeout=max(0.01, deadline - time.monotonic()))
        except queue.Empty:
            break
        events.append(event)
        if event[0] == kind:
            return events, event[1]
    raise AssertionError(f"等待事件 {kind} 超时，已收到：{[item[0] for item in events]}")


def _method_map(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    result = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    result[item.name] = item
    return result


def _is_stub(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    statements = list(node.body)
    if statements and isinstance(statements[0], ast.Expr) and isinstance(statements[0].value, ast.Constant):
        if isinstance(statements[0].value.value, str):
            statements.pop(0)
    return not statements or all(
        isinstance(item, ast.Pass)
        or (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant) and item.value.value is Ellipsis)
        for item in statements
    )


class LosslessSourceTests(unittest.TestCase):
    def test_every_workspace_button_targets_an_implemented_method(self) -> None:
        tree = ast.parse((ROOT / "lossless_workspace.py").read_text(encoding="utf-8"))
        methods = _method_map(tree)
        callbacks: set[str] = set()
        labels: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "Button":
                continue
            for keyword in node.keywords:
                if keyword.arg == "text" and isinstance(keyword.value, ast.Constant):
                    labels.add(str(keyword.value.value))
                if keyword.arg != "command":
                    continue
                for child in ast.walk(keyword.value):
                    if (
                        isinstance(child, ast.Attribute)
                        and isinstance(child.value, ast.Name)
                        and child.value.id == "self"
                    ):
                        callbacks.add(child.attr)
        missing = sorted(name for name in callbacks if name not in methods)
        stubs = sorted(name for name in callbacks if name in methods and _is_stub(methods[name]))
        self.assertEqual(missing, [])
        self.assertEqual(stubs, [])
        self.assertTrue(
            {
                "执行片段导出",
                "提取勾选轨道",
                "混流外部轨并导出",
                "检测黑场",
                "检测静音",
                "检测场景",
                "按间隔抽帧",
                "只保留关键帧 decimate",
                "改变 FPS（重编码）",
                "HLS 无损封装为 MKV",
            }.issubset(labels)
        )

    def test_build_function_reuses_workspace_instead_of_recreating_model(self) -> None:
        tree = ast.parse((ROOT / "lossless_workspace.py").read_text(encoding="utf-8"))
        methods = _method_map(tree)
        self.assertIn("build_lossless_tab", methods)
        self.assertFalse(_is_stub(methods["build_lossless_tab"]))

    def test_exporter_toolbox_player_and_event_loop_are_connected(self) -> None:
        source = (ROOT / "rtx5090_exporter.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = _method_map(tree)

        toolbox = ast.get_source_segment(source, methods["_build_editor"])
        self.assertIn('text="  无损分段  "', toolbox)
        self.assertIn("self.lossless_workspace.build(self.lossless_panel)", toolbox)
        drain = ast.get_source_segment(source, methods["_drain_events"])
        if "_dispatch_event" in methods:
            drain += "\n" + ast.get_source_segment(source, methods["_dispatch_event"])
        self.assertIn("self.lossless_workspace.open_media(info.path)", drain)
        integration = (ROOT / 'studio_integration.py').read_text(encoding='utf-8')
        for event, handler in (
            ("lossless_media", "handle_media"),
            ("lossless_media_error", "handle_media_error"),
            ("lossless_detection", "handle_detection"),
            ("lossless_job_done", "handle_job_done"),
            ("lossless_job_error", "handle_job_error"),
        ):
            self.assertIn(event, integration)
            self.assertIn(f"self.lossless_workspace.{handler}", integration)
        self.assertIn("self.lossless_workspace.on_player_time(self.editor_current_time)", drain)
        close = ast.get_source_segment(source, methods["_close"])
        self.assertIn("self.lossless_workspace.close()", close)

    def test_keyboard_shortcuts_are_widget_local_not_global_entry_hooks(self) -> None:
        global_shortcuts: list[str] = []
        local_shortcuts: list[str] = []
        for filename in ("rtx5090_exporter.py", "lossless_workspace.py"):
            source = (ROOT / filename).read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in {"bind", "bind_all"} or not node.args:
                    continue
                sequence = node.args[0]
                if not isinstance(sequence, ast.Constant) or not isinstance(sequence.value, str):
                    continue
                if "Key" not in sequence.value:
                    continue
                rendered = f"{filename}:{node.lineno} {ast.get_source_segment(source, node)}"
                if node.func.attr == "bind_all":
                    global_shortcuts.append(rendered)
                else:
                    local_shortcuts.append(rendered)
        self.assertEqual(global_shortcuts, [])
        self.assertTrue(local_shortcuts)
        self.assertTrue(all("editor_join_timeline_canvas.bind" in item for item in local_shortcuts))


class LosslessWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="lossless_ui_test_")
        self.root = Path(self.temporary.name)
        self.source = self.root / "源 素材.mkv"
        self.source.write_bytes(b"source")
        self.app = _App()
        self.workspace = _workspace(self.app)
        self.errors = Mock()
        self.workspace._error = self.errors
        self.workspace.state.source = str(self.source)
        self.workspace.state.probe = _probe(str(self.source))
        self.workspace.state.model = SegmentModel(
            [CutSegment(id="whole", start=0.0, end=10.0, name="全片", selected=True)],
            media_duration=10.0,
        )
        self.workspace.state.active_id = "whole"
        self.workspace.state.auto_save = False
        self.workspace.output_dir_var.set(str(self.root / "输出"))
        self.workspace.merged_output_var.set(str(self.root / "合并.mkv"))
        self.workspace.utility_source_var.set(str(self.source))
        self.workspace.utility_output_var.set(str(self.root / "工具.mkv"))

    def tearDown(self) -> None:
        self.errors.assert_not_called()
        self.temporary.cleanup()

    def test_dock_rebuild_preserves_workspace_and_model_identity(self) -> None:
        self.workspace.build = Mock()
        self.app.lossless_workspace = self.workspace
        first = lw.build_lossless_tab(self.app, object())
        model = self.workspace.state.model
        second = lw.build_lossless_tab(self.app, object())

        self.assertIs(first, self.workspace)
        self.assertIs(second, self.workspace)
        self.assertIs(second.state.model, model)
        self.assertEqual(second.state.model.get("whole").name, "全片")
        self.assertEqual(self.workspace.build.call_count, 2)

    def test_separate_merge_and_both_all_reach_plan_runner(self) -> None:
        for mode in ("separate", "merge", "both"):
            with self.subTest(mode=mode):
                self.workspace.mode_var.set(mode)
                plan = SimpleNamespace(
                    segment_outputs=(str(self.root / f"{mode}-part.mkv"),),
                    merged_output=str(self.root / f"{mode}-merged.mkv") if mode != "separate" else None,
                    warnings=("test warning",),
                    cleanup_paths=(),
                )
                self.workspace._run_plan = Mock()
                with patch.object(lw.lf, "plan_lossless_export", return_value=plan) as factory:
                    self.workspace.export_cut_plan()

                factory.assert_called_once()
                self.assertEqual(factory.call_args.kwargs["mode"], mode)
                self.assertEqual(factory.call_args.args[1], str(self.source))
                self.workspace._run_plan.assert_called_once_with(plan, "无损片段导出", 10.0)

    def test_plan_runner_reports_progress_and_returns_outputs(self) -> None:
        plan = Mock()

        def run(received_plan, *, cancel_event, progress, log):
            self.assertIs(received_plan, plan)
            self.assertIs(cancel_event, self.app.cancel_event)
            self.assertTrue(callable(log))
            progress(lr.LosslessProgress(
                command_index=1, command_count=2, fraction=0.5,
                overall_fraction=0.25, fps=60.0, speed=2.0,
            ))
            return SimpleNamespace(outputs=("part.mkv", "merged.mkv"))

        with patch("lossless_runner.run_lossless_export", side_effect=run) as runner:
            self.workspace._run_plan(plan, "测试导出", 4.0)
            events, payload = _wait_for_event(self.app, "lossless_job_done")

        runner.assert_called_once()
        progress = [item[1] for item in events if item[0] == "editor_export_progress"]
        self.assertEqual(len(progress), 1)
        overall, stage, item_percent, fps, speed = progress[0]
        self.assertEqual((overall, item_percent), (25.0, 50.0))
        self.assertEqual(stage, "测试导出 1/2")
        self.assertEqual((fps, speed), ("60.0", "2.0x"))
        generation, job_id, label, outputs = payload
        self.assertEqual(generation, self.workspace.state.generation)
        self.assertTrue(job_id)
        self.assertEqual(label, "测试导出")
        self.assertEqual(outputs, ["part.mkv", "merged.mkv"])

    def test_plan_runner_cancellation_reaches_cancelled_ui_state(self) -> None:
        def cancel(*_args, **_kwargs):
            self.app.cancel_event.set()
            raise lr.LosslessExportCancelled("导出已取消")

        with patch("lossless_runner.run_lossless_export", side_effect=cancel):
            self.workspace._run_plan(Mock(), "取消导出", 4.0)
            _events, payload = _wait_for_event(self.app, "lossless_job_error")

        self.workspace.handle_job_error(payload)
        self.assertFalse(self.app.working)
        self.assertEqual(self.app.progress_text_var.get(), "已取消")
        self.errors.assert_not_called()

    def test_command_runner_reports_overall_progress_and_completion(self) -> None:
        output = self.root / "progress.mkv"
        output.write_bytes(b"output")

        def process(_command, report):
            report({"out_time_us": "500000", "fps": "60", "speed": "2x"})

        self.app.process_hook = process
        self.workspace._start_commands("进度测试", [["ffmpeg"]], [2.0], [str(output)])

        events, payload = _wait_for_event(self.app, "lossless_job_done")
        generation, job_id, label, outputs = payload
        self.assertEqual(generation, self.workspace.state.generation)
        self.assertTrue(job_id)
        self.assertEqual(label, "进度测试")
        self.assertEqual(outputs, [str(output)])
        progress = [item[1] for item in events if item[0] == "editor_export_progress"]
        self.assertEqual(len(progress), 1)
        overall, stage, item_percent, fps, speed = progress[0]
        self.assertAlmostEqual(overall, 25.0)
        self.assertEqual(stage, "进度测试 1/1")
        self.assertAlmostEqual(item_percent, 25.0)
        self.assertEqual((fps, speed), ("60", "2x"))

        self.workspace.handle_job_done(payload)
        self.assertFalse(self.app.working)
        self.assertEqual(self.app.progress_var.get(), 100)
        self.assertEqual(self.app.progress_text_var.get(), "进度测试完成")

    def test_command_runner_cancellation_reaches_cancelled_ui_state(self) -> None:
        output = self.root / "cancel.mkv"
        output.write_bytes(b"output")
        started = threading.Event()
        release = threading.Event()

        def process(_command, _report):
            started.set()
            self.assertTrue(release.wait(5.0))

        self.app.process_hook = process
        cleanup = self.root / "cleanup.ffmetadata"
        cleanup.write_text("temporary", encoding="utf-8")
        self.workspace._start_commands(
            "取消测试", [["ffmpeg"]], [1.0], [str(output)], cleanup_paths=[str(cleanup)]
        )
        self.assertTrue(started.wait(5.0))
        self.app.cancel_event.set()
        release.set()

        _events, payload = _wait_for_event(self.app, "lossless_job_error")
        self.workspace.handle_job_error(payload)

        self.assertFalse(self.app.working)
        self.assertEqual(self.app.progress_text_var.get(), "已取消")
        deadline = time.monotonic() + 2.0
        while cleanup.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(cleanup.exists())
        self.assertTrue(any("取消" in item for item in self.app.logs))

    def test_open_media_automatically_loads_matching_sidecar_llc(self) -> None:
        llc_path = self.source.with_suffix(".llc")
        edl.save_llc(
            llc_path,
            self.source.name,
            [CutSegment(id="from-llc", start=1.0, end=4.0, name="旁路片段", selected=True)],
            media_duration=10.0,
        )
        media = _probe(str(self.source))
        keyframes = lf.KeyframeIndex(stream_index=0, frames=(lf.Keyframe(time=0.0),))

        with (
            patch.object(lw.lf, "probe_media", return_value=media),
            patch.object(lw.lf, "probe_keyframes", return_value=keyframes),
        ):
            self.workspace.open_media(str(self.source))
            _events, payload = _wait_for_event(self.app, "lossless_media")
            self.workspace.handle_media(payload)

        self.assertEqual([item.id for item in self.workspace.state.model.segments], ["from-llc"])
        self.assertEqual(self.workspace.state.model.get("from-llc").name, "旁路片段")
        self.assertIs(self.workspace.state.keyframes, keyframes)
        self.assertIn("已自动加载", self.workspace.summary_var.get())
        self.assertIn(llc_path.name, self.workspace.summary_var.get())

    def test_track_selection_toggle_extract_and_external_mux_are_wired(self) -> None:
        self.workspace.state.track_selection = {stream.index: True for stream in self.workspace.state.probe.streams}
        self.workspace.track_tree = SimpleNamespace(selection=lambda: ("1",))
        self.workspace._refresh_tracks = Mock()
        self.workspace.toggle_tree_tracks()
        self.assertFalse(self.workspace.state.track_selection[1])
        selection = self.workspace._selection()
        self.assertEqual(selection.video, (0,))
        self.assertEqual(selection.audio, ())
        self.workspace.set_all_tracks(True)
        self.assertEqual(self.workspace._selection().audio, (1,))

        self.workspace.state.track_selection = {0: True, 1: False, 2: False, 3: False}
        self.workspace._start_commands = Mock()
        with patch.object(lw.lf, "build_extract_command", return_value=["extract"]) as build:
            self.workspace.extract_tracks()
        self.assertEqual(build.call_args.args[3], "video")
        self.assertEqual(build.call_args.kwargs["stream"], 0)
        self.assertEqual(self.workspace._start_commands.call_args.args[0], "提取轨道")

        subtitle = self.root / "外部.srt"
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
        self.workspace.external_path_var.set(str(subtitle))
        self.workspace.external_kind_var.set("subtitle")
        self.workspace.external_language_var.set("zho")
        self.workspace.external_forced_var.set(True)
        self.workspace.add_external()
        self.assertEqual(len(self.workspace.state.external_tracks), 1)
        self.workspace._start_commands = Mock()
        with patch.object(lw.lf, "build_external_mux_command", return_value=["mux"]) as build:
            self.workspace.mux_external()
        self.assertIs(build.call_args.args[2], self.workspace.state.external_tracks)
        self.assertEqual(self.workspace._start_commands.call_args.args[0], "外部轨混流")

    def test_black_silence_and_scene_detection_use_host_runner_and_parsers(self) -> None:
        cases = {
            "black": "black_start:1 black_end:2 black_duration:1",
            "silence": "silence_start: 3\nsilence_end: 4 | silence_duration: 1",
            "scene": "[Parsed_showinfo_1 @ 0x1] n:0 pts:1 pts_time:5 duration:1",
        }
        for kind, log in cases.items():
            with self.subTest(kind=kind):
                self.app.process_result = (0, log)
                self.workspace.start_detection(kind)
                self.assertTrue(self.app.working)
                _events, payload = _wait_for_event(self.app, "lossless_detection")
                self.workspace.handle_detection(payload)

                self.assertFalse(self.app.working)
                self.assertEqual(self.workspace.state.detection_kind, kind)
                if kind == "black":
                    self.assertEqual([(item.start, item.end) for item in self.workspace.state.detection_ranges], [(1.0, 2.0)])
                elif kind == "silence":
                    self.assertEqual([(item.start, item.end) for item in self.workspace.state.detection_ranges], [(3.0, 4.0)])
                else:
                    self.assertEqual(self.workspace.state.detection_cuts, [5.0])
                self.assertEqual(self.app.progress_text_var.get(), "检测完成")

    def test_detection_results_feed_segment_operations(self) -> None:
        state = self.workspace.state
        state.detection_kind = "black"
        state.detection_ranges = [detect.TimeRange(2.0, 3.0)]
        self.workspace.add_detected_segments()
        self.assertTrue(any(item.name == "黑场" for item in state.model.segments))

        state.model = SegmentModel(
            [CutSegment(id="whole", start=0.0, end=10.0, selected=True)], media_duration=10.0
        )
        self.workspace.subtract_detected()
        self.assertEqual([(item.start, item.end) for item in state.model.segments], [(0.0, 2.0), (3.0, 10.0)])

        state.detection_cuts = [5.0]
        self.workspace.split_detected_scenes()
        self.assertEqual(
            [(item.start, item.end) for item in state.model.segments],
            [(0.0, 2.0), (3.0, 5.0), (5.0, 10.0)],
        )

    def test_utility_actions_build_commands_and_enter_shared_runner(self) -> None:
        cases = (
            ("run_loop", lw.lf, "build_loop_command", "loop 流复制"),
            ("run_decimate", lw.lf, "build_keyframe_only_command", "关键帧 decimate"),
            ("run_fps", lw.lf, "build_fps_change_command", "FPS 重编码"),
            ("run_hls", lw.lf, "build_hls_to_mkv_command", "HLS 转 MKV"),
            ("export_interval_frames", lw.lf, "build_interval_stills_command", "间隔抽帧"),
        )
        for method_name, module, builder_name, label in cases:
            with self.subTest(action=method_name):
                if method_name == "run_hls":
                    self.workspace.utility_source_var.set("https://example.invalid/live.m3u8")
                    self.workspace.utility_output_var.set(str(self.root / "live.mkv"))
                else:
                    self.workspace.utility_source_var.set(str(self.source))
                    self.workspace.utility_output_var.set(str(self.root / f"{method_name}.mkv"))
                self.workspace._start_commands = Mock()
                with patch.object(module, builder_name, return_value=[method_name]) as builder:
                    getattr(self.workspace, method_name)()
                builder.assert_called_once()
                args = self.workspace._start_commands.call_args.args
                self.assertEqual(args[0], label)
                self.assertEqual(args[1], [[method_name]])

        frame_output = self.root / "当前帧.png"
        self.workspace.frame_format_var.set("png")
        self.workspace._start_commands = Mock()
        with (
            patch.object(lw.filedialog, "asksaveasfilename", return_value=str(frame_output)),
            patch.object(lw.detect, "build_thumbnail_command", return_value=["thumbnail"]) as builder,
        ):
            self.workspace.export_current_frame()
        builder.assert_called_once()
        self.assertEqual(self.workspace._start_commands.call_args.args[0], "导出当前帧")
        self.assertEqual(self.workspace._start_commands.call_args.args[1], [["thumbnail"]])


class LosslessWorkspaceRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="lossless_ui_regression_")
        self.root = Path(self.temporary.name)
        self.source = self.root / "源 素材.mkv"
        self.source.write_bytes(b"source")
        self.app = _App()
        self.workspace = _workspace(self.app)
        self.errors = Mock()
        self.workspace._error = self.errors
        self.workspace.state.source = str(self.source)
        self.workspace.state.probe = _probe(str(self.source))
        self.workspace.state.model = SegmentModel(
            [CutSegment(id="whole", start=0.0, end=10.0, name="全片", selected=True)],
            media_duration=10.0,
        )
        self.workspace.state.active_id = "whole"
        self.workspace.state.auto_save = False
        self.workspace.output_dir_var.set(str(self.root / "输出"))
        self.workspace.merged_output_var.set(str(self.root / "合并.mkv"))
        self.workspace.utility_source_var.set(str(self.source))
        self.workspace.utility_output_var.set(str(self.root / "工具.mkv"))

    def tearDown(self) -> None:
        self.errors.assert_not_called()
        self.temporary.cleanup()

    def test_reset_session_clears_previous_media_and_transient_state(self) -> None:
        state = self.workspace.state
        state.track_selection = {0: True, 1: False}
        state.external_tracks = [lf.ExternalTrack(path=str(self.source))]
        state.detection_kind = "black"
        state.detection_ranges = [detect.TimeRange(1.0, 2.0)]
        state.detection_cuts = [3.0]
        state.current_time = 4.0
        self.workspace.playback_items = (state.model.segments[0],)
        self.workspace.playback_index = 0
        self.workspace.playback_loop = True
        generation = state.generation

        self.workspace.reset_session()

        self.assertGreater(state.generation, generation)
        self.assertEqual(state.source, "")
        self.assertIsNone(state.probe)
        self.assertIsNone(state.keyframes)
        self.assertEqual(state.model.segments, ())
        self.assertIsNone(state.active_id)
        self.assertEqual(state.current_time, 0.0)
        self.assertEqual(state.track_selection, {})
        self.assertEqual(state.external_tracks, [])
        self.assertEqual(state.detection_kind, "")
        self.assertEqual(state.detection_ranges, [])
        self.assertEqual(state.detection_cuts, [])
        self.assertEqual(self.workspace.playback_items, ())
        self.assertEqual(self.workspace.playback_index, -1)
        self.assertFalse(self.workspace.playback_loop)
        self.assertEqual(self.workspace.source_var.get(), "尚未打开素材")
        self.assertIn("打开视频", self.workspace.summary_var.get())

    def test_project_switch_clears_cuts_and_rejects_old_probe_result(self) -> None:
        import project_file

        self.app.lossless_workspace = self.workspace
        self.app.editor_join_files = []
        self.app.editor_join_alignments = {}
        self.app.editor_timeline_transitions = {}
        old_generation = self.workspace.state.generation
        old_probe = self.workspace.state.probe
        old_segments = list(self.workspace.state.model.segments)

        project_file.apply_state(self.app, {"state": {"lossless": {
            "source": "", "auto_save": True,
        }}})

        state = self.workspace.state
        self.assertEqual(state.source, "")
        self.assertIsNone(state.probe)
        self.assertEqual(state.model.segments, ())
        self.assertGreater(state.generation, old_generation)
        self.workspace.handle_media((
            old_generation, old_probe, None, old_segments, None, "", "", False,
            state.edit_generation,
        ))
        self.assertIsNone(state.probe)
        self.assertEqual(state.model.segments, ())

    def test_offline_project_cannot_overwrite_its_sidecar_after_reconnect(self) -> None:
        import project_file

        self.app.lossless_workspace = self.workspace
        self.app.editor_join_files = []
        self.app.editor_join_alignments = {}
        self.app.editor_timeline_transitions = {}
        source_b = self.root / "离线素材.mkv"
        sidecar_b = source_b.with_suffix(".llc")
        edl.save_llc(sidecar_b, source_b.name,
                     [CutSegment(start=2, end=4, name="素材B原片段")], media_duration=10)
        original = sidecar_b.read_bytes()

        warnings = project_file.apply_state(self.app, {"state": {"lossless": {
            "source": str(source_b), "auto_save": True,
        }}})
        self.assertTrue(any("离线" in warning for warning in warnings))
        self.assertEqual(self.workspace.state.model.segments, ())
        self.assertIsNone(self.workspace.state.probe)
        source_b.write_bytes(b"reconnected source")
        self.workspace.state.model.add(1, 3, name="读取前编辑")
        self.workspace._changed()

        self.assertEqual(sidecar_b.read_bytes(), original)
        self.assertIn("尚未读取完成", self.workspace.state.llc_save_error)

    def test_reprobe_completion_keeps_edits_made_while_probing(self) -> None:
        media = _probe(str(self.source))
        keyframes = lf.KeyframeIndex(stream_index=0, frames=(lf.Keyframe(time=0.0),))
        started = threading.Event()
        release = threading.Event()

        def slow_probe(_ffprobe, _source):
            started.set()
            self.assertTrue(release.wait(5.0))
            return media

        with (
            patch.object(lw.lf, "probe_media", side_effect=slow_probe),
            patch.object(lw.lf, "probe_keyframes", return_value=keyframes),
        ):
            self.workspace.open_media(str(self.source), preserve=True)
            self.assertTrue(started.wait(5.0))
            self.workspace.state.model.add(5.0, 6.0, name="探测期间新增", selected=True)
            self.workspace._changed(save=False)
            release.set()
            _events, payload = _wait_for_event(self.app, "lossless_media")
            self.workspace.handle_media(payload)

        names = [segment.name for segment in self.workspace.state.model.segments]
        self.assertIn("探测期间新增", names)
        self.assertIn("全片", names)
        self.assertIs(self.workspace.state.probe, media)
        self.assertIn("保留探测期间的新编辑", self.workspace.summary_var.get())

    def test_auto_save_with_empty_model_keeps_existing_llc_visible(self) -> None:
        llc_path = self.source.with_suffix(".llc")
        edl.save_llc(
            llc_path, self.source.name,
            [CutSegment(start=1.0, end=4.0, name="不能丢", selected=True)],
            media_duration=10.0,
        )
        before = llc_path.read_text(encoding="utf-8")
        self.workspace.state.model = SegmentModel([], media_duration=10.0)
        self.workspace.state.auto_save = True
        self.workspace.view = SimpleNamespace(winfo_exists=lambda: True)

        self.workspace._changed()

        self.assertEqual(llc_path.read_text(encoding="utf-8"), before)
        self.assertTrue(self.workspace.state.llc_save_error)
        self.assertIn("自动保存 LLC 失败", self.workspace.summary_var.get())
        with self.assertRaises(ValueError):
            self.workspace.save_llc(automatic=True)
        self.assertEqual(llc_path.read_text(encoding="utf-8"), before)

        self.workspace.state.model = SegmentModel(
            [CutSegment(start=0.0, end=10.0, selected=True)], media_duration=10.0
        )
        self.workspace.save_llc(automatic=True)
        self.assertEqual(self.workspace.state.llc_save_error, "")

    def test_manual_save_can_replace_existing_llc_with_empty_model(self) -> None:
        llc_path = self.source.with_suffix(".llc")
        edl.save_llc(
            llc_path, self.source.name,
            [CutSegment(start=1.0, end=4.0, name="旧片段", selected=True)],
            media_duration=10.0,
        )
        self.workspace.state.model = SegmentModel([], media_duration=10.0)

        self.workspace.save_llc()

        self.assertEqual(edl.load_document(llc_path, "llc").segments, [])

    def test_stale_job_events_do_not_clear_a_newer_job_state(self) -> None:
        generation = self.workspace.state.generation
        _generation, old_job = self.workspace._begin_job()
        _generation, new_job = self.workspace._begin_job()
        self.app._set_working(True)

        self.workspace.handle_job_done((generation, old_job, "旧任务", ["old.mkv"]))
        self.assertTrue(self.app.working)
        self.workspace.handle_job_error((generation, old_job, "旧任务", "旧错误"))
        self.assertTrue(self.app.working)
        self.errors.assert_not_called()

        self.workspace.handle_job_done((generation, new_job, "新任务", ["new.mkv"]))
        self.assertFalse(self.app.working)
        self.assertIn("新任务", self.app.progress_text_var.get())
        self.assertIn("新任务完成", self.workspace.summary_var.get())

        self.app._set_working(True)
        self.workspace.handle_detection(
            (generation, old_job, "black", 0, "black_start:1 black_end:2 black_duration:1")
        )
        self.assertTrue(self.app.working)
        self.assertEqual(self.workspace.state.detection_ranges, [])

    def test_detection_callback_after_unmount_does_not_touch_dead_tree(self) -> None:
        class DeadTree:
            def winfo_exists(self):
                raise lw.tk.TclError("widget destroyed")

            def _unexpected(self, *_args, **_kwargs):
                raise AssertionError("已销毁的检测列表不应被访问")

            delete = _unexpected
            get_children = _unexpected
            insert = _unexpected

        self.workspace.detect_tree = DeadTree()
        self.workspace.unmount()
        payload = (
            self.workspace.state.generation,
            "black",
            0,
            "black_start:1 black_end:2 black_duration:1",
        )
        self.workspace.handle_detection(payload)

        self.assertFalse(self.app.working)
        self.assertEqual([(item.start, item.end) for item in self.workspace.state.detection_ranges], [(1.0, 2.0)])

    def test_utility_failure_removes_only_new_partial_output(self) -> None:
        partial = self.root / "半成品.mkv"
        preexisting = self.root / "已有成片.mkv"
        preexisting.write_bytes(b"keep")

        def fail_with_partial(command, _report):
            if command[-1] == str(partial):
                partial.write_bytes(b"partial")
            return 1, "boom"

        self.app.process_hook = fail_with_partial
        self.workspace._start_commands("失败测试", [["ffmpeg", str(partial)]], [1.0], [str(partial)])
        _events, payload = _wait_for_event(self.app, "lossless_job_error")
        self.assertFalse(partial.exists())
        self.app._set_working(False)

        def fail(_command, _report):
            return 1, "boom"

        self.app.process_hook = fail
        self.workspace._start_commands("失败测试", [["ffmpeg", str(preexisting)]], [1.0], [str(preexisting)])
        _events, payload = _wait_for_event(self.app, "lossless_job_error")
        self.assertEqual(preexisting.read_bytes(), b"keep")
        self.app._set_working(False)

    def test_utility_cancellation_removes_new_partial_output(self) -> None:
        partial = self.root / "取消半成品.mkv"
        started = threading.Event()
        release = threading.Event()

        def slow(_command, _report):
            partial.write_bytes(b"half")
            started.set()
            self.assertTrue(release.wait(5.0))
            return 1, "进程被取消"

        self.app.process_hook = slow
        self.workspace._start_commands("取消测试", [["ffmpeg", str(partial)]], [1.0], [str(partial)])
        self.assertTrue(started.wait(5.0))
        self.app.cancel_event.set()
        release.set()
        _events, payload = _wait_for_event(self.app, "lossless_job_error")
        self.assertFalse(partial.exists())
        self.app._set_working(False)

    def test_sync_options_flushes_controls_and_survives_unmount(self) -> None:
        self.workspace.mode_var.set("both")
        self.workspace.extension_var.set(".mp4")
        self.workspace.snap_var.set(False)
        self.workspace.template_var.set("{name}_{index:02d}")

        state = self.workspace.sync_options()

        self.assertIs(state, self.workspace.state)
        self.assertEqual(self.workspace.state.output_mode, "both")
        self.assertEqual(self.workspace.state.extension, ".mp4")
        self.assertFalse(self.workspace.state.snap_start)
        self.assertEqual(self.workspace.state.name_template, "{name}_{index:02d}")

        self.workspace.unmount()
        self.workspace.snap_var.set(True)
        self.workspace.sync_options()
        self.assertTrue(self.workspace.state.snap_start)

    def test_chapter_generation_matches_export_snap_setting(self) -> None:
        self.workspace.snap_var.set(True)
        self.workspace.state.snap_start = True
        keyframes = lf.KeyframeIndex(stream_index=0, frames=(lf.Keyframe(time=0.0),))
        self.workspace.state.keyframes = keyframes
        chapters = (lf.ChapterInfo(index=1, start=0.5, end=9.0, tags={"title": "章节"}),)
        self.workspace._start_commands = Mock()
        with (
            patch.object(lw.lf, "chapters_from_segments", return_value=chapters) as builder,
            patch.object(lw.lf, "write_chapters_file", return_value=Path("chapters.ffmetadata")),
            patch.object(lw.lf, "build_segments_chapters_command", return_value=["chapters"]),
        ):
            self.workspace.export_chapters()

        self.assertTrue(builder.call_args.kwargs["snap_start"])
        self.assertIs(builder.call_args.kwargs["keyframes"], keyframes)
        self.assertEqual(self.workspace._start_commands.call_args.args[0], "写入章节")


class RealTkSmokeTests(unittest.TestCase):
    def test_withdrawn_real_tk_builds_all_pages_and_reuses_model(self) -> None:
        try:
            root = lw.tk.Tk()
            root.withdraw()
        except lw.tk.TclError as exc:
            self.skipTest(f"真实 Tk 环境不可用：{exc}")
        app = _App()
        app._root = root._root
        app.tk = root.tk
        app.lossless_subtab = 0
        workspace = lw.LosslessWorkspace(app)
        first = lw.ttk.Frame(root)
        try:
            workspace.build(first)
            root.update_idletasks()
            workspace.state.model.add(0.0, 1.0, segment_id="persistent", selected=True)
            model = workspace.state.model
            self.assertEqual(len(workspace.notebook.tabs()), 5)

            workspace.unmount()
            first.destroy()
            second = lw.ttk.Frame(root)
            workspace.build(second)

            self.assertIs(workspace.state.model, model)
            self.assertEqual(workspace.state.model.get("persistent").end, 1.0)
            self.assertEqual(len(workspace.notebook.tabs()), 5)
            root.update_idletasks()
            workspace.close()
            second.destroy()
            root.update_idletasks()
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
