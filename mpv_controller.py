from __future__ import annotations

import json
import math
import ctypes
import os
import subprocess
import threading
import time
import uuid
from typing import BinaryIO

from exporter_core import CREATE_NO_WINDOW


MPV_SCREENSHOT_OPTIONS = [
    "--screenshot-format=jpg",
    "--screenshot-jpeg-quality=98",
    "--screenshot-jpeg-source-chroma=yes",
    # Keep the source/HDR calculations in the GPU renderer, then convert the
    # finished frame to widely compatible 8-bit sRGB for JPEG output.
    "--screenshot-tag-colorspace=no",
    "--screenshot-high-bit-depth=no",
    "--screenshot-sw=no",
]


# Use the native D3D11 decoding path with the D3D11 renderer on Windows.
# A failed GPU decoder must not silently turn an 8K thumbnail into CPU work.
MPV_GPU_PLAYBACK_OPTIONS = [
    "--hwdec=d3d11va,nvdec",
    "--hwdec-codecs=all",
    "--hwdec-software-fallback=no",
    "--vo=gpu-next",
    "--gpu-api=d3d11",
    "--gpu-context=d3d11",
    "--profile=fast",
    # Bound compressed demux packets per viewer. Decoder surfaces, GPU
    # textures and the rest of each process have separate memory costs.
    "--demuxer-max-bytes=32MiB",
    "--demuxer-max-back-bytes=8MiB",
    "--demuxer-readahead-secs=2",
    "--cache-on-disk=no",
]


