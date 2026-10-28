"""Synchronous, cancellation-aware execution for lossless FFmpeg export plans."""

from __future__ import annotations

import math
import os
import queue
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from lossless_ffmpeg import CREATE_NO_WINDOW, LosslessExportPlan, quote_command


@dataclass(frozen=True, slots=True)
class LosslessProgress:
    command_index: int
    command_count: int
    seconds: float = 0.0
    fraction: float | None = None
    overall_fraction: float | None = None
    frame: int | None = None
    fps: float | None = None
    speed: float | None = None


@dataclass(frozen=True, slots=True)
class LosslessRunResult:
    separate_outputs: tuple[str, ...]
    merged_outputs: tuple[str, ...]

    @property
    def outputs(self) -> tuple[str, ...]:
        return self.separate_outputs + self.merged_outputs


class LosslessExportCancelled(RuntimeError):
    pass


class LosslessExportError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        command: Sequence[str],
        command_index: int,
        returncode: int | None,
        stderr: str,
    ) -> None:
        super().__init__(message)
        self.command = tuple(command)
        self.command_index = command_index
        self.returncode = returncode
        self.stderr = stderr


ProgressCallback = Callable[[LosslessProgress], None]
LogCallback = Callable[[str], None]


def _path_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve(strict=False)))


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    resolved = path.resolve(strict=False)
    for root in roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            continue
    return False


def _mkdir(path: Path, created: list[Path]) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    path.mkdir(parents=True, exist_ok=True)
    created.extend(reversed(missing))


def _seconds(value: Any) -> float | None:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _clock_seconds(value: str) -> float | None:
    try:
        hours, minutes, seconds = value.strip().split(":")
        result = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _command_duration(command: Sequence[str]) -> float | None:
    for index, argument in enumerate(command[:-1]):
        if argument == "-t":
            return _seconds(command[index + 1])
    return None


def _event(
    index: int,
    count: int,
    values: dict[str, str],
    duration: float | None,
    *,
    finished: bool = False,
) -> LosslessProgress:
    seconds = _seconds(values.get("out_time_us", ""))
    if seconds is not None:
        seconds /= 1_000_000
    if seconds is None:
        raw = _seconds(values.get("out_time_ms", ""))
        seconds = raw / 1_000_000 if raw is not None else None
    if seconds is None:
        seconds = _clock_seconds(values.get("out_time", "")) or 0.0
    fraction = min(1.0, seconds / duration) if duration and duration > 0 else None
    if finished or values.get("progress") == "end":
        fraction = 1.0
    overall = (
        min(1.0, (index - 1 + fraction) / count)
        if fraction is not None else None
    )
    frame: int | None
    try:
        frame = int(values["frame"])
    except (KeyError, ValueError):
        frame = None
    speed_text = values.get("speed", "").strip().removesuffix("x").strip()
    return LosslessProgress(
        command_index=index,
        command_count=count,
        seconds=seconds,
        fraction=fraction,
        overall_fraction=overall,
        frame=frame,
        fps=_seconds(values.get("fps", "")),
        speed=_seconds(speed_text),
    )


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_command(
    command: Sequence[str],
    *,
    index: int,
    count: int,
    duration: float | None,
    cancel_event: Any,
    progress: ProgressCallback | None,
    log: LogCallback | None,
) -> None:
    def cancelled() -> bool:
        return cancel_event is not None and bool(cancel_event.is_set())

    if cancelled():
        raise LosslessExportCancelled("导出已取消。")
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as exc:
        detail = str(exc)
        raise LosslessExportError(
            f"无法启动 FFmpeg：\n{detail}",
            command=command,
            command_index=index,
            returncode=None,
            stderr=detail,
        ) from exc

    events: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def read_stream(kind: str, stream: TextIO) -> None:
        try:
            for line in iter(stream.readline, ""):
                events.put((kind, line.rstrip("\r\n")))
        finally:
            events.put((kind, None))

    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()

    values: dict[str, str] = {}
    stderr_lines: list[str] = []
    open_streams = 2
    try:
        while open_streams:
            if cancelled():
                raise LosslessExportCancelled("导出已取消。")
            try:
                kind, line = events.get(timeout=0.05)
            except queue.Empty:
                if process.poll() is not None and not any(reader.is_alive() for reader in readers):
                    break
                continue
            if line is None:
                open_streams -= 1
                continue
            if kind == "stderr":
                stderr_lines.append(line)
                if log is not None:
                    log(line)
                continue
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
                if progress is not None and key in {
                    "frame", "fps", "out_time", "out_time_ms", "out_time_us",
                    "speed", "progress",
                }:
                    progress(_event(index, count, values, duration))
        returncode = process.wait()
    finally:
        _terminate(process)
        for reader in readers:
            reader.join(timeout=1.0)
        process.stdout.close()
        process.stderr.close()

    if returncode:
        stderr = "\n".join(stderr_lines).strip()
        raise LosslessExportError(
            f"FFmpeg 第 {index}/{count} 条命令失败（退出码 {returncode}）：\n{stderr}",
            command=command,
            command_index=index,
            returncode=returncode,
            stderr=stderr,
        )
    if progress is not None:
        progress(_event(index, count, values, duration, finished=True))


