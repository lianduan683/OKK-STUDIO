from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lossless_ffmpeg as ll  # noqa: E402
import lossless_runner as runner  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ll.find_binary("ffmpeg", ROOT)
FFPROBE = ll.find_binary("ffprobe", ROOT)
HAS_REAL_FFMPEG = bool(FFMPEG and FFPROBE)


@unittest.skipUnless(HAS_REAL_FFMPEG, "需要 bin/ffmpeg.exe 与 bin/ffprobe.exe")
class RealLosslessRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.folder = Path(tempfile.mkdtemp(prefix="无损 runner-"))
        cls.source = cls.folder / "中文 素材" / "源 视频.mkv"
        cls.source.parent.mkdir(parents=True)
        command = [
            str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "96k", "-shortest", str(cls.source),
        ]
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=ll.CREATE_NO_WINDOW,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))
        cls.probe = ll.probe_media(str(FFPROBE), str(cls.source))
        cls.keyframes = ll.probe_keyframes(str(FFPROBE), str(cls.source))
        cls.source_bytes = cls.source.read_bytes()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.folder, ignore_errors=True)

    def setUp(self) -> None:
        self.output = Path(tempfile.mkdtemp(prefix="输出 目录-", dir=self.folder))

    def _segments(self) -> list[tuple[float, float]]:
        return [(0.0, 1.0), (1.0, 2.0)]

    def _plan(self, mode: str) -> ll.LosslessExportPlan:
        kwargs: dict[str, object] = {}
        if mode in {"merge", "both"}:
            kwargs["output"] = str(self.output / f"{mode} 合并.mkv")
        if mode == "merge":
            temp_dir = self.output / "计划 临时"
            temp_dir.mkdir()
            (temp_dir / "用户 文件.txt").write_text("不要删除", encoding="utf-8")
            kwargs["temp_dir"] = str(temp_dir)
        return ll.plan_lossless_export(
            str(FFMPEG), str(self.source), self._segments(),
            mode=mode,
            output_dir=str(self.output),
            template="{index:02d}_中文 片段{ext}",
            probe=self.probe,
            keyframes=self.keyframes,
            chapters=mode in {"merge", "both"},
            **kwargs,
        )

    def _assert_media(self, path: str) -> None:
        media = ll.probe_media(str(FFPROBE), path)
        self.assertTrue(media.primary_video)
        self.assertGreater(media.duration, 0.1)
        self.assertEqual(media.primary_video.codec_name, self.probe.primary_video.codec_name)

    def test_real_separate_progress_logs_and_outputs(self) -> None:
        plan = self._plan("separate")
        progress: list[runner.LosslessProgress] = []
        logs: list[str] = []

        result = runner.run_lossless_export(plan, progress=progress.append, log=logs.append)

        self.assertEqual(result.separate_outputs, plan.segment_outputs)
        self.assertEqual(result.merged_outputs, ())
        self.assertEqual(len(result.outputs), 2)
        for path in result.outputs:
            self.assertTrue(Path(path).is_file(), path)
            self._assert_media(path)
        self.assertTrue(any(item.seconds > 0 for item in progress))
        self.assertEqual(progress[-1].command_index, 2)
        self.assertEqual(progress[-1].overall_fraction, 1.0)
        self.assertTrue(any("执行 1/2" in line for line in logs))
        self.assertEqual(self.source.read_bytes(), self.source_bytes)

    def test_real_merge_cleans_only_planned_temp_files(self) -> None:
        plan = self._plan("merge")
        user_file = Path(plan.temp_dir or "") / "用户 文件.txt"

        result = runner.run_lossless_export(plan)

        self.assertEqual(result.separate_outputs, ())
        self.assertEqual(result.merged_outputs, (plan.merged_output,))
        self._assert_media(result.merged_outputs[0])
        for path in plan.part_outputs:
            self.assertFalse(Path(path).exists(), path)
        self.assertFalse(Path(plan.manifest_path or "").exists())
        self.assertFalse(Path(plan.chapters_path or "").exists())
        self.assertEqual(user_file.read_text(encoding="utf-8"), "不要删除")
        self.assertTrue(Path(plan.temp_dir or "").is_dir())

    def test_real_both_returns_separate_and_merged_outputs(self) -> None:
        plan = self._plan("both")

        result = runner.run_lossless_export(plan)

        self.assertEqual(result.separate_outputs, plan.segment_outputs)
        self.assertEqual(result.merged_outputs, (plan.merged_output,))
        self.assertEqual(len(result.outputs), 3)
        for path in result.outputs:
            self.assertTrue(Path(path).is_file(), path)
            self._assert_media(path)
        self.assertFalse(Path(plan.temp_dir or "").exists())
        for path in plan.segment_outputs:
            self.assertTrue(Path(path).is_file(), path)

    def test_real_failure_contains_stderr_and_removes_partial_output(self) -> None:
        output = self.output / "失败 片段.mkv"
        missing_output = self.output / "不存在" / "无法打开.mkv"
        command = ll.build_ffmpeg_base(str(FFMPEG), loglevel="info") + [
            "-i", str(self.source), "-map", "0:v?", "-map", "0:a?",
            "-c", "copy", str(output),
            "-c", "copy", str(missing_output),
        ]
        plan = ll.LosslessExportPlan(
            mode="separate",
            source=str(self.source),
            commands=(tuple(command),),
            segment_outputs=(str(output),),
        )

        with self.assertRaises(runner.LosslessExportError) as caught:
            runner.run_lossless_export(plan)

        error = caught.exception
        self.assertNotEqual(error.returncode, 0)
        self.assertTrue(error.stderr)
        self.assertIn(error.stderr, str(error))
        self.assertFalse(output.exists())
        self.assertFalse(missing_output.exists())
        self.assertEqual(self.source.read_bytes(), self.source_bytes)

    def test_real_cancellation_terminates_ffmpeg_and_removes_partial_output(self) -> None:
        output = self.output / "取消 片段.mkv"
        command = ll.build_ffmpeg_base(str(FFMPEG), loglevel="info") + [
            "-re", "-i", str(self.source), "-t", "4",
            "-map", "0:v?", "-map", "0:a?", "-c", "copy", str(output),
        ]
        plan = ll.LosslessExportPlan(
            mode="separate",
            source=str(self.source),
            commands=(tuple(command),),
            segment_outputs=(str(output),),
        )
        cancel = threading.Event()
        started = threading.Event()
        errors: list[BaseException] = []

        def on_progress(event: runner.LosslessProgress) -> None:
            if event.seconds > 0:
                started.set()

        def export() -> None:
            try:
                runner.run_lossless_export(
                    plan, cancel_event=cancel, progress=on_progress,
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=export)
        worker.start()
        self.assertTrue(started.wait(timeout=15), "FFmpeg 未上报实时进度")
        cancel.set()
        worker.join(timeout=10)

        self.assertFalse(worker.is_alive(), "取消后 FFmpeg 进程未结束")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], runner.LosslessExportCancelled)
        self.assertFalse(output.exists())
        self.assertEqual(self.source.read_bytes(), self.source_bytes)

    def test_source_output_collision_is_rejected_without_touching_source(self) -> None:
        command = [str(FFMPEG), "-i", str(self.source), "-c", "copy", str(self.source)]
        plan = ll.LosslessExportPlan(
            mode="separate",
            source=str(self.source),
            commands=(tuple(command),),
            segment_outputs=(str(self.source),),
        )
        with self.assertRaises(ValueError):
            runner.run_lossless_export(plan)
        self.assertEqual(self.source.read_bytes(), self.source_bytes)


class PlanOutputValidationTests(unittest.TestCase):
    def test_duplicate_segment_outputs_are_rejected_before_running(self) -> None:
        command = ("ffmpeg", "-i", "source.mkv")
        plan = ll.LosslessExportPlan(
            mode="separate",
            source="source.mkv",
            commands=(command, command),
            segment_outputs=("same.mkv", "same.mkv"),
        )
        with self.assertRaises(ValueError) as caught:
            runner.run_lossless_export(plan)
        self.assertIn("重复", str(caught.exception))

    def test_merged_output_colliding_with_segment_output_is_rejected(self) -> None:
        command = ("ffmpeg", "-i", "source.mkv")
        with tempfile.TemporaryDirectory() as folder:
            segment = str(Path(folder) / "片段.mkv")
            plan = ll.LosslessExportPlan(
                mode="both",
                source="source.mkv",
                commands=(command, command, command),
                segment_outputs=(segment,),
                part_outputs=(segment,),
                merged_output=segment,
            )
            with self.assertRaises(ValueError) as caught:
                runner.run_lossless_export(plan)
            self.assertIn("合并输出与分段输出路径重复", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
