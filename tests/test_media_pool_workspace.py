from __future__ import annotations

import ast
import contextlib
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import media_pool_workspace as mpw  # noqa: E402
from exporter_core import MediaInfo  # noqa: E402
from media_pool_core import ALL_BIN_ID, UNGROUPED_BIN_ID  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


class _Var:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self):
        return self.value

    def set(self, value) -> None:
        self.value = value


class FakeNotebook:
    def __init__(self) -> None:
        self.selects: list[object] = []

    def select(self, target) -> None:
        self.selects.append(target)


class PoolApp:
    """模拟 ExporterApp：只提供媒体池工作台会触碰的接口。"""

    def __init__(self) -> None:
        self.caps = SimpleNamespace(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe")
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.input_var = _Var()
        self.editor_tab = "editor-frame"
        self.main_notebook = FakeNotebook()
        self.logs: list[str] = []
        self._open_folder = Mock(name="_open_folder")
        self._set_input_video = Mock(name="_set_input_video")
        self._add_join_paths = Mock(name="_add_join_paths")
        self._add_proxy_paths = Mock(name="_add_proxy_paths")
        self._load_editor_video = Mock(name="_load_editor_video")

    def _log(self, detail: str) -> None:
        self.logs.append(detail)


def _method_map(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    result: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
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


class MediaPoolSourceTests(unittest.TestCase):
    """源码级检查：按钮回调必须存在且不是 stub。"""

    def test_every_workspace_button_targets_an_implemented_method(self) -> None:
        tree = ast.parse((ROOT / "media_pool_workspace.py").read_text(encoding="utf-8"))
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
                "导入文件", "导入文件夹", "刷新",
                "移动到…", "资源管理器定位", "移除所选",
                "设为导出源", "加入拼接", "加入代理",
                "新建", "重命名", "删除",
            }.issubset(labels)
        )

    def test_probe_worker_reports_through_app_event_queue(self) -> None:
        source = (ROOT / "media_pool_workspace.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = _method_map(tree)
        worker = ast.get_source_segment(source, methods["_probe_worker"])
        self.assertIn("self.app.caps.ffprobe_path", worker)
        self.assertIn("exporter_core.probe_media(", worker)
        self.assertIn("self.app.events.put((PROBE_EVENT", worker)
        self.assertIn("self.app.events.put((PROBE_ERROR_EVENT", worker)
        self.assertIn("PROBE_EVENT = \"media_pool_probe\"", source)
        self.assertIn("PROBE_ERROR_EVENT = \"media_pool_probe_error\"", source)


class TkWorkspaceTestCase(unittest.TestCase):
    """需要真实 Tk 的用例基类；无显示环境时整类跳过。"""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"无显示环境，跳过真实 Tk 媒体池测试：{exc}")
        cls.root.withdraw()
        cls.probe_mock = cls.enterClassContext(
            patch.object(mpw.exporter_core, "probe_media")
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.root.destroy()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="media_pool_ws_test_")
        self.root_dir = Path(self.temporary.name)
        self.app = PoolApp()
        self.probe_mock.reset_mock(return_value=False, side_effect=True)
        self.probe_mock.side_effect = RuntimeError("测试中不允许真实探针")
        self.show_error = self.enterContext(patch.object(mpw.messagebox, "showerror"))
        self.ask_yesno = self.enterContext(
            patch.object(mpw.messagebox, "askyesno", return_value=True)
        )
        self.workspace = mpw.MediaPoolWorkspace(self.app, self.root)

    def tearDown(self) -> None:
        self.workspace.close()
        self.workspace.destroy()
        self.drain_events()
        self.temporary.cleanup()

    # ------------------------------------------------------------ helpers

    def drain_events(self) -> list[tuple[str, object]]:
        events: list[tuple[str, object]] = []
        while True:
            try:
                events.append(self.app.events.get_nowait())
            except queue.Empty:
                return events

    def succeed_probes_with(self, info: MediaInfo) -> None:
        """后续探针全部返回 info（放在 add_paths 之前才生效）。"""
        self.probe_mock.side_effect = None
        self.probe_mock.return_value = info

    def wait_for_event(self, kind: str, timeout: float = 5.0) -> object:
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        while time.monotonic() < deadline:
            try:
                event_kind, payload = self.app.events.get(
                    timeout=max(0.01, deadline - time.monotonic())
                )
            except queue.Empty:
                break
            seen.append(event_kind)
            if event_kind == kind:
                self.drain_events()
                return payload
        raise AssertionError(f"等待事件 {kind} 超时，已收到：{seen}")

    def make_video(self, name: str, directory: Path | None = None) -> Path:
        target = (directory or self.root_dir) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"0")
        return target

    def add_clip(self, name: str = "clip.mp4", bin_id: str | None = None) -> object:
        path = self.make_video(name)
        (added,) = self.workspace.add_paths([path], bin_id)
        return added

    def set_selection(self, *clips) -> None:
        self.workspace.clip_tree.selection_set(*(clip.key for clip in clips))

    def flush_search(self, text: str) -> None:
        self.workspace.search_var.set(text)
        self.workspace.after_cancel(self.workspace._search_after)
        self.workspace._search_after = None
        self.workspace._search_flush()