def _validate_plan_outputs(plan: LosslessExportPlan) -> None:
    """Reject duplicate segment paths and merged/segment name collisions."""
    for label, paths in (
        ("分段输出", plan.segment_outputs),
        ("临时分段", plan.part_outputs),
    ):
        seen: dict[str, str] = {}
        for path in paths:
            key = _path_key(path)
            if key in seen:
                raise ValueError(
                    f"{label}路径重复：{seen[key]} / {path}；"
                    "请修改命名模板或输出目录。"
                )
            seen[key] = str(path)
    if plan.merged_output:
        merged_key = _path_key(plan.merged_output)
        for path in (*plan.segment_outputs, *plan.part_outputs):
            if _path_key(path) == merged_key:
                raise ValueError(
                    f"合并输出与分段输出路径重复：{path}；"
                    "请修改合并文件名或输出目录。"
                )


def run_lossless_export(
    plan: LosslessExportPlan,
    *,
    cancel_event: Any = None,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
) -> LosslessRunResult:
    """Execute *plan* in the calling thread and return its final output paths.

    ``cancel_event`` only needs an ``is_set()`` method.  Completed separate
    outputs are kept on failure/cancellation; the active partial output and
    temporary files created by this run are removed.  Pre-existing files and
    unplanned files in temporary folders are never deleted.
    """
    if plan.mode not in {"separate", "merge", "both"}:
        raise ValueError("runner 仅支持 separate、merge 或 both 导出计划。")
    if not plan.commands:
        raise ValueError("导出计划没有 FFmpeg 命令。")
    _validate_plan_outputs(plan)

    command_outputs = list(plan.part_outputs or plan.segment_outputs)
    if plan.merged_output:
        command_outputs.append(plan.merged_output)
    if len(command_outputs) != len(plan.commands):
        raise ValueError("计划命令数量与输出路径数量不一致。")

    source_key = _path_key(plan.source)
    managed_paths = {
        _path_key(path): Path(path)
        for path in (
            *plan.segment_outputs,
            *( (plan.merged_output,) if plan.merged_output else () ),
            *plan.part_outputs,
            *((plan.manifest_path,) if plan.manifest_path else ()),
            *((plan.chapters_path,) if plan.chapters_path else ()),
        )
    }
    if source_key in managed_paths:
        raise ValueError("输出路径不能覆盖源片。")
    preexisting = {
        key for key, path in managed_paths.items()
        if path.exists() or path.is_symlink()
    }
    cleanup_roots = [Path(path) for path in plan.cleanup_paths]
    created_dirs: list[Path] = []
    current_output: Path | None = None

    def emit_log(message: str) -> None:
        if log is not None:
            log(message)

    def remove_created(path: Path) -> None:
        key = _path_key(path)
        if key in preexisting or key == source_key:
            return
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
        except OSError as exc:
            emit_log(f"警告：无法清理 {path}：{exc}")

    def cleanup_temporary() -> None:
        final_paths = (
            *plan.segment_outputs,
            *((plan.merged_output,) if plan.merged_output else ()),
        )
        final_keys = {_path_key(path) for path in final_paths}
        candidates = [
            Path(path) for path in (
                *plan.part_outputs,
                *((plan.manifest_path,) if plan.manifest_path else ()),
                *((plan.chapters_path,) if plan.chapters_path else ()),
            )
        ]
        for path in candidates:
            if _path_key(path) not in final_keys and _inside(path, cleanup_roots):
                remove_created(path)
        for folder in reversed(created_dirs):
            if not _inside(folder, cleanup_roots):
                continue
            try:
                folder.rmdir()
            except OSError:
                pass

    try:
        for path in managed_paths.values():
            _mkdir(path.parent, created_dirs)
        if plan.temp_dir:
            _mkdir(Path(plan.temp_dir), created_dirs)
        plan.write_sidecars()
        for warning in plan.warnings:
            emit_log(warning)

        durations = [_command_duration(command) for command in plan.commands]
        if plan.merged_output and len(durations) > 1 and all(
            value is not None for value in durations[:-1]
        ):
            durations[-1] = sum(value for value in durations[:-1] if value is not None)

        for index, (command, output) in enumerate(
            zip(plan.commands, command_outputs), start=1
        ):
            if cancel_event is not None and cancel_event.is_set():
                raise LosslessExportCancelled("导出已取消。")
            current_output = Path(output)
            emit_log(f"执行 {index}/{len(plan.commands)}：{quote_command(command)}")
            if progress is not None:
                progress(_event(index, len(plan.commands), {}, durations[index - 1]))
            _run_command(
                command,
                index=index,
                count=len(plan.commands),
                duration=durations[index - 1],
                cancel_event=cancel_event,
                progress=progress,
                log=log,
            )
            if not current_output.is_file():
                raise LosslessExportError(
                    f"FFmpeg 未生成预期输出：{current_output}",
                    command=command,
                    command_index=index,
                    returncode=0,
                    stderr="",
                )
            current_output = None
        if cancel_event is not None and cancel_event.is_set():
            raise LosslessExportCancelled("导出已取消。")
    except BaseException:
        if current_output is not None:
            remove_created(current_output)
        cleanup_temporary()
        raise
    else:
        cleanup_temporary()

    return LosslessRunResult(
        separate_outputs=tuple(plan.segment_outputs),
        merged_outputs=(plan.merged_output,) if plan.merged_output else (),
    )


__all__ = [
    "LosslessExportCancelled",
    "LosslessExportError",
    "LosslessProgress",
    "LosslessRunResult",
    "run_lossless_export",
]
