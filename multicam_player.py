"""Asynchronous, common-clock MPV monitoring for the multi-camera editor.

The worker owns every MPV object.  The public methods only queue messages;
the caller may drain ``events`` from its normal Tk ``after`` callback.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import queue
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from exporter_core import CREATE_NO_WINDOW
from mpv_controller import MpvController
from subtitle_core import subtitle_track_to_dict, write_ass
from creative_core import ColorGrade, validate_grade
from program_edits import grade_at
from realtime_grade import RealtimeGrade, validate_preview_target
from transition_preview import transition_signature
from gpu_live_player import GpuLivePlayerMixin


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


class _AudioController(MpvController):
    """Reuse MPV IPC without opening a GPU output for the master sound."""

    def start(self, timeout: float = 8.0) -> None:
        if self.process and self.process.poll() is None:
            return
        if os.name != "nt":
            raise RuntimeError("内嵌 MPV 播放器当前仅支持 Windows。")
        self._close_pipe()
        self.process = subprocess.Popen(
            [self.executable, "--no-config", "--idle=yes", "--terminal=no",
             "--vid=no", "--vo=null", "--force-window=no", "--keep-open=yes",
             "--input-default-bindings=no", f"--input-ipc-server={self.pipe_name}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
        )
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("主音频 MPV 启动失败。")
            try:
                self.command(["get_property", "mpv-version"], timeout=0.5)
                return
            except (OSError, RuntimeError) as exc:
                last_error = exc
                time.sleep(0.05)
        raise RuntimeError(f"连接主音频 MPV 超时：{last_error}")


@dataclass
class _Slot:
    window: int
    controller: MpvController | None = None
    key: tuple | None = None
    failed_key: tuple | None = None
    active: bool = False
    subtitle_key: tuple | None = None
    subtitle_id: int | None = None
    last_seek_time: float = float('-inf')
    prewarm_target: tuple | None = None
    playback_speed: float = 1.0
    last_speed_poll: float = float('-inf')


class MulticamPlayer(GpuLivePlayerMixin):
    """Threaded MPV player.  All times are seconds on the project timeline."""

    TICK = 1 / 30
    DRIFT_INTERVAL = 0.5
    DRIFT_LIMIT = 0.12
    DRIFT_COOLDOWN = 1.0
    THUMB_DRIFT_LIMIT = 0.4
    THUMB_DRIFT_COOLDOWN = 2.0
    PREWARM_SECONDS = 3.0

    def __init__(self, executable: str, camera_windows: dict[int, int],
                 program_window: int, audio_window: int, events: queue.Queue,
                 *, program_standby_window: int | None = None,
                 comparison_window: int | None = None,
                 comparison_standby_window: int | None = None,
                 gpu_live: bool = False, gpu_live_runtime=None,
                 gpu_session_factory=None, gpu_output_window=None) -> None:
        self.executable = str(executable)
        self.events = events
        self.camera_windows = self._checked_camera_windows(camera_windows)
        self.program_window = int(program_window)
        self.program_standby_window = int(program_standby_window) if program_standby_window is not None else None
        if comparison_standby_window is not None and comparison_window is None:
            raise ValueError('原片备用窗口需要同时提供原片主窗口。')
        self.comparison_window = int(comparison_window) if comparison_window is not None else None
        self.comparison_standby_window = int(comparison_standby_window) if comparison_standby_window is not None else None
        self.audio_window = int(audio_window)
        self._gpu_live_requested = bool(gpu_live)
        self._gpu_live_runtime = gpu_live_runtime
        self._gpu_session_factory = gpu_session_factory
        self.gpu_output_window = int(gpu_output_window) if gpu_output_window is not None else self.program_window
        self._commands: queue.Queue = queue.Queue()
        self._closing = threading.Event()
        self._done = threading.Event()
        # close() only signals; keep the worker alive until its finally block
        # has reaped our MPV processes when Tk/Python's main thread exits.
        self._thread = threading.Thread(target=self._run, name="multicam-mpv", daemon=False)
        self._thread.start()

    def submit(self, action: str, **kwargs: Any) -> None:
        if self._closing.is_set():
            return
        if action == "configure" and "project" in kwargs:
            kwargs["project"] = copy.deepcopy(kwargs["project"])
        if action == "grade_preview" and "grade" in kwargs:
            kwargs["grade"] = copy.deepcopy(kwargs["grade"])
        if action in {"transition_previews", "camera_windows"}:
            kwargs = copy.deepcopy(kwargs)
        self._commands.put((action, kwargs))

    def close(self) -> None:
        """Request shutdown without waiting on MPV from the UI thread."""
        if not self._closing.is_set():
            self._closing.set()
            self._commands.put(("close", {}))

    def wait_closed(self, timeout: float | None = None) -> bool:
        """Optional shutdown wait for a non-UI owner or a test."""
        return self._done.wait(timeout)

    def _emit(self, kind: str, value: Any) -> None:
        self.events.put((kind, value))

    def _error(self, label: str, exc: Exception) -> None:
        if self._closing.is_set():
            return
        text = f"{label}：{exc}"
        if text not in self._reported:
            self._reported.add(text)
            self._emit("error", text)

    def _run(self) -> None:
        self._project = None
        self._use_proxies = True
        self._playing = False
        self._position = 0.0
        self._anchor = time.monotonic()
        self._audition: int | None = None
        self._slots = {i: _Slot(w) for i, w in self.camera_windows.items()}
        self._program = _Slot(self.program_window)
        self._program_standby = (_Slot(self.program_standby_window)
                                 if self.program_standby_window is not None else None)
        self._awaiting_program_swap = None
        self._comparison_enabled = False
        self._comparison = _Slot(self.comparison_window) if self.comparison_window is not None else None
        self._comparison_standby = (_Slot(self.comparison_standby_window)
                                     if self.comparison_standby_window is not None else None)
        self._awaiting_comparison_swap = None
        self._audio = _Slot(self.audio_window)
        self._init_gpu_live()
        self._audio_duration: float | None = None
        self._audio_duration_path = ""
        self._audio_offset = 0.0
        # A rendered programme excerpt carries exactly the export treatment.
        # It replaces only the programme picture; camera viewers and master
        # sound continue on the same project clock.
        self._render_preview: tuple[str, float, float] | None = None
        self._transition_previews = []
        self._transition_signature = None
        self._grade_draft = None
        self._grade_engine = RealtimeGrade()
        self._subtitle_cache: tempfile.TemporaryDirectory | None = None
        self._subtitle_key: str | None = None
        self._subtitle_ass_path = ""
        self._last_drift = 0.0
        self._reported: set[str] = set()
        try:
            while not self._closing.is_set():
                began = time.monotonic()
                try:
                    action, kwargs = self._commands.get(timeout=self.TICK)
                    if action == "close":
                        break
                    # Consecutive seeks from a dragged slider do not load stale frames.
                    actions = [(action, kwargs)]
                    while not self._commands.empty():
                        following = self._commands.get_nowait()
                        if (following[0] in {"seek", "grade_preview", "camera_windows"}
                                and following[0] == actions[-1][0]):
                            if following[0] == 'camera_windows':
                                # Tk has already retired the superseded page's
                                # parents, even if no decoder ever used them.
                                # Carry their HWNDs into the surviving action
                                # so its release acknowledgement covers them.
                                previous = actions[-1][1]
                                obsolete = [*previous.get('superseded_windows', ()),
                                            *self._checked_camera_windows(previous.get('windows')).values(),
                                            *following[1].get('superseded_windows', ())]
                                following[1]['superseded_windows'] = tuple(dict.fromkeys(obsolete))
                            actions[-1] = following
                        else:
                            actions.append(following)
                    for action, kwargs in actions:
                        if self._closing.is_set():
                            break
                        try:
                            self._dispatch(action, kwargs)
                        except Exception as exc:
                            # A rejected preview must not discard a subsequent
                            # queued pause/seek command from the UI.
                            self._error("预览操作失败", exc)
                            self._playing = False
                            self._pause_all()
                except queue.Empty:
                    pass
                except Exception as exc:
                    self._error("预览操作失败", exc)
                    self._playing = False
                    self._pause_all()
                if self._closing.is_set():
                    break
                try:
                    if self._project is not None:
                        self._tick()
                except Exception as exc:
                    self._error("同步预览失败", exc)
                    self._playing = False
                    self._pause_all()
                self._state()
                # Queue waits are interruptible; MPV work is never done on Tk's thread.
                elapsed = time.monotonic() - began
                if elapsed < self.TICK:
                    self._closing.wait(self.TICK - elapsed)
        finally:
            for slot in self._all_slots():
                if slot.controller is not None:
                    self._dispose(slot.controller)
            if self._gpu_session is not None:
                self._gpu_session.close()
                self._gpu_session = None
            if self._subtitle_cache is not None:
                self._subtitle_cache.cleanup()
                self._subtitle_cache = None
            self._subtitle_ass_path = ""
            self._grade_engine.close()
            self._done.set()

    def _dispatch(self, action: str, values: dict) -> None:
        if action == "camera_windows":
            windows = self._checked_camera_windows(values.get('windows'))
            previous = self._slots
            replacements = {}
            released = []
            for camera, slot in previous.items():
                if windows.get(camera) == slot.window:
                    replacements[camera] = slot
                    continue
                if slot.controller is not None:
                    self._dispose(slot.controller)
                    forget = getattr(self._grade_engine, 'forget', None)
                    if callable(forget): forget(slot.controller)
                released.append(slot.window)
            self.camera_windows = windows
            self._slots = {camera: replacements.get(camera, _Slot(window)) for camera, window in windows.items()}
            kept = set(windows.values())
            released.extend(window for window in values.get('superseded_windows', ())
                            if window not in kept and window not in released)
            # Tk keeps retired parent frames alive until every old decoder
            # has finished using its HWND.  No program/audio slot is touched.
            self._emit('camera_windows_released', released)
            # The next tick advances programme/audio before opening thumbnails.
        elif action == "configure":
            position = self._clock() if self._project is not None else 0.0
            self._project = values["project"]
            signature = transition_signature(self._project)
            if signature != self._transition_signature:
                self._transition_previews = []
            self._transition_signature = signature
            self._grade_draft = None
            # Any published edit can change colour, transitions or subtitles.
            # Never keep a rendered excerpt after the project has changed.
            self._render_preview = None
            self._prepare_subtitles()
            self._use_proxies = bool(values.get("use_proxies", True))
            self._position = min(position, self._duration())
            self._anchor = time.monotonic()
            if 'gpu_live' in values:
                if type(values['gpu_live']) is not bool: raise ValueError('GPU 实时预览开关必须是布尔值。')
                self._gpu_live_requested = values['gpu_live']
            self._configure_gpu_live(self._gpu_live_requested)
            # Editing a cut republishes the model frequently.  An unchanged
            # file keeps its running decoder and clock; only an actual file
            # change below freezes playback while it is being opened.
            self._sync()
        elif action == "transition_previews":
            if values.get("signature") != self._transition_signature:
                return  # A newer edit won while the short windows rendered.
            items = values.get("items", [])
            if not isinstance(items, (list, tuple)) or len(items) > 100000:
                raise ValueError("转场预览列表无效")
            checked = []
            previous_end = -1.0
            for item in items:
                if not isinstance(item, dict): raise ValueError("转场预览结构无效")
                path = item.get('path')
                first, last = item.get('start'), item.get('end')
                if (not isinstance(path, str) or '\x00' in path or not Path(path).is_file()
                        or isinstance(first, bool) or isinstance(last, bool)
                        or not isinstance(first, (int, float)) or not isinstance(last, (int, float))
                        or not math.isfinite(first) or not math.isfinite(last)
                        or first < 0 or last <= first or last > self._duration()+1e-6 or first < previous_end):
                    raise ValueError("转场预览范围或文件无效")
                checked.append((path, float(first), float(last)))
                previous_end = float(last)
            self._transition_previews = checked
            self._position = self._clock()
            self._anchor = time.monotonic()
            self._sync()
            self._emit("status", f"转场预览已就绪 · {len(checked)} 处")
        elif action == "program_swap_ack":
            if values.get("window") == self._awaiting_program_swap:
                self._awaiting_program_swap = None
        elif action in ('resize', 'gpu_resize'):
            if self._gpu_live_enabled:
                self._gpu_session.resize(values['width'], values['height'])
        elif action == "comparison_swap_ack":
            if values.get("window") == self._awaiting_comparison_swap:
                self._awaiting_comparison_swap = None
        elif action == "stabilization_compare":
            enabled = values.get('enabled', False)
            if type(enabled) is not bool:
                raise ValueError('防抖对比开关必须是布尔值。')
            if enabled and self._comparison is None:
                raise ValueError('未提供原片对比窗口。')
            if enabled == self._comparison_enabled:
                return
            self._position = self._clock()
            self._anchor = time.monotonic()
            self._comparison_enabled = enabled
            if enabled:
                # Both panels must show the same live treatment; a baked
                # transition/excerpt would confound a geometry comparison.
                self._render_preview = None
            else:
                self._dispose_comparison()
            self._sync(force=self._program_standby is None)
            self._emit('status', '原片 / 防抖后同步对比' if enabled else '已关闭防抖对比')
        elif action == "grade_preview":
            target = validate_preview_target(values.get("target", ("global",)))
            grade = values.get("grade")
            if grade is not None:
                validate_grade(grade, check_lut=False)
                if self._project is None:
                    raise ValueError("请先载入工程，再预览调色。")
                if target[0] == "camera" and target[1] not in _get(self._project, "cameras", []):
                    raise ValueError("实时调色引用了不存在的机位。")
            self._grade_draft = (target, grade) if grade is not None else None
            if (self._gpu_live_enabled and grade is not None and grade.enabled and grade.lut_path
                    and not self._gpu_session.capabilities.get('lut')):
                # The native session must not silently discard a draft LUT.
                # Keep the same draft and publish it through the old renderer.
                self._configure_gpu_live(True)
                self._sync()
                return
            if grade is not None and (self._render_preview is not None or self._is_baked_key(self._program.key)):
                was_rendered = self._is_baked_key(self._program.key)
                self._render_preview = None
                if was_rendered:
                    # A baked excerpt cannot receive another grade without
                    # doubling the saved treatment. Return to raw once at
                    # the start of the gesture, then keep this decoder.
                    self._position = self._clock()
                    self._anchor = time.monotonic()
                    self._sync(force=self._program_standby is None)
                    self._emit("status", "已返回原片，实时调色中")
            # Uniform updates are independent of decoder/clock operations.
            # In particular a paused drag never reloads or seeks a video.
            self._sync_grades()
        elif action == "render_preview":
            if self._project is None:
                raise ValueError("请先载入工程，再预览效果。")
            try:
                path = os.fspath(values.get("path", ""))
            except TypeError as exc:
                raise ValueError("效果预览文件路径无效。") from exc
            if not isinstance(path, str) or not path.strip() or "\x00" in path:
                raise ValueError("效果预览文件路径无效。")
            try:
                start, end = float(values["start"]), float(values["end"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ValueError("效果预览时间范围无效。") from exc
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                raise ValueError("效果预览结束必须晚于开始，且时间必须有效。")
            if start >= self._duration():
                raise ValueError("效果预览开始时间超过工程时长。")
            if self._comparison_enabled:
                self._comparison_enabled = False
                self._dispose_comparison()
            self._render_preview = (path, start, end)
            self._position = start
            self._anchor = time.monotonic()
            self._playing = bool(values.get("play", True))
            self._sync(force=True)
        elif action == "seek":
            self._position = min(self._duration(), max(0.0, _number(values.get("time"))))
            self._anchor = time.monotonic()
            self._sync(force=True)
        elif action == "play" and self._project is not None:
            if self._position >= self._duration():
                self._position = 0.0
            self._anchor = time.monotonic()
            self._playing = True
            self._sync(force=True)
        elif action == "pause":
            self._position = self._clock()
            self._anchor = time.monotonic()
            self._playing = False
            self._pause_all()
        elif action == "audition":
            camera = values.get("camera")
            self._audition = None if camera is None else int(camera)
            self._position = self._clock()
            self._anchor = time.monotonic()
            self._sync(force=self._program_standby is None)
        elif action not in {"close", "play"}:
            raise ValueError(f"未知预览操作：{action}")

    def _duration(self) -> float:
        duration = _get(self._project, "duration", 0.0)
        return max(0.0, _number(duration() if callable(duration) else duration))

    @staticmethod
    def _checked_camera_windows(windows) -> dict[int, int]:
        if not isinstance(windows, dict) or len(windows) > 9:
            raise ValueError('同一监看页最多显示 9 个机位')
        if any(type(camera) is not int or camera <= 0 or type(window) is not int or window <= 0
               for camera, window in windows.items()):
            raise ValueError('机位监看窗口或编号无效')
        if len(set(windows.values())) != len(windows):
            raise ValueError('不同机位不能使用同一个监看窗口')
        return dict(windows)

    def _camera_at(self, position: float) -> int:
        if self._audition is not None:
            return self._audition
        method = _get(self._project, "camera_at")
        if callable(method):
            selected = method(position)
            return int(selected) if selected is not None else 0
        cameras = _get(self._project, "cameras", [])
        return int(cameras[0] if isinstance(cameras[0], int) else _get(cameras[0], "id", 1)) if cameras else 0

    def _camera(self, camera_id: int) -> Any:
        cameras = _get(self._project, "cameras", [])
        if isinstance(cameras, dict):
            return cameras.get(camera_id) or cameras.get(str(camera_id))
        return next((c for c in cameras if int(_get(c, "id", -1)) == camera_id), None)

    def _clip(self, camera_id: int, position: float) -> tuple | None:
        media_at = _get(self._project, 'media_at')
        if callable(media_at):
            found = media_at(camera_id, position, use_proxies=self._use_proxies)
            if found is None: return None
            path, source_time, key = found
            return key, str(path), max(0.0, float(source_time))
        method = _get(self._project, "clip_at")
        camera = self._camera(camera_id)
        if callable(method):
            found = method(camera_id, position)
        elif camera is not None and callable(_get(camera, "clip_at")):
            found = camera.clip_at(position)
        else:
            found = None
        if found is None:
            return None
        # A model may return the clip alone or (clip, source_seconds).
        if isinstance(found, tuple):
            clip, source_time = found[0], _number(found[1])
        else:
            clip = found
            start = _number(_get(clip, "start", _get(clip, "timeline_start", 0.0)))
            source_in = _number(_get(clip, "source_in", _get(clip, "in_point", 0.0)))
            source_time = position - start + source_in
        original = str(_get(clip, "path", _get(clip, "source_path", "")))
        proxy = _get(clip, "proxy_path", _get(clip, "proxy", ""))
        if self._use_proxies and not proxy and original:
            from proxy_cache import find_proxy
            proxy=find_proxy(original)
        if self._use_proxies and not proxy and original:
            source = Path(original)
            discovered = source.parent / "ProxyMedia" / (source.stem + ".mp4")
            if discovered.is_file():
                proxy = str(discovered)
        path = str(proxy) if self._use_proxies and proxy and Path(str(proxy)).is_file() else original
        if not path:
            return None
        key = (path, _get(clip, "id", None), _get(clip, "start", _get(clip, "timeline_start", None)))
        return key, path, max(0.0, source_time)

    def _audio_spec(self, position: float) -> tuple | None:
        master = _get(self._project, "master_audio", "")
        path = str(master if isinstance(master, (str, os.PathLike)) else _get(master, "path", ""))
        self._audio_offset = _number(_get(self._project, "audio_offset", _get(master, "offset", 0.0)))
        source = position - self._audio_offset
        if not path or source < 0:
            return None
        declared_duration = _number(_get(self._project, "master_duration", 0.0))
        if declared_duration > 0 and source >= declared_duration:
            return None
        if self._audio_duration_path == path and self._audio_duration is not None and source >= self._audio_duration:
            return None
        return (path,), path, source

    def _comparison_spec(self, camera_id: int, position: float) -> tuple | None:
        """Read the original sensor view, bypassing proxy/stabilized assets."""
        method = _get(self._project, 'clip_at')
        if not callable(method): return None
        found = method(camera_id, position)
        if found is None: return None
        if isinstance(found, tuple):
            clip, source_time = found[0], _number(found[1])
        else:
            clip = found
            source_time = (position - _number(_get(clip,'start',_get(clip,'timeline_start',0.0)))
                           + _number(_get(clip,'source_in',_get(clip,'in_point',0.0))))
        path = str(_get(clip,'path',_get(clip,'source_path','')))
        if not path: return None
        key = ('comparison-original',path,_get(clip,'id'),_get(clip,'start',_get(clip,'timeline_start')))
        return key,path,max(0.0,source_time)

    def _program_spec(self, camera: int, position: float) -> tuple | None:
        if self._comparison_enabled:
            return self._clip(camera, position)
        preview = self._render_preview
        if preview is not None:
            path, start, end = preview
            if start <= position < end:
                return ("render-preview", path, start, end), path, position - start
        if self._audition is None and self._grade_draft is None and not self._gpu_live_enabled:
            for path, start, end in self._transition_previews:
                if start <= position < end:
                    return ("transition-preview", path, start, end), path, position-start
        return self._clip(camera, position)

    @staticmethod
    def _is_baked_key(key):
        return bool(key and key[0] in {'render-preview', 'transition-preview'})

    def _prepare_subtitles(self) -> None:
        """Materialise styled track data on the worker, only when it changed."""
        track = _get(self._project, "subtitles")
        if track is None:
            self._subtitle_ass_path, self._subtitle_key = "", None
            return
        payload = subtitle_track_to_dict(track)
        if not payload["enabled"] or not payload["cues"]:
            self._subtitle_ass_path, self._subtitle_key = "", None
            return
        signature = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                              sort_keys=True).encode("utf-8")).hexdigest()
        if signature == self._subtitle_key and self._subtitle_ass_path:
            return
        if self._subtitle_cache is None:
            from cache_paths import cache_dir
            self._subtitle_cache = tempfile.TemporaryDirectory(prefix="okk_preview_subtitles_",dir=cache_dir('previews/subtitles'),
                                                                 ignore_cleanup_errors=True)
        # A new name makes MPV read changed styles even when the programme's
        # camera file stays loaded. All generated files belong to this player.
        path = Path(self._subtitle_cache.name) / f"{signature}.ass"
        write_ass(track, path, 1920, 1080)
        self._subtitle_ass_path, self._subtitle_key = str(path), signature

    def _sync_program_subtitles(self, spec: tuple, newly_loaded: bool, slot: _Slot | None = None,
                                timeline_position: float | None = None) -> None:
        slot = self._program if slot is None else slot
        controller = slot.controller
        if controller is None:
            return
        if newly_loaded:
            slot.subtitle_key, slot.subtitle_id = None, None
        rendered = self._is_baked_key(spec[0])
        # ASS cue stamps are on the global timeline. The source file clock is
        # timeline - clip.start, so subtitles receive the same -clip.start.
        delay = round(spec[2] - (self._position if timeline_position is None else timeline_position), 8)
        desired = ((self._subtitle_ass_path, delay)
                   if self._subtitle_ass_path and not rendered else ("", 0.0))
        if desired == slot.subtitle_key:
            return
        try:
            if slot.subtitle_id is not None:
                controller.command(["sub-remove", slot.subtitle_id])
                slot.subtitle_id = None
            controller.command(["set_property", "sid", "no"])
            controller.command(["set_property", "sub-delay", desired[1]])
            if desired[0]:
                controller.command(["set_property", "sub-ass-override", "no"])
                controller.command(["sub-add", desired[0], "select", "工程字幕"])
                selected = controller.command(["get_property", "sid"])
                if isinstance(selected, int) and not isinstance(selected, bool) and selected > 0:
                    slot.subtitle_id = selected
            slot.subtitle_key = desired
        except Exception as exc:
            # Subtitle failures must not blank an otherwise usable video.
            slot.subtitle_key = desired
            self._error("字幕预览失败", exc)

    def _clock(self, *, update_position: bool = True) -> float:
        value = self._position + (time.monotonic() - self._anchor if self._playing else 0.0)
        if self._playing and self._audio.active and self._audio.controller:
            try:
                controller = self._audio.controller
                if not controller.command(["get_property", "eof-reached"]):
                    sample = controller.command(["get_property", "time-pos"])
                    if sample is not None:
                        value = _number(sample) + self._audio_offset
                        if update_position:
                            self._position, self._anchor = value, time.monotonic()
            except Exception as exc:
                self._audio.failed_key = self._audio.key
                self._blank(self._audio, force=True)
                self._error("主音频时钟不可用，改用本地时钟", exc)
        return max(0.0, min(self._duration(), value))

    def _tick(self) -> None:
        self._position = self._clock()
        self._anchor = time.monotonic()
        if self._playing and self._position >= self._duration():
            self._playing = False
            self._pause_all()
        self._sync()

    def _seek_picture(self, slot: _Slot, source_time: float) -> None:
        self._set_picture_speed(slot, 1.0)
        slot.controller.command(['seek', source_time, 'absolute+exact'])
        slot.last_seek_time = time.monotonic()
        slot.prewarm_target = None

    def _set_picture_speed(self, slot: _Slot, speed: float) -> bool:
        speed = max(1.0, min(1.15, speed))
        if abs(slot.playback_speed-speed) < .001:
            return False
        slot.controller.command(['set_property', 'speed', speed])
        slot.playback_speed = speed
        return True

    def _catch_up_picture(self, slot: _Slot, delta: float) -> bool:
        speed = 1.+min(.15, max(0., delta)*2) if delta > .04 else 1.
        return self._set_picture_speed(slot, speed)

    def _correct_picture_drift(self, slot: _Slot, source_time: float, *, thumbnail=False) -> bool:
        """Do not restart a pending GOP seek or repeatedly chase slow decode."""
        cooldown = self.THUMB_DRIFT_COOLDOWN if thumbnail else self.DRIFT_COOLDOWN
        limit = self.THUMB_DRIFT_LIMIT if thumbnail else self.DRIFT_LIMIT
        now = time.monotonic()
        catching_up = not thumbnail and slot.playback_speed > 1.
        if catching_up and now-slot.last_speed_poll < .1:
            return False
        if not catching_up and now-slot.last_seek_time < cooldown:
            return False
        timeout = .2 if thumbnail else .4
        try:
            if slot.controller.command(['get_property', 'seeking'], timeout=timeout) is True:
                if not thumbnail: self._set_picture_speed(slot, 1.)
                return False
            actual = slot.controller.command(['get_property', 'time-pos'], timeout=timeout)
            if not thumbnail:
                slot.last_speed_poll = now
                # Native video playback catches up without restarting a GOP.
                # A viewer ahead of sound returns to normal speed and waits.
                return self._catch_up_picture(slot, source_time-_number(actual)) if actual is not None else False
            if actual is not None and abs(_number(actual)-source_time) <= limit:
                return False
            self._seek_picture(slot, source_time)
            return True
        except (OSError, RuntimeError) as error:
            # A busy or failed viewer must not stop the master sound or the
            # other running decoders merely because its status query failed.
            slot.last_seek_time = time.monotonic()
            self._error('读取画面同步状态失败，保持当前播放', error)
            return False

    def _prepare_picture_frame(self, slot: _Slot, source_time: float, *, force=False, newly_loaded=False) -> float:
        """Reuse a prewarmed frame, or make one precise seek before handoff."""
        reuse = False
        prepared_position = source_time
        if not force and not newly_loaded:
            actual = slot.controller.command(['get_property', 'time-pos'])
            tolerance = self.DRIFT_LIMIT if self._playing else .5/max(1., _number(_get(self._project, 'fps', 30.), 30.))
            reuse = actual is not None and abs(_number(actual)-source_time) <= tolerance
            if reuse: prepared_position = _number(actual)
        if not reuse:
            self._seek_picture(slot, source_time)
        ready = slot.controller.wait_for_frame(source_time, cancel_event=self._closing)
        return float(ready) if isinstance(ready, (int, float)) else prepared_position

    def _sync(self, force: bool = False) -> None:
        if self._project is None:
            return
        position = self._position
        camera = self._camera_at(position)
        # Thumbnail decoder changes are independent of the programme clock.
        # A new page or another camera's next source must not freeze sound.
        desired = []
        if self._program_standby is None:
            desired.append((self._program, self._program_spec(camera, position), "主输出", False))
        desired.append((self._audio, self._audio_spec(position), "主音频", True))
        loading = any(spec is not None and slot.key != spec[0] and slot.failed_key != spec[0]
                      for slot, spec, _, _ in desired)
        if force or loading:
            self._pause_all()
        drift = time.monotonic() - self._last_drift >= self.DRIFT_INTERVAL
        if drift:
            self._last_drift = time.monotonic()
        for slot, spec, label, audio in desired:
            if self._closing.is_set():
                return
            if spec is None:
                self._blank(slot)
                continue
            key, path, source_time = spec
            if slot.failed_key == key:
                continue
            newly_loaded = slot.key != key
            try:
                if newly_loaded:
                    self._emit("status", f"正在载入{label}：{Path(path).name}")
                    if slot.controller is None:
                        slot.controller = (_AudioController(self.executable, slot.window) if audio
                                           else self._new_program_controller(slot.window))
                    controller = slot.controller
                    controller.start()
                    if self._closing.is_set():
                        return
                    controller.command(["set_property", "pause", True])
                    controller.command(["set_property", "mute", not audio])
                    if audio:
                        controller.command(["set_property", "vid", "no"])
                        controller.command(["loadfile", path, "replace"])
                        self._wait_audio(controller)
                        self._audio_duration = _number(controller.command(["get_property", "duration"]), -1.0)
                        self._audio_duration_path = path
                        if self._audio_duration < 0:
                            self._audio_duration = None
                    else:
                        controller.load(path)
                    if self._closing.is_set():
                        return
                    controller.command(["set_property", "pause", True])
                    controller.command(["set_property", "mute", not audio])
                    slot.key, slot.active, slot.failed_key = key, True, None
                controller = slot.controller
                if controller is None:
                    continue
                if slot is self._program:
                    self._sync_program_subtitles(spec, newly_loaded)
                correct = force or loading or newly_loaded
                if not correct and self._playing:
                    if audio and drift:
                        actual = controller.command(["get_property", "time-pos"])
                        correct = actual is None or abs(_number(actual) - source_time) > self.DRIFT_LIMIT
                    elif not audio and (drift or slot.playback_speed > 1.):
                        self._correct_picture_drift(slot, source_time)
                if correct and not (force or loading):
                    if audio: controller.command(["seek", source_time, "absolute+exact"])
                    else: self._seek_picture(slot, source_time)
                slot.active = True
            except Exception as exc:
                slot.failed_key = key
                self._blank(slot, force=True)
                self._error(f"{label}预览失败（视频须硬件解码，可先生成代理）" if not audio else "主音频预览失败", exc)
        # Exclude startup/loading time from the shared clock, then resume together.
        if force or loading:
            self._anchor = time.monotonic()
            for slot, spec, _, _ in desired:
                if slot.active and slot.controller:
                    try:
                        if spec:
                            if slot is self._audio: slot.controller.command(["seek", spec[2], "absolute+exact"])
                            else: self._seek_picture(slot, spec[2])
                    except Exception as exc:
                        self._error("恢复预览失败", exc)
            for slot, _, _, _ in desired:
                if slot.active and slot.controller:
                    try:
                        slot.controller.command(["set_property", "pause", not self._playing])
                    except Exception as exc:
                        self._error("恢复预览失败", exc)
            self._anchor = time.monotonic()
            self._emit("status", "播放中" if self._playing else "预览已就绪")
        if self._gpu_live_enabled:
            try:
                self._gpu_sync_program(force=force or loading, drift=drift)
            except Exception as error:
                self._gpu_failover(camera, error)
            if not self._gpu_live_enabled and self._program_standby is not None:
                self._sync_double_program(drift=drift)
        elif self._program_standby is not None:
            self._sync_double_program(force=force or loading, drift=drift)
        if self._comparison_enabled:
            self._sync_double_comparison(force=force or loading,drift=drift)
        self._sync_thumbnails(force=force or loading, drift=drift)
        self._sync_grades()

    def _sync_thumbnails(self, *, force=False, drift=False) -> None:
        if self._project is None:
            return
        opened = False
        for camera, slot in self._slots.items():
            if self._closing.is_set(): return
            # Thumbnail calibration may cross a cut after this tick's
            # programme sync. Sample the live clock without publishing a
            # new programme position/camera before its decoder follows.
            position = self._clock(update_position=False) if self._playing else self._position
            spec = self._clip(camera, position)
            if spec is None:
                self._blank(slot)
                continue
            key, path, source_time = spec
            if slot.failed_key == key: continue
            newly_loaded = slot.key != key
            # A nine-camera page must not monopolize the worker across cuts.
            # Keep existing viewers in sync, but open at most one new source
            # before returning to programme/audio and queued UI commands.
            if newly_loaded and opened: continue
            try:
                if newly_loaded:
                    opened = True
                    self._emit('status', f'正在载入机位 {camera}：{Path(path).name}')
                    if slot.controller is None:
                        slot.controller = MpvController(self.executable, slot.window)
                    controller = slot.controller
                    controller.start()
                    self._set_picture_speed(slot, 1.)
                    controller.command(['set_property', 'pause', True])
                    controller.command(['set_property', 'mute', True])
                    controller.load(path)
                    slot.key, slot.failed_key, slot.active = key, None, False
                    if self._closing.is_set(): return
                    # Source loading elapsed while program/audio kept playing.
                    # Seek this decoder to the latest clock, not a stale one.
                    position = self._clock(update_position=False) if self._playing else self._position
                    fresh = self._clip(camera, position)
                    if fresh is None:
                        self._blank(slot)
                        continue
                    if fresh[0] != key: continue
                    source_time = fresh[2]
                controller = slot.controller
                if controller is None: continue
                correct = force or newly_loaded or not slot.active
                if not correct and drift and self._playing:
                    self._correct_picture_drift(slot, source_time, thumbnail=True)
                if correct:
                    self._seek_picture(slot, source_time)
                if force or newly_loaded or not slot.active:
                    controller.command(['set_property', 'pause', not self._playing])
                slot.active = True
            except Exception as exc:
                slot.failed_key = key
                self._blank(slot, force=True)
                self._error(f'机位 {camera} 预览失败（视频须硬件解码，可先生成代理）', exc)

    def _all_slots(self):
        return [*self._slots.values(), self._program, self._audio,
                *([self._program_standby] if self._program_standby is not None else []),
                *([self._comparison] if self._comparison is not None else []),
                *([self._comparison_standby] if self._comparison_standby is not None else [])]

    def _dispose_comparison(self):
        """Release the optional decoders as soon as comparison is hidden."""
        for slot in (self._comparison,self._comparison_standby):
            if slot is None: continue
            if slot.controller is not None:
                self._dispose(slot.controller)
                forget = getattr(self._grade_engine,'forget',None)
                if callable(forget): forget(slot.controller)
            slot.controller = None
            slot.key = slot.failed_key = None
            slot.active = False
            slot.subtitle_key = slot.subtitle_id = None
        self._awaiting_comparison_swap = None

    def _sync_double_comparison(self, *, force=False, drift=False):
        """Keep the original beside stabilized output without freezing sound."""
        current = self._comparison
        if current is None: return
        position = self._clock() if self._playing else self._position
        camera = self._camera_at(position)
        spec = self._comparison_spec(camera,position)
        if spec is None:
            self._blank(current)
            return
        key,path,source_time = spec
        if current.key == key and current.controller is not None:
            self._sync_program_subtitles(spec,False,current,position)
            if force:
                self._seek_picture(current, source_time)
                current.controller.command(['set_property','pause',not self._playing])
            elif self._playing and (drift or current.playback_speed > 1.):
                self._correct_picture_drift(current, source_time)
            return
        if self._awaiting_comparison_swap is not None: return
        staging = self._comparison_standby if current.active and self._comparison_standby is not None else current
        if staging.failed_key == key: return
        try:
            newly_loaded = staging.key != key
            if staging.controller is None: staging.controller=MpvController(self.executable,staging.window)
            controller=staging.controller
            controller.start()
            self._set_picture_speed(staging, 1.)
            controller.command(['set_property','pause',True])
            controller.command(['set_property','mute',True])
            if newly_loaded:
                controller.load(path)
                staging.key,staging.failed_key=key,None
                staging.subtitle_key,staging.subtitle_id=None,None
            if self._closing.is_set(): return
            position=self._clock() if self._playing else self._position
            fresh=self._comparison_spec(self._camera_at(position),position)
            if fresh is None or fresh[0]!=key: return
            spec,source_time=fresh,fresh[2]
            self._sync_program_subtitles(spec,newly_loaded,staging,position)
            for notice in self._grade_engine.apply(controller,
                    self._program_grade_chain(self._camera_at(position),position)):
                self._error('实时调色提示',RuntimeError(notice))
            ready_position=self._prepare_picture_frame(staging, source_time, force=force, newly_loaded=newly_loaded)
            latest_position=self._clock() if self._playing else self._position
            latest=self._comparison_spec(self._camera_at(latest_position),latest_position)
            if latest is None or latest[0]!=key: return
            if self._closing.is_set(): return
            staging.active=True
            staging.last_seek_time=time.monotonic()
            staging.prewarm_target=None
            if self._playing: self._catch_up_picture(staging, latest[2]-ready_position)
            controller.command(['set_property','pause',not self._playing])
            if staging is not current:
                self._comparison,self._comparison_standby=staging,current
            self._position,self._anchor=latest_position,time.monotonic()
            self._awaiting_comparison_swap=staging.window
            self._emit('comparison_swap',staging.window)
            if current is not staging and current.controller is not None:
                self._set_picture_speed(current, 1.)
                current.controller.command(['set_property','pause',True])
        except Exception as exc:
            staging.failed_key=key
            self._error('原片对比画面载入失败，已保留当前画面',exc)

    def _sync_double_program(self, *, force: bool = False, drift: bool = False) -> None:
        """Prepare a new decoder behind the visible one, then publish a swap."""
        position = self._clock() if self._playing else self._position
        camera = self._camera_at(position)
        spec = self._program_spec(camera, position)
        if spec is None:
            self._blank(self._program)
            return
        key, path, source_time = spec
        current = self._program
        if current.key == key and current.controller is not None:
            self._sync_program_subtitles(spec, False)
            if force:
                self._seek_picture(current, source_time)
                current.controller.command(["set_property", "pause", not self._playing])
            elif self._playing and (drift or current.playback_speed > 1.):
                self._correct_picture_drift(current, source_time)
            if not force:
                self._prewarm_program(position)
            return
        if self._awaiting_program_swap is not None:
            # Tk must raise the prepared HWND before the old visible buffer
            # can be reused. This handshake also covers a busy UI event loop.
            return
        staging = self._program_standby if current.active else current
        if staging.failed_key == key:
            return
        try:
            newly_loaded = staging.key != key
            if staging.controller is None:
                staging.controller = self._new_program_controller(staging.window)
            controller = staging.controller
            controller.start()
            self._set_picture_speed(staging, 1.)
            controller.command(["set_property", "pause", True])
            controller.command(["set_property", "mute", True])
            if newly_loaded:
                controller.load(path)
                staging.key, staging.failed_key = key, None
                staging.subtitle_key, staging.subtitle_id = None, None
            if self._closing.is_set():
                return
            # Loading may take several hundred ms. The independent master
            # audio and visible decoder keep running during that time.
            position = self._clock() if self._playing else self._position
            fresh = self._program_spec(self._camera_at(position), position)
            if fresh is None or fresh[0] != key:
                return  # The requested shot already ended; do not flash it.
            spec, source_time = fresh, fresh[2]
            self._sync_program_subtitles(spec, newly_loaded, staging, position)
            for notice in self._grade_engine.apply(controller,
                    self._program_grade_chain(self._camera_at(position), position),
                    bypass=self._is_baked_key(key)):
                self._error("实时调色提示", RuntimeError(notice))
            ready_position = self._prepare_picture_frame(staging, source_time, force=force, newly_loaded=newly_loaded)
            latest_position = self._clock() if self._playing else self._position
            latest = self._program_spec(self._camera_at(latest_position), latest_position)
            if latest is None or latest[0] != key:
                return
            if self._closing.is_set():
                return
            staging.active = True
            staging.last_seek_time = time.monotonic()
            staging.prewarm_target = None
            if self._playing: self._catch_up_picture(staging, latest[2]-ready_position)
            controller.command(["set_property", "pause", not self._playing])
            if staging is not current:
                self._program, self._program_standby = staging, current
            self._position, self._anchor = latest_position, time.monotonic()
            self._awaiting_program_swap = staging.window
            self._emit("program_swap", staging.window)
            if current is not staging and current.controller is not None:
                # Keep its last frame intact underneath until the next load.
                self._set_picture_speed(current, 1.)
                current.controller.command(["set_property", "pause", True])
        except Exception as exc:
            staging.failed_key = key
            self._error("备用主画面载入失败，已保留当前画面", exc)

    def _prewarm_program(self, position):
        """Prepare the next short effect/source before its exact boundary.

        MPV opening a 0.5s transition at its first frame can otherwise spend
        much of that window loading.  Readiness still uses the normal swap
        handshake; this only warms the hidden decoder while sound continues.
        """
        staging = self._program_standby
        if staging is None or self._awaiting_program_swap is not None:
            return
        edges = []
        for _, first, last in self._transition_previews:
            edges.extend((first, last))
        for cut in _get(self._project, 'cuts', []):
            edges.append(_get(cut, 'time', 0.0))
        for clip in _get(self._project, 'clips', []):
            start = _get(clip, 'start', 0.0)
            edges.extend((start, start+_get(clip, 'duration', 0.0)))
        for edge in sorted(set(value for value in edges if position+1e-6 < value <= position+self.PREWARM_SECONDS)):
            spec = self._program_spec(self._camera_at(edge), edge)
            if spec is None or spec[0] == self._program.key:
                continue
            key, path, source_time = spec
            target = (key, source_time)
            if staging.failed_key == key or (staging.key == key and staging.prewarm_target == target):
                return
            try:
                if staging.controller is None:
                    staging.controller = self._new_program_controller(staging.window)
                controller = staging.controller
                controller.start()
                self._set_picture_speed(staging, 1.)
                if self._gpu_live_enabled:
                    self._gpu_pause(staging, True, force=True)
                else:
                    controller.command(['set_property', 'pause', True])
                controller.command(['set_property', 'mute', True])
                if staging.key != key:
                    controller.load(path)
                    staging.key, staging.failed_key = key, None
                    staging.subtitle_key, staging.subtitle_id = None, None
                self._seek_picture(staging, source_time)
                staging.prewarm_target = target
                if self._gpu_live_enabled:
                    # It owns a future frame, rather than an active clock.
                    # Handoff verifies this frame without a second GOP seek.
                    staging.active = False
            except Exception as exc:
                staging.failed_key = key
                if self._gpu_live_enabled:
                    self._gpu_failover(self._camera_at(edge), exc)
                else:
                    self._error('后台准备下一画面失败，切换时将保留当前画面', exc)
            return

    def _program_grade_chain(self, camera, position):
        saved = _get(self._project, "camera_grades", {})
        camera_grade = saved.get(camera, saved.get(str(camera)))
        local_grade = (grade_at(self._project, position) if _get(self._project, "clip_grades", []) else None)
        global_grade = _get(self._project, "global_grade", ColorGrade())
        draft = self._grade_draft
        if draft:
            if draft[0] == ("camera", camera):
                camera_grade = draft[1]
            elif draft[0][0] == "global":
                global_grade = draft[1]
            elif draft[0][0] == "selection" and draft[0][1] <= position < draft[0][2]:
                local_grade = draft[1]
        return camera_grade, local_grade, global_grade

    def _sync_grades(self) -> None:
        if self._project is None:
            return
        saved = _get(self._project, "camera_grades", {})
        global_grade = _get(self._project, "global_grade", ColorGrade())
        local_grade = (grade_at(self._project, self._position)
                       if _get(self._project, "clip_grades", []) else None)
        draft = self._grade_draft

        def camera_grade(camera):
            if draft and draft[0] == ("camera", camera):
                return draft[1]
            return saved.get(camera, saved.get(str(camera)))

        for camera, slot in self._slots.items():
            if slot.active and slot.controller is not None:
                try:
                    for notice in self._grade_engine.apply(slot.controller, (camera_grade(camera),)):
                        self._error("实时调色提示", RuntimeError(notice))
                except Exception as exc:
                    self._error(f"机位 {camera} 实时调色失败", exc)
        slot = self._program
        if slot.active and slot.controller is not None and not self._gpu_live_enabled:
            rendered = self._is_baked_key(slot.key)
            if draft:
                if draft[0][0] == "global":
                    global_grade = draft[1]
                elif draft[0][0] == "selection":
                    first, last = draft[0][1:]
                    if first <= self._position < last:
                        local_grade = draft[1]
            try:
                for notice in self._grade_engine.apply(slot.controller,
                        (camera_grade(self._camera_at(self._position)), local_grade, global_grade), bypass=rendered):
                    self._error("实时调色提示", RuntimeError(notice))
            except Exception as exc:
                self._error("主输出实时调色失败", exc)
        slot = self._comparison
        if self._comparison_enabled and slot is not None and slot.active and slot.controller is not None:
            try:
                for notice in self._grade_engine.apply(slot.controller,
                        self._program_grade_chain(self._camera_at(self._position),self._position)):
                    self._error('实时调色提示',RuntimeError(notice))
            except Exception as exc:
                self._error('原片对比实时调色失败',exc)
        if self._gpu_live_enabled:
            try:
                self._gpu_refresh_grades()
            except Exception as error:
                self._gpu_failover(self._camera_at(self._position), error)

    def _wait_audio(self, controller: MpvController) -> None:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if self._closing.is_set():
                raise RuntimeError("正在关闭预览。")
            try:
                if not controller.command(["get_property", "idle-active"]):
                    if controller.command(["get_property", "duration"]) is not None:
                        return
            except RuntimeError:
                pass
            time.sleep(0.05)
        raise RuntimeError("等待主音频载入超时。")

    def _blank(self, slot: _Slot, force: bool = False) -> None:
        if slot.key is None and not slot.active and not force:
            return
        if slot.controller is not None:
            try:
                if slot is not self._audio: self._set_picture_speed(slot, 1.)
                slot.controller.command(["stop"])
            except Exception as exc:
                self._error("清空预览失败", exc)
        slot.key, slot.active = None, False
        slot.prewarm_target = None
        slot.subtitle_key, slot.subtitle_id = None, None

    def _pause_all(self) -> None:
        for slot in self._all_slots():
            if slot.controller is not None and slot.active:
                try:
                    if slot is not self._audio: self._set_picture_speed(slot, 1.)
                    slot.controller.command(["set_property", "pause", True])
                except Exception as exc:
                    self._error("暂停预览失败", exc)

    def _state(self) -> None:
        camera = self._camera_at(self._position) if self._project is not None else 0
        preview = self._render_preview
        current_picture = self._clip(camera,self._position) if self._comparison_enabled and self._project is not None else None
        self._emit("state", {"time": self._position, "playing": self._playing,
                             "camera": camera, "ready": self._project is not None and not self._program.failed_key,
                             "preview": bool(preview and preview[1] <= self._position < preview[2]),
                             "transition_preview": bool(self._program.key and self._program.key[0] == 'transition-preview'),
                             "comparison_enabled":self._comparison_enabled,
                             "comparison_ready":bool(self._comparison_enabled and self._comparison and self._comparison.active),
                             "gpu_live": self._gpu_live_enabled,
                             "live_transition": (self._gpu_context['plan'].kind if self._gpu_context and self._gpu_context['plan'].contains(self._position) else None),
                             "stabilization_active":bool(current_picture and current_picture[0][0]=='stabilized'
                                                         and self._program.active and self._program.key==current_picture[0])})

    @staticmethod
    def _dispose(controller: MpvController) -> None:
        if getattr(controller, 'release_only', False):
            controller.stop()
            return
        process = controller.process
        try:
            controller.stop()
        except Exception:
            pass
        if process is not None:
            try:
                process.wait(timeout=0.7)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    process.kill()
                    process.wait(timeout=0.7)
                except (OSError, subprocess.TimeoutExpired):
                    pass