class AsyncProbeTests(TkWorkspaceTestCase):
    def make_info(self, clip) -> MediaInfo:
        return MediaInfo(path=clip.path, duration=12.5, width=1920, height=1080,
                         fps=59.94, video_codec="hevc", bit_depth=10)

    def test_probe_success_marks_clip_ready(self) -> None:
        info = self.make_info(SimpleNamespace(path=str(self.root_dir / "good.mp4")))
        self.succeed_probes_with(info)
        clip = self.add_clip("good.mp4")
        self.assertEqual(clip.status, "probing")

        payload = self.wait_for_event(mpw.PROBE_EVENT)

        self.assertEqual(payload["key"], clip.key)
        self.assertEqual(payload["token"], self.workspace._tokens[clip.key])
        self.assertEqual(payload["path"], clip.path)
        self.workspace.handle_probe(payload)
        self.assertIs(clip.info, info)
        self.assertEqual(clip.status, "ready")
        self.assertEqual(clip.error, "")
        self.assertEqual(self.workspace.count_var.get(), "共 1 个素材")

    def test_probe_failure_marks_clip_error_when_ffprobe_ready(self) -> None:
        clip = self.add_clip("broken.mp4")

        payload = self.wait_for_event(mpw.PROBE_ERROR_EVENT)

        self.assertEqual(payload["key"], clip.key)
        self.assertIn("测试中不允许真实探针", payload["error"])
        self.workspace.handle_probe_error(payload)
        self.assertEqual(clip.status, "error")
        self.assertIn("测试中不允许真实探针", clip.error)
        self.assertNotIn(clip.key, self.workspace._awaiting_capabilities)
        self.assertEqual(self.workspace.count_var.get(), "共 1 个素材 · 1 个异常")

    def test_probe_failure_without_ffprobe_waits_for_capabilities(self) -> None:
        clip = self.add_clip("wait.mp4")
        self.app.caps.ffprobe_path = ""

        payload = self.wait_for_event(mpw.PROBE_ERROR_EVENT)
        self.workspace.handle_probe_error(payload)

        self.assertEqual(clip.status, "pending")
        self.assertEqual(clip.error, "等待 ffprobe")
        self.assertIn(clip.key, self.workspace._awaiting_capabilities)

    def test_on_capabilities_retries_pending_probes(self) -> None:
        clip = self.add_clip("retry.mp4")
        self.app.caps.ffprobe_path = ""
        error_payload = self.wait_for_event(mpw.PROBE_ERROR_EVENT)
        self.workspace.handle_probe_error(error_payload)
        first_token = self.workspace._tokens[clip.key]

        self.workspace.on_capabilities()  # ffprobe 仍然缺失：不重试
        self.assertEqual(self.workspace._tokens[clip.key], first_token)
        self.assertIn(clip.key, self.workspace._awaiting_capabilities)

        self.app.caps.ffprobe_path = "ffprobe"
        info = self.make_info(clip)
        self.succeed_probes_with(info)
        self.workspace.on_capabilities()

        self.assertEqual(clip.status, "probing")
        self.assertNotIn(clip.key, self.workspace._awaiting_capabilities)
        payload = self.wait_for_event(mpw.PROBE_EVENT)
        self.assertEqual(payload["token"], first_token + 1)
        self.workspace.handle_probe(payload)
        self.assertEqual(clip.status, "ready")

    def test_stale_probe_result_is_ignored_by_token(self) -> None:
        self.succeed_probes_with(self.make_info(SimpleNamespace(path=str(self.root_dir / "token.mp4"))))
        clip = self.add_clip("token.mp4")
        stale = self.wait_for_event(mpw.PROBE_EVENT)
        self.assertEqual(stale["token"], 1)

        self.workspace._enqueue_probe(clip)  # 重新探针 → token 递增
        fresh = self.wait_for_event(mpw.PROBE_EVENT)
        self.assertEqual(fresh["token"], 2)

        self.workspace.handle_probe(stale)
        self.assertIsNone(clip.info)
        self.assertEqual(clip.status, "probing")

        self.workspace.handle_probe(fresh)
        self.assertEqual(clip.status, "ready")

    def test_tokens_are_tracked_per_path(self) -> None:
        first = self.add_clip("one.mp4")
        second = self.add_clip("two.mp4")
        self.drain_events()
        self.assertEqual(self.workspace._tokens[first.key], 1)
        self.assertEqual(self.workspace._tokens[second.key], 1)

    def test_refresh_reprobes_offline_clip_once_file_returns(self) -> None:
        info = self.make_info(SimpleNamespace(path=str(self.root_dir / "revive.mp4")))
        self.succeed_probes_with(info)
        clip = self.add_clip("revive.mp4")
        self.workspace.handle_probe(self.wait_for_event(mpw.PROBE_EVENT))
        self.assertEqual(clip.status, "ready")

        Path(clip.path).unlink()
        self.workspace.refresh()
        self.assertEqual(clip.status, "offline")

        Path(clip.path).write_bytes(b"0")  # 离线素材恢复
        token_before = self.workspace._tokens[clip.key]
        self.workspace.refresh()

        self.assertEqual(clip.status, "probing")
        self.assertEqual(self.workspace._tokens[clip.key], token_before + 1)
        payload = self.wait_for_event(mpw.PROBE_EVENT)
        self.assertEqual(payload["token"], token_before + 1)
        self.workspace.handle_probe(payload)
        self.assertEqual(clip.status, "ready")
        self.assertEqual(self.workspace.count_var.get(), "共 1 个素材")

    def test_refresh_retries_errored_clip_without_readding(self) -> None:
        clip = self.add_clip("flaky.mp4")
        self.workspace.handle_probe_error(self.wait_for_event(mpw.PROBE_ERROR_EVENT))
        self.assertEqual(clip.status, "error")
        token_before = self.workspace._tokens[clip.key]

        self.workspace.refresh()

        self.assertEqual(clip.status, "probing")
        self.assertEqual(self.workspace._tokens[clip.key], token_before + 1)
        payload = self.wait_for_event(mpw.PROBE_ERROR_EVENT)
        self.assertEqual(payload["token"], token_before + 1)

    def test_refresh_skips_clips_awaiting_capabilities(self) -> None:
        # ffprobe 未就绪时的迟到 error 必须等 on_capabilities，refresh 不能抢先探针
        clip = self.add_clip("caps_wait.mp4")
        self.app.caps.ffprobe_path = ""
        self.workspace.handle_probe_error(self.wait_for_event(mpw.PROBE_ERROR_EVENT))
        self.assertIn(clip.key, self.workspace._awaiting_capabilities)
        token_before = self.workspace._tokens[clip.key]

        self.workspace.refresh()

        self.assertEqual(clip.status, "pending")
        self.assertEqual(clip.error, "等待 ffprobe")
        self.assertEqual(self.workspace._tokens[clip.key], token_before)
        self.assertIn(clip.key, self.workspace._awaiting_capabilities)