class MpvController:
    def __init__(self, executable: str, window_id: int, *, allow_software_decode: bool = False) -> None:
        self.executable = executable
        self.window_id = int(window_id) & 0xFFFFFFFF
        self.allow_software_decode = bool(allow_software_decode)
        self.pipe_name = rf"\\.\pipe\okk_mpv_{os.getpid()}_{uuid.uuid4().hex}"
        self.process: subprocess.Popen | None = None
        self._pipe: BinaryIO | None = None
        self._lock = threading.Lock()
        self._read_buffer = bytearray()
        self._request_id = 0
        self._event_counts = {}
        self.operation_cancel_event = None
        # The aligned-preview endpoint is enforced by the application poller.
        # MPV exposes `end` as a file-local option, but some Windows builds do
        # not expose it as a writable IPC property and return
        # "error accessing property" when set_property is used.
        self.preview_end_seconds: float | None = None

    def start(self, timeout: float = 8.0) -> None:
        if os.name != "nt":
            raise RuntimeError("内嵌 MPV 播放器当前仅针对 Windows 构建。")
        if self.process and self.process.poll() is None:
            return
        self._close_pipe()
        gpu_options = [
            "--hwdec-software-fallback=yes" if self.allow_software_decode and option == "--hwdec-software-fallback=no"
            else option for option in MPV_GPU_PLAYBACK_OPTIONS
        ]
        try:
            from gpu_d3d11 import preferred_adapter_name
            adapter=preferred_adapter_name()
        except (OSError,RuntimeError,AttributeError):
            adapter=''
        if adapter:
            gpu_options.append('--d3d11-adapter='+adapter)
        args = [
            self.executable,
            "--no-config",
            "--idle=yes",
            "--force-window=yes",
            "--keep-open=yes",
            "--terminal=no",
            "--osc=no",
            "--input-default-bindings=no",
            "--input-vo-keyboard=no",
            "--input-cursor=no",
            f"--wid={self.window_id}",
            f"--input-ipc-server={self.pipe_name}",
        ] + gpu_options + MPV_SCREENSHOT_OPTIONS
        self.process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("MPV 启动失败。请确认 mpv.exe 是可独立运行的 Windows 版本。")
            try:
                self.command(["get_property", "mpv-version"])
                return
            except (OSError, RuntimeError) as exc:
                last_error = exc
                time.sleep(0.1)
        raise RuntimeError(f"连接 MPV 超时：{last_error or '命名管道未就绪'}")

    def _close_pipe(self) -> None:
        pipe = self._pipe
        self._pipe = None
        self._read_buffer.clear()
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass

    def _check_operation_cancelled(self):
        event = getattr(self, 'operation_cancel_event', None)
        if event is not None and event.is_set():
            raise RuntimeError('预览操作已被更新的定位请求取消。')

    def _connect_pipe(self, deadline: float) -> BinaryIO:
        last_error: OSError | None = None
        while time.monotonic() < deadline:
            self._check_operation_cancelled()
            if not self.process or self.process.poll() is not None:
                raise RuntimeError("MPV 尚未启动。")
            try:
                return open(self.pipe_name, "r+b", buffering=0)
            except OSError as exc:
                # MPV may have created the named pipe before it is ready to
                # accept a client. Windows reports either file-not-found or
                # access-denied during that short interval.
                last_error = exc
                time.sleep(0.05)
        raise RuntimeError(f"连接 MPV 通信管道失败：{last_error or '等待超时'}")

    @staticmethod
    def _available_bytes(pipe):
        """Peek before reading a Windows pipe; readline itself has no timeout."""
        if os.name != 'nt':return None
        try:descriptor=pipe.fileno()
        except (AttributeError,OSError,ValueError):return None  # In-memory test streams.
        import msvcrt
        from ctypes import wintypes
        peek=ctypes.windll.kernel32.PeekNamedPipe
        peek.argtypes=[wintypes.HANDLE,wintypes.LPVOID,wintypes.DWORD,
                       ctypes.POINTER(wintypes.DWORD),ctypes.POINTER(wintypes.DWORD),ctypes.POINTER(wintypes.DWORD)]
        peek.restype=wintypes.BOOL
        available=wintypes.DWORD()
        if not peek(msvcrt.get_osfhandle(descriptor),None,0,None,ctypes.byref(available),None):
            raise ctypes.WinError()
        return available.value

    def _read_line(self, deadline):
        while time.monotonic()<deadline:
            self._check_operation_cancelled()
            if b'\n' in self._read_buffer:
                line,_,remaining=self._read_buffer.partition(b'\n')
                self._read_buffer=bytearray(remaining)
                return line
            available=self._available_bytes(self._pipe)
            if available is None:return self._pipe.readline()
            if available:
                data=self._pipe.read(min(available,65536))
                if not data:raise OSError('MPV 通信管道已关闭')
                self._read_buffer.extend(data)
                if len(self._read_buffer)>8*1024*1024:raise RuntimeError('MPV 通信响应过大')
            else:time.sleep(min(.002,max(0.,deadline-time.monotonic())))
        raise TimeoutError('MPV 命令响应超时')

    def command(self, command: list, timeout: float = 3.0):
        if not self.process or self.process.poll() is not None:
            raise RuntimeError("MPV 尚未启动。")
        with self._lock:
            self._request_id+=1
            request_id=self._request_id
            payload = (json.dumps({"command": command,"request_id":request_id}, ensure_ascii=False) + "\n").encode("utf-8")
            deadline = time.monotonic() + timeout
            last_error: OSError | None = None
            while time.monotonic() < deadline:
                try:
                    if self._pipe is None:
                        self._pipe = self._connect_pipe(deadline)
                    self._pipe.write(payload)
                    while time.monotonic() < deadline:
                        line = self._read_line(deadline)
                        if not line:
                            continue
                        response = json.loads(line.decode("utf-8", errors="replace"))
                        if 'event' in response:
                            event=response['event']
                            self._event_counts[event]=self._event_counts.get(event,0)+1
                        if "error" not in response or response.get('request_id')!=request_id:
                            continue
                        if response.get("error") != "success":
                            raise RuntimeError(f"MPV 命令失败：{response.get('error')}")
                        return response.get("data")
                except TimeoutError as exc:
                    self._close_pipe()
                    raise RuntimeError('MPV 命令响应超时，已释放通信连接') from exc
                except OSError as exc:
                    last_error = exc
                    self._close_pipe()
                    time.sleep(0.05)
            if last_error:
                raise RuntimeError(f"MPV 通信失败：{last_error}") from last_error
            self._close_pipe()
        raise RuntimeError("MPV 命令响应超时。")

    def load(self, path: str, timeout: float = 8.0) -> dict[str, str]:
        self._check_operation_cancelled()
        self.preview_end_seconds = None
        generation=self._event_counts.get('file-loaded',0)
        self.command(["loadfile", path, "replace"])
        self.command(["set_property", "pause", True])
        # audio-delay is a global MPV property. Reset it whenever an ordinary
        # file is loaded so a previous alignment preview cannot affect later
        # playback of the camera's own audio.
        self.command(["set_property", "audio-delay", 0.0])
        return self._wait_for_gpu_decoder(timeout,expected_path=path,after_generation=generation)

    def load_aligned_preview(
        self,
        video_path: str,
        audio_path: str,
        audio_delay: float,
        *,
        start_seconds: float = 0.0,
        end_seconds: float | None = None,
        timeout: float = 8.0,
    ) -> dict[str, str]:
        """Load one camera clip and play its independently aligned master audio."""
        if end_seconds is not None and float(end_seconds) <= float(start_seconds):
            raise ValueError("试听结束时间必须晚于开始时间。")
        backend = self.load(video_path, timeout=timeout)
        self.command(["audio-add", audio_path, "select", "已对齐主音频"])
        self.command(["set_property", "audio-delay", float(audio_delay)])
        self.preview_end_seconds = float(end_seconds) if end_seconds is not None else None
        self.command(["seek", max(0.0, float(start_seconds)), "absolute+exact"])
        self.command(["set_property", "pause", False])
        return backend

    def _wait_for_gpu_decoder(self, timeout: float, *, expected_path=None, after_generation=None) -> dict[str, str]:
        """Keep GPU decode strict unless an explicit compatibility path opts in.

        CPU decode is accepted only for a matching, newly loaded file with a
        real video output. Idle or stale decoder state never satisfies load.
        """
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        cpu_fallback: Exception | None = None
        while time.monotonic() < deadline:
            self._check_operation_cancelled()
            try:
                fresh_file = False
                if expected_path is not None:
                    loaded=self.command(['get_property','path'],timeout=min(.5,max(.01,deadline-time.monotonic())))
                    if (not loaded or os.path.normcase(os.path.abspath(str(loaded)))!=os.path.normcase(os.path.abspath(str(expected_path)))
                            or after_generation is None
                            or self._event_counts.get('file-loaded',0)<=after_generation):
                        time.sleep(.025);continue
                    fresh_file = True
                hwdec = str(self.command(["get_property", "hwdec-current"],timeout=min(.5,max(.01,deadline-time.monotonic())) ) or "").lower()
                if hwdec == "no":
                    if self.allow_software_decode and fresh_file:
                        output = self.command(["get_property", "video-out-params"],
                            timeout=min(.5,max(.01,deadline-time.monotonic())))
                        dimensions = (output.get("w"), output.get("h")) if isinstance(output, dict) else ()
                        if len(dimensions) == 2 and all(type(value) in (int,float) and math.isfinite(value) and value > 0
                                                        for value in dimensions):
                            return {"hwdec": "no", "renderer": "gpu-next/d3d11", "decode": "cpu"}
                    # MPV briefly reports "no" while an idle/previous decoder
                    # is being replaced. Do not reject a supported GPU file
                    # before the asynchronous new decoder has initialized.
                    cpu_fallback = RuntimeError(
                        "该素材未能启用 GPU 硬件解码，MPV 已回退到 CPU。"
                        "请更新显卡驱动或换用包含 D3D11VA 的 MPV；"
                        "若素材编码本身不受显卡支持，请先生成 1080p 代理。"
                    )
                elif hwdec:
                    try:
                        interop = str(self.command(["get_property", "hwdec-interop"]) or "")
                    except RuntimeError:
                        interop = ""
                    try:
                        pixel_format = str(
                            self.command(["get_property", "video-params/hw-pixelformat"]) or ""
                        )
                    except RuntimeError:
                        pixel_format = ""
                    return {
                        "hwdec": hwdec,
                        "interop": interop,
                        "pixel_format": pixel_format,
                        "renderer": "gpu-next/d3d11",
                    }
            except RuntimeError as exc:
                last_error = exc
            time.sleep(0.08)
        if cpu_fallback is not None:
            raise cpu_fallback
        raise RuntimeError(f"等待 GPU 硬件解码超时：{last_error or '未读到 hwdec-current'}")

    def wait_for_frame(self, target: float, *, timeout: float = 4.0, cancel_event=None) -> float:
        """Wait for a decoded, rendered seek result before raising its HWND.

        MPV's default playback startup waits for the first rendered frame;
        `seeking` covers both seeking and playback restart. Combine that
        barrier with real output dimensions and gpu-next's fresh render
        passes, rather than treating file-loaded/decoder-open as frame ready.
        https://mpv.io/manual/stable/#properties-seeking
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("正在关闭预览。")
            try:
                seeking = self.command(["get_property", "seeking"], timeout=.7)
                position = self.command(["get_property", "time-pos"], timeout=.7)
                output = self.command(["get_property", "video-out-params"], timeout=.7)
                passes = self.command(["get_property", "vo-passes"], timeout=.7)
                if (seeking is False and isinstance(position, (int, float))
                        and abs(position - target) <= .15
                        and isinstance(output, dict) and output.get("w", 0) > 0 and output.get("h", 0) > 0
                        and isinstance(passes, dict) and passes.get("fresh")):
                    return float(position)
            except (RuntimeError, TypeError, ValueError):
                pass
            if cancel_event is not None:
                cancel_event.wait(.008)
            else:
                time.sleep(.008)
        raise RuntimeError("等待备用画面完成渲染超时；已保留当前画面。")

    def stop(self) -> None:
        process = self.process
        if not process:
            return
        if process.poll() is None:
            try:
                self.command(["quit"], timeout=1.0)
            except Exception:
                try:
                    process.terminate()
                except OSError:
                    pass
        self._close_pipe()
        self.process = None