class ClosedWorkspaceTests(TkWorkspaceTestCase):
    def test_close_ignores_late_results_and_rejects_new_work(self) -> None:
        clip = self.add_clip("late.mp4")
        self.drain_events()
        token_before = self.workspace._tokens[clip.key]

        self.workspace.close()

        self.assertTrue(self.workspace.closed)
        self.assertEqual(self.workspace._tokens[clip.key], token_before + 1)
        self.assertEqual(self.workspace._awaiting_capabilities, set())

        info = MediaInfo(path=clip.path, duration=3.0)
        self.workspace.handle_probe(
            {"key": clip.key, "token": token_before, "info": info}
        )
        self.workspace.handle_probe(
            {"key": clip.key, "token": self.workspace._tokens[clip.key], "info": info}
        )
        self.workspace.handle_probe_error(
            {"key": clip.key, "token": self.workspace._tokens[clip.key], "error": "late"}
        )
        self.assertIsNone(clip.info)
        self.assertEqual(clip.status, "probing")
        self.assertEqual(clip.error, "")

        self.assertEqual(self.workspace.add_paths([self.make_video("new.mp4")]), [])
        self.app.caps.ffprobe_path = ""
        self.workspace._awaiting_capabilities.add(clip.key)
        self.workspace.on_capabilities()
        self.assertEqual(self.workspace._tokens[clip.key], token_before + 1)

    def test_close_is_idempotent(self) -> None:
        self.workspace.close()
        self.workspace.close()
        self.assertTrue(self.workspace.closed)

    def test_probe_workers_are_daemons(self) -> None:
        self.assertEqual(len(mpw._PROBE_THREADS), mpw.PROBE_WORKERS)
        for thread in mpw._PROBE_THREADS:
            self.assertTrue(thread.daemon)

    def test_close_cancels_queued_probes_and_does_not_wait_for_running(self) -> None:
        release = threading.Event()
        started = threading.Event()

        def blocking_probe(_ffprobe, _path, **_kwargs) -> None:
            started.set()
            self.assertTrue(release.wait(timeout=5))
            raise RuntimeError("blocked probe")

        self.probe_mock.side_effect = blocking_probe
        self.add_clip("busy_a.mp4")
        self.add_clip("busy_b.mp4")
        queued = self.add_clip("queued_c.mp4")
        self.assertTrue(started.wait(timeout=5))
        deadline = time.monotonic() + 5
        while self.probe_mock.call_count < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.probe_mock.call_count, 2)

        begin = time.monotonic()
        self.workspace.close()
        self.assertLess(time.monotonic() - begin, 1.0)  # 不等待运行中的 ffprobe

        release.set()
        time.sleep(0.3)
        probed = {call.args[1] for call in self.probe_mock.call_args_list}
        self.assertNotIn(queued.path, probed)  # 未开始的任务已被取消


class SendActionTests(TkWorkspaceTestCase):
    def test_set_export_source_calls_app_with_selected_path(self) -> None:
        clip = self.add_clip("main.mp4")
        self.set_selection(clip)

        self.workspace.set_export_source()

        self.app._set_input_video.assert_called_once_with(clip.path)
        self.show_error.assert_not_called()
        self.assertTrue(any("已设为导出源" in item for item in self.app.logs))

    def test_set_export_source_requires_selection(self) -> None:
        self.workspace.set_export_source()
        self.app._set_input_video.assert_not_called()
        self.show_error.assert_called_once()

    def test_set_export_source_rejects_missing_file(self) -> None:
        clip = self.add_clip("ghost.mp4")
        Path(clip.path).unlink()
        self.set_selection(clip)

        self.workspace.set_export_source()

        self.app._set_input_video.assert_not_called()
        self.show_error.assert_called_once()
        self.assertEqual(clip.status, "offline")

    def test_set_export_source_falls_back_to_input_var(self) -> None:
        clip = self.add_clip("fallback.mp4")
        self.set_selection(clip)
        del self.app._set_input_video

        self.workspace.set_export_source()

        self.assertEqual(self.app.input_var.get(), clip.path)
        self.show_error.assert_not_called()

        del self.app.input_var
        self.workspace.set_export_source()
        self.show_error.assert_called_once()

    def test_add_to_join_sends_existing_paths_and_skips_missing(self) -> None:
        online_a = self.add_clip("join_a.mp4")
        online_b = self.add_clip("join_b.mp4")
        offline = self.add_clip("join_gone.mp4")
        Path(offline.path).unlink()
        self.set_selection(online_b, offline, online_a)

        self.workspace.add_to_join()

        sent = self.app._add_join_paths.call_args.args[0]
        self.assertEqual(set(sent), {online_a.path, online_b.path})
        self.assertTrue(any("已加入拼接 2 个素材" in item for item in self.app.logs))
        self.show_error.assert_not_called()

    def test_add_to_join_validates_selection_and_missing_files(self) -> None:
        self.workspace.add_to_join()  # 未选择
        self.app._add_join_paths.assert_not_called()
        self.show_error.assert_called_once()
        self.show_error.reset_mock()

        offline = self.add_clip("join_all_gone.mp4")
        Path(offline.path).unlink()
        self.set_selection(offline)
        self.workspace.add_to_join()
        self.app._add_join_paths.assert_not_called()
        self.show_error.assert_called_once()
        self.assertEqual(offline.status, "offline")
        self.show_error.reset_mock()

        del self.app._add_join_paths
        online = self.add_clip("join_ok.mp4")
        self.set_selection(online)
        self.workspace.add_to_join()
        self.show_error.assert_called_once()

    def test_add_to_proxy_sends_existing_paths(self) -> None:
        online = self.add_clip("proxy_a.mp4")
        offline = self.add_clip("proxy_gone.mp4")
        Path(offline.path).unlink()
        self.set_selection(online, offline)

        self.workspace.add_to_proxy()

        sent = self.app._add_proxy_paths.call_args.args[0]
        self.assertEqual(sent, [online.path])
        self.assertTrue(any("已加入代理 1 个原片" in item for item in self.app.logs))

    def test_add_to_proxy_requires_selection_and_files(self) -> None:
        self.workspace.add_to_proxy()
        self.app._add_proxy_paths.assert_not_called()
        self.show_error.assert_called_once()
        self.show_error.reset_mock()

        offline = self.add_clip("proxy_all_gone.mp4")
        Path(offline.path).unlink()
        self.set_selection(offline)
        self.workspace.add_to_proxy()
        self.app._add_proxy_paths.assert_not_called()
        self.show_error.assert_called_once()

    def test_double_click_loads_editor_and_switches_tab(self) -> None:
        clip = self.add_clip("preview.mp4")
        event = SimpleNamespace(y=7)

        with patch.object(self.workspace.clip_tree, "identify_row", return_value=clip.key):
            self.workspace._on_double_click(event)

        self.app._load_editor_video.assert_called_once_with(clip.path)
        self.assertEqual(self.app.main_notebook.selects, [self.app.editor_tab])
        self.assertTrue(any("已在剪辑工作台打开" in item for item in self.app.logs))

    def test_double_click_ignores_empty_and_missing_rows(self) -> None:
        clip = self.add_clip("gone_preview.mp4")
        Path(clip.path).unlink()
        event = SimpleNamespace(y=7)

        with patch.object(self.workspace.clip_tree, "identify_row", return_value=""):
            self.workspace._on_double_click(event)
        self.app._load_editor_video.assert_not_called()
        self.assertEqual(self.app.main_notebook.selects, [])

        with patch.object(self.workspace.clip_tree, "identify_row", return_value=clip.key):
            self.workspace._on_double_click(event)
        self.app._load_editor_video.assert_not_called()
        self.assertEqual(self.app.main_notebook.selects, [])
        self.show_error.assert_called_once()

    def test_double_click_falls_back_to_clip_page_index_without_editor_tab(self) -> None:
        clip = self.add_clip("fallback_tab.mp4")
        event = SimpleNamespace(y=7)
        del self.app.editor_tab

        with patch.object(self.workspace.clip_tree, "identify_row", return_value=clip.key):
            self.workspace._on_double_click(event)

        self.app._load_editor_video.assert_called_once_with(clip.path)
        self.assertEqual(self.app.main_notebook.selects, [2])

    def test_reveal_selected_opens_folder_for_existing_clip(self) -> None:
        clip = self.add_clip("reveal.mp4")
        self.set_selection(clip)
        self.workspace.reveal_selected()
        self.app._open_folder.assert_called_once_with(clip.path)

        missing = self.add_clip("reveal_gone.mp4")
        Path(missing.path).unlink()
        self.set_selection(missing)
        self.workspace.reveal_selected()
        self.show_error.assert_called_once()


class RemoveTests(TkWorkspaceTestCase):
    def test_remove_selected_unlinks_reference_not_file(self) -> None:
        first = self.add_clip("remove_a.mp4")
        second = self.add_clip("remove_b.mp4")
        kept = self.add_clip("keep.mp4")
        self.set_selection(first, second)

        self.workspace.remove_selected()

        self.ask_yesno.assert_called_once()
        self.assertIn("只是取消引用", self.ask_yesno.call_args.args[1])
        self.assertNotIn(first.key, self.workspace.model.clips)
        self.assertNotIn(second.key, self.workspace.model.clips)
        self.assertIn(kept.key, self.workspace.model.clips)
        self.assertTrue(Path(first.path).is_file())
        self.assertTrue(Path(second.path).is_file())
        self.assertEqual(set(self.workspace.clip_tree.get_children()), {kept.key})
        self.assertTrue(any("已移除 2 个素材引用" in item for item in self.app.logs))

    def test_remove_cancelled_keeps_clips(self) -> None:
        clip = self.add_clip("stay.mp4")
        self.set_selection(clip)
        self.ask_yesno.return_value = False

        self.workspace.remove_selected()

        self.assertIn(clip.key, self.workspace.model.clips)
        self.show_error.assert_not_called()

    def test_remove_without_selection_shows_error(self) -> None:
        self.workspace.remove_selected()
        self.ask_yesno.assert_not_called()
        self.show_error.assert_called_once()

    def test_removed_clip_late_probe_result_is_ignored(self) -> None:
        self.succeed_probes_with(MediaInfo(path=str(self.root_dir / "removed_late.mp4"), duration=1.0))
        clip = self.add_clip("removed_late.mp4")
        stale = self.wait_for_event(mpw.PROBE_EVENT)
        self.set_selection(clip)
        self.ask_yesno.return_value = True
        self.workspace.remove_selected()

        self.workspace.handle_probe(stale)  # token 已递增且 clip 已移除
        self.assertNotIn(clip.key, self.workspace.model.clips)


class WorkspaceUiTests(TkWorkspaceTestCase):
    def test_bin_switch_filters_clip_tree(self) -> None:
        media_bin = self.workspace.model.create_bin("B-Roll")
        in_bin = self.add_clip("in_bin.mp4", bin_id=media_bin.id)
        ungrouped = self.add_clip("no_bin.mp4")
        self.workspace.refresh()

        self.assertEqual(self.workspace._bin_ids, [ALL_BIN_ID, UNGROUPED_BIN_ID, media_bin.id])
        self.assertEqual(
            set(self.workspace.clip_tree.get_children()),
            {in_bin.key, ungrouped.key},
        )

        self.workspace.bin_list.selection_clear(0, "end")
        self.workspace.bin_list.selection_set(2)
        self.workspace._on_bin_selected()
        self.assertEqual(self.workspace.current_bin, media_bin.id)
        self.assertEqual(set(self.workspace.clip_tree.get_children()), {in_bin.key})

        self.workspace.bin_list.selection_clear(0, "end")
        self.workspace.bin_list.selection_set(1)
        self.workspace._on_bin_selected()
        self.assertEqual(self.workspace.current_bin, UNGROUPED_BIN_ID)
        self.assertEqual(set(self.workspace.clip_tree.get_children()), {ungrouped.key})

        self.workspace.bin_list.selection_clear(0, "end")
        self.workspace._on_bin_selected()  # 空选择：保持原媒体夹
        self.assertEqual(self.workspace.current_bin, UNGROUPED_BIN_ID)

    def test_search_filters_tree_case_insensitively(self) -> None:
        alpha = self.add_clip("Alpha_Interview.mp4")
        beta = self.add_clip("beta_tour.mp4")

        self.flush_search("ALPHA")
        self.assertEqual(set(self.workspace.clip_tree.get_children()), {alpha.key})
        self.assertTrue(self.workspace.count_var.get().startswith("共 2 个素材"))

        self.flush_search("no-match-at-all")
        self.assertEqual(self.workspace.clip_tree.get_children(), ())

        self.flush_search("")
        self.assertEqual(
            set(self.workspace.clip_tree.get_children()),
            {alpha.key, beta.key},
        )

    def test_multi_selection_removal_and_restore(self) -> None:
        first = self.add_clip("multi_a.mp4")
        second = self.add_clip("multi_b.mp4")
        third = self.add_clip("multi_c.mp4")

        self.set_selection(first, second)
        self.assertEqual(len(self.workspace.clip_tree.selection()), 2)
        self.ask_yesno.return_value = False
        self.workspace.remove_selected()
        self.assertEqual(len(self.workspace.model.clips), 3)

        self.ask_yesno.return_value = True
        self.workspace.remove_selected()
        self.assertEqual(set(self.workspace.clip_tree.get_children()), {third.key})
        self.assertEqual(len(self.workspace.clip_tree.selection()), 0)

    def test_toggle_sort_orders_tree_and_marks_heading(self) -> None:
        long_clip = self.add_clip("long.mp4")
        short_clip = self.add_clip("short.mp4")
        missing_info = self.add_clip("noinfo.mp4")
        long_clip.info = MediaInfo(path=long_clip.path, duration=90.0)
        short_clip.info = MediaInfo(path=short_clip.path, duration=5.0)

        self.workspace.toggle_sort("duration")
        self.assertEqual(
            list(self.workspace.clip_tree.get_children()),
            [missing_info.key, short_clip.key, long_clip.key],
        )
        self.assertTrue(
            self.workspace.clip_tree.heading("duration", "text").endswith("▲")
        )

        self.workspace.toggle_sort("duration")
        self.assertEqual(
            list(self.workspace.clip_tree.get_children()),
            [long_clip.key, short_clip.key, missing_info.key],
        )
        self.assertTrue(
            self.workspace.clip_tree.heading("duration", "text").endswith("▼")
        )

    def test_import_target_bin_follows_current_bin(self) -> None:
        self.assertEqual(self.workspace._import_target_bin(), None)
        media_bin = self.workspace.model.create_bin("导入目标")
        self.workspace.refresh()
        self.workspace.bin_list.selection_clear(0, "end")
        self.workspace.bin_list.selection_set(2)
        self.workspace._on_bin_selected()
        self.assertEqual(self.workspace._import_target_bin(), media_bin.id)

        self.workspace.current_bin = UNGROUPED_BIN_ID
        self.assertEqual(self.workspace._import_target_bin(), None)

    def test_move_menu_requires_selection_and_moves_clips(self) -> None:
        clip = self.add_clip("movable.mp4")
        media_bin = self.workspace.model.create_bin("目的地")

        with patch.object(self.workspace, "_selected_keys", return_value=[]):
            self.workspace._show_move_menu()
        self.show_error.assert_called_once()
        self.show_error.reset_mock()

        with (
            patch.object(mpw.tk, "Menu") as menu_factory,
            patch.object(self.workspace.move_button, "winfo_rootx", return_value=10),
            patch.object(self.workspace.move_button, "winfo_rooty", return_value=20),
            patch.object(self.workspace.move_button, "winfo_height", return_value=30),
        ):
            menu_factory.return_value.tk_popup = Mock()
            self.workspace._show_move_menu()

        self.workspace.clip_tree.selection_set(clip.key)
        self.workspace._move_selected(media_bin.id)
        self.assertEqual(clip.bin_id, media_bin.id)
        self.workspace._move_selected(UNGROUPED_BIN_ID)
        self.assertIsNone(clip.bin_id)

    def test_move_menu_destroys_previous_menu(self) -> None:
        clip = self.add_clip("menu_clip.mp4")
        self.set_selection(clip)

        with (
            patch.object(mpw.tk, "Menu") as menu_factory,
            patch.object(self.workspace.move_button, "winfo_rootx", return_value=10),
            patch.object(self.workspace.move_button, "winfo_rooty", return_value=20),
            patch.object(self.workspace.move_button, "winfo_height", return_value=30),
        ):
            menus = []
            for _ in range(2):
                menu = Mock()
                menu.tk_popup = Mock()
                menus.append(menu)
            menu_factory.side_effect = menus
            self.workspace._show_move_menu()
            self.workspace._show_move_menu()

        self.assertIs(self.workspace._move_menu, menus[1])
        menus[0].destroy.assert_called_once()


class DropTargetTests(TkWorkspaceTestCase):
    """从资源管理器拖放文件/文件夹导入媒体池。"""

    def make_drop_event(self, data, action: str = "copy") -> SimpleNamespace:
        return SimpleNamespace(data=data, action=action)

    def select_bin(self, index: int) -> str:
        self.workspace.bin_list.selection_clear(0, "end")
        self.workspace.bin_list.selection_set(index)
        self.workspace._on_bin_selected()
        return self.workspace.current_bin

    def require_tkdnd(self) -> None:
        if mpw.TkinterDnD is None or not getattr(mpw.TkinterDnD, "TkdndVersion", None):
            self.skipTest("tkinterdnd2/tkdnd 不可用，跳过拖放注册测试")

    def drop_target_widgets(self, workspace=None) -> list:
        workspace = workspace or self.workspace
        return [
            workspace,               # 整个媒体池 Frame
            workspace.clip_tree,     # 主落点：素材列表
            workspace.bin_list,      # 媒体夹列表
            workspace.search_entry,  # 搜索框
            workspace.empty_state,   # 空状态占位
        ]

    def test_setup_registers_drop_targets_on_all_visible_widgets(self) -> None:
        self.require_tkdnd()
        # 注册全程 mock（含解绑）：tkdnd 对“未注册先解绑”会原生崩溃，不能落真实调用
        with (
            patch.object(mpw.TkinterDnD, "_require"),
            patch.object(tk.BaseWidget, "drop_target_register", autospec=True) as register,
            patch.object(tk.BaseWidget, "dnd_bind", autospec=True) as bind,
            patch.object(tk.BaseWidget, "drop_target_unregister", autospec=True),
            patch.object(tk.BaseWidget, "unbind", autospec=True),
        ):
            workspace = mpw.MediaPoolWorkspace(self.app, self.root)
            try:
                targets = getattr(workspace, "_drop_targets", None)
                self.assertIsInstance(targets, list)
                registered = {call.args[0] for call in register.call_args_list}
                handlers = {
                    "<<DropEnter>>": workspace._on_drop_enter,
                    "<<DropLeave>>": workspace._on_drop_leave,
                    "<<Drop>>": workspace._on_drop,
                }
                for widget in self.drop_target_widgets(workspace):
                    # 每个可见控件都在目标列表里，且真的注册了 DND_FILES
                    self.assertIn(widget, targets)
                    self.assertIn(widget, registered)
                    register.assert_any_call(widget, mpw.DND_FILES)
                    # 三个事件统一绑定到共享处理器（所有控件共用同一套回调）
                    bound = {}
                    for call in bind.call_args_list:
                        if call.args and call.args[0] is widget:
                            bound[call.args[1]] = call.args[2]
                    self.assertEqual(set(bound), set(mpw.DROP_EVENTS))
                    for sequence, handler in handlers.items():
                        self.assertEqual(bound[sequence], handler)
            finally:
                workspace.close()
                workspace.destroy()

    def test_register_drop_target_binds_shared_handlers_on_widget(self) -> None:
        self.require_tkdnd()
        for widget in (self.workspace.bin_list, self.workspace.search_entry,
                       self.workspace.empty_state):
            # setUp 时已注册过：先移出列表绕过去重守卫，再直接调用注册方法
            if widget in self.workspace._drop_targets:
                self.workspace._drop_targets.remove(widget)
            with (
                patch.object(widget, "drop_target_register") as register,
                patch.object(widget, "dnd_bind") as bind,
            ):
                self.workspace._register_drop_target(widget)
            register.assert_called_once_with(mpw.DND_FILES)
            bound = {call.args[0]: call.args[1] for call in bind.call_args_list}
            self.assertEqual(set(bound), set(mpw.DROP_EVENTS))
            self.assertEqual(bound["<<DropEnter>>"], self.workspace._on_drop_enter)
            self.assertEqual(bound["<<DropLeave>>"], self.workspace._on_drop_leave)
            self.assertEqual(bound["<<Drop>>"], self.workspace._on_drop)
            if widget in self.workspace._drop_targets:
                self.workspace._drop_targets.remove(widget)  # 还原，避免 close 重复解绑

    def test_tkdnd_package_loaded_into_interpreter(self) -> None:
        if mpw.TkinterDnD is None:
            self.skipTest("未安装 tkinterdnd2")
        # 注册发生在 setUp 创建的工作台上：解释器里必须真的加载了 tkdnd
        self.assertTrue(mpw.TkinterDnD.TkdndVersion)

    def test_drop_enter_highlights_frame_and_leave_resets(self) -> None:
        self.workspace._on_drop_enter(self.make_drop_event("", action="copy"))
        self.assertEqual(self.workspace.cget("highlightthickness"), mpw.DROP_HIGHLIGHT_WIDTH)
        self.assertEqual(self.workspace.cget("highlightbackground"), mpw.PURPLE)

        self.workspace._on_drop_leave()
        self.assertEqual(self.workspace.cget("highlightthickness"), 0)

        self.workspace._on_drop(self.make_drop_event(""))  # drop 结束也要清掉高亮
        self.assertEqual(self.workspace.cget("highlightthickness"), 0)

    def test_drop_parses_braced_paths_and_targets_current_bin(self) -> None:
        media_bin = self.workspace.model.create_bin("拖放夹")
        self.workspace.refresh()
        bin_id = self.select_bin(2)
        self.assertEqual(bin_id, media_bin.id)

        folder = self.root_dir / "拖放 目录"
        folder.mkdir()
        spaced = self.make_video("带 空格.mp4", folder)
        chinese = self.make_video("中文素材.mov")
        # tkdnd 传来的 event.data 是合法 Tcl 列表：每个路径都由花括号包裹
        data = f"{{{spaced}}} {{{chinese}}}"

        self.workspace._on_drop(self.make_drop_event(data))

        self.assertEqual(len(self.workspace.model.clips), 2)
        paths = {clip.path for clip in self.workspace.model.clips.values()}
        self.assertEqual(paths, {str(spaced), str(chinese)})
        for clip in self.workspace.model.clips.values():
            self.assertEqual(clip.bin_id, media_bin.id)

    def test_drop_folder_recurses_and_mixed_drop_works(self) -> None:
        folder = self.root_dir / "整包素材"
        folder.mkdir()
        self.make_video("pack_a.mp4", folder)
        self.make_video("pack_b.mp4", folder)
        loose = self.make_video("loose.mp4")

        self.workspace._on_drop(self.make_drop_event(f"{{{folder}}} {{{loose}}}"))

        names = {clip.name for clip in self.workspace.model.clips.values()}
        self.assertEqual(names, {"pack_a.mp4", "pack_b.mp4", "loose.mp4"})
        self.drain_events()

    def test_drop_ignores_empty_or_blank_data(self) -> None:
        with patch.object(self.workspace, "add_paths") as add:
            for data in ("", "   ", "{}", None):
                self.workspace._on_drop(self.make_drop_event(data))
        add.assert_not_called()

    def test_drop_deduplicates_repeated_paths(self) -> None:
        path = self.make_video("dup.mp4")
        self.workspace._on_drop(self.make_drop_event(f"{{{path}}} {{{path}}}"))
        self.assertEqual(len(self.workspace.model.clips), 1)

    def test_drop_swallows_exceptions_and_keeps_callback_safe(self) -> None:
        path = self.make_video("boom.mp4")
        with patch.object(self.workspace, "add_paths", side_effect=RuntimeError("boom")):
            returned = self.workspace._on_drop(self.make_drop_event(f"{{{path}}}"))

        self.assertEqual(returned, "copy")  # 未向 Tk 回调外抛异常
        self.assertTrue(any("拖放导入失败" in item and "boom" in item for item in self.app.logs))

    def test_drop_after_close_is_ignored(self) -> None:
        self.workspace.close()
        with patch.object(self.workspace, "add_paths") as add:
            self.workspace._on_drop(self.make_drop_event(str(self.make_video("late.mp4"))))
        add.assert_not_called()

    def test_close_unbinds_every_registered_drop_target(self) -> None:
        self.require_tkdnd()
        widgets = self.drop_target_widgets()
        with contextlib.ExitStack() as stack:
            unregisters = [
                stack.enter_context(patch.object(widget, "drop_target_unregister"))
                for widget in widgets
            ]
            unbinds = [
                stack.enter_context(patch.object(widget, "unbind"))
                for widget in widgets
            ]
            self.workspace.close()
            for unregister in unregisters:
                unregister.assert_called_once_with()
            for unbind in unbinds:
                self.assertEqual(
                    sorted(call.args[0] for call in unbind.call_args_list),
                    sorted(mpw.DROP_EVENTS),
                )
            totals = (
                sum(mock.call_count for mock in unregisters),
                sum(mock.call_count for mock in unbinds),
            )
            self.workspace.close()  # 幂等：重复关闭不得再次解绑
            self.assertEqual(sum(mock.call_count for mock in unregisters), totals[0])
            self.assertEqual(sum(mock.call_count for mock in unbinds), totals[1])

    def test_close_clears_all_dnd_bindings_from_widgets(self) -> None:
        self.require_tkdnd()
        # setUp 创建工作台时注册确实落在了真实控件上
        for widget in (self.workspace.clip_tree, self.workspace.search_entry):
            self.assertNotEqual(widget.bind("<<Drop>>"), "")
        self.workspace.close()
        for widget in self.drop_target_widgets():
            for sequence in mpw.DROP_EVENTS:
                self.assertEqual(widget.bind(sequence), "")

    def test_close_survives_tcl_error_and_still_unbinds_rest(self) -> None:
        self.require_tkdnd()
        with patch.object(
            self.workspace, "drop_target_unregister",
            side_effect=tk.TclError("已销毁"),
        ):
            self.workspace.close()  # 单个目标解绑失败不得中断关闭

        self.assertTrue(self.workspace.closed)
        for widget in (self.workspace.clip_tree, self.workspace.bin_list,
                       self.workspace.search_entry, self.workspace.empty_state):
            for sequence in mpw.DROP_EVENTS:
                self.assertEqual(widget.bind(sequence), "")


if __name__ == "__main__":
    unittest.main()
