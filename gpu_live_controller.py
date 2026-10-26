"""Two libmpv texture channels sharing one in-process D3D11 compositor.

No helper executable is launched. The renderer/Core bridge is injected for
tests or loaded lazily from gpu_live_session in the existing Python process.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import threading
import time
from realtime_grade import grade_uniforms


def grade_layout():
    names = tuple(grade_uniforms(())[0])
    digest = hashlib.sha256('\n'.join(names).encode('utf-8')).hexdigest()
    return names, digest


def validate_capabilities(value):
    if not isinstance(value, dict) or value.get('protocol') != 1:
        raise RuntimeError('GPU 预览通信协议版本不匹配。')
    if set(value.get('slots', ())) != {'a', 'b'}:
        raise RuntimeError('GPU 预览缺少双纹理通道。')
    if value.get('grade_uniforms'):
        names, digest = grade_layout()
        if value.get('grade_count') != len(names):
            raise RuntimeError('GPU 实时调色参数数量不匹配。')
        if value.get('grade_fields') is not None:
            matched = tuple(value['grade_fields']) == names
        else:
            matched = value.get('grade_layout_hash') == digest
        if not matched: raise RuntimeError('GPU 实时调色字段布局未经验证。')
    return dict(value)


class GpuLiveSession:
    """Lease one in-process renderer; never spawn or terminate a process."""
    process = None
    executable = 'inprocess-libmpv'
    pipe_name = ''

    def __init__(self, parent_window, *, runtime=None, session_factory=None):
        self.window_id = int(parent_window)
        self.runtime = str(Path(runtime).resolve()) if runtime else None
        self._factory = session_factory
        self._backend = None
        self._request_id = 0
        self.capabilities = None
        self._leases = {}
        self._lease_lock = threading.RLock()
        self._closed = False
        self._grade_keys = {}

    def start(self, timeout=8.):
        if self._closed: raise RuntimeError('GPU 预览会话已关闭。')
        if self._backend is not None: return
        factory = self._factory
        if factory is None:
            from gpu_live_session import GpuLiveSession as factory
        try:
            self._backend = factory(self.window_id, runtime=self.runtime)
            self._backend.start()
            self.capabilities = validate_capabilities(self.request('capabilities', timeout=timeout))
        except BaseException:
            self.close()
            raise

    def request(self, op, *, timeout=3., **parameters):
        if self._closed or self._backend is None:
            raise RuntimeError('GPU 预览会话未运行。')
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('GPU 通信超时必须为正数。')
        self._request_id += 1
        payload = {'op': op, 'request_id': self._request_id, **parameters}
        try:
            callback = getattr(self._backend, 'request', None)
            if callable(callback):
                return callback(payload, timeout=timeout)
            if op == 'capabilities':
                value = self._backend.capabilities
                return value() if callable(value) else value
            if op == 'command':
                return self._backend.command(parameters['slot'], parameters['command'], timeout=timeout)
            if op == 'frame_state':
                return self._backend.frame_state(parameters['slot'], timeout=timeout)
            if op == 'quit': return self._backend.close()
            return getattr(self._backend, op)(**parameters)
        except (OSError, TimeoutError) as error:
            # Core async futures own their deadlines. Never replay a timed
            # out command, which could execute the same seek twice.
            raise RuntimeError('GPU 预览操作失败或超时，未重复执行请求。') from error

    def acquire(self, slot, owner):
        if slot not in ('a', 'b'): raise ValueError('GPU 纹理通道必须是 a 或 b。')
        with self._lease_lock:
            if slot in self._leases and self._leases[slot] != owner:
                raise RuntimeError('GPU 纹理通道已被其他控制器占用。')
            self.start()
            self._leases[slot] = owner

    def release(self, slot, owner):
        with self._lease_lock:
            if self._leases.get(slot) != owner: return
            del self._leases[slot]
            if self._leases:
                try: self.request('command', slot=slot, command=['stop'], timeout=.5)
                except RuntimeError: pass
            else:
                self.close()

    def frame_state(self, slot, *, timeout=.5):
        value = self.request('frame_state', slot=slot, timeout=timeout)
        if not isinstance(value, dict): raise RuntimeError('GPU 画面状态结构无效。')
        return value

    def grade(self, slot, values, *, lut_paths=None):
        names, digest = grade_layout()
        if tuple(values) != names:
            raise ValueError('GPU 调色字段顺序与原生着色器布局不一致。')
        flat = [float(values[name]) for name in names]
        if not all(math.isfinite(value) for value in flat): raise ValueError('GPU 调色数值必须有限。')
        paths = list(lut_paths or ['', '', ''])
        if len(paths) != 3: raise ValueError('GPU 调色需要三层 LUT 路径。')
        if any(paths) and not self.capabilities.get('lut'):
            raise RuntimeError('原生 GPU 预览尚不支持此工程的 LUT，请使用精确效果预览。')
        key = tuple(flat), tuple(paths)
        if self._grade_keys.get(slot) == key: return None
        result = self.request('grade', slot=slot, values=flat, layout_hash=digest, lut_paths=paths)
        self._grade_keys[slot] = key
        return result

    def compose(self, *, front_slot, other_slot=None, kind='fade', progress=0., active=False,
                subtitle=None, **parameters):
        if front_slot not in ('a', 'b') or other_slot not in (None, 'a', 'b'):
            raise ValueError('GPU 合成通道无效。')
        if active and (other_slot is None or other_slot == front_slot):
            raise ValueError('GPU 转场需要两个不同纹理通道。')
        if not isinstance(progress, (int, float)) or not math.isfinite(progress) or not 0 <= progress <= 1:
            raise ValueError('GPU 转场进度必须在 0 到 1 之间。')
        if active and kind not in self.capabilities.get('transitions', ()):
            raise RuntimeError('原生 GPU 预览不支持所选转场：'+str(kind))
        if subtitle and not self.capabilities.get('subtitles'):
            raise RuntimeError('原生 GPU 预览尚不支持此工程的字幕，请使用精确效果预览。')
        return self.request('compose', front_slot=front_slot, other_slot=other_slot,
            kind=kind, progress=float(progress), active=bool(active), subtitle=subtitle, **parameters)

    def resize(self, width, height):
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            raise ValueError('GPU 预览尺寸必须为正整数。')
        return self.request('resize', width=width, height=height)

    def screenshot(self, path):
        return self.request('screenshot', path=str(Path(path).resolve()))

    def close(self):
        if self._closed: return
        self._closed = True
        self._leases.clear()
        backend, self._backend = self._backend, None
        if backend is not None: backend.close()


class TextureController:
    """MPV-compatible controller interface for one shared texture channel."""
    shared_process = True
    release_only = True

    def __init__(self, session: GpuLiveSession, slot: str, window_id: int):
        if slot not in ('a', 'b'): raise ValueError('GPU 纹理通道必须是 a 或 b。')
        self.session, self.slot = session, slot
        self.window_id = self.window = int(window_id)
        self.executable = session.executable
        self.pipe_name = session.pipe_name
        self._acquired = False
        self._released = False
        self._frame_floor = -1
        self.preview_end_seconds = None

    @property
    def process(self): return self.session.process

    def start(self, timeout=8.):
        if self._released: raise RuntimeError('GPU 纹理通道已释放。')
        if not self._acquired:
            self.session.acquire(self.slot, id(self))
            self._acquired = True

    def command(self, command, timeout=3.):
        self.start()
        if command[:2] == ['set_property', 'glsl-shader-opts']:
            return self.session.grade(self.slot, command[2])
        if command[:2] == ['change-list', 'glsl-shaders']:
            return None  # Native HLSL grading already owns these uniforms.
        if command == ['quit']:
            self.stop(); return None
        if command and command[0] == 'seek':
            self._frame_floor = int(self.session.frame_state(self.slot, timeout=min(.5, timeout)).get('render_generation', -1))
        return self.session.request('command', slot=self.slot, command=list(command), timeout=timeout)

    def set_grades(self, grades):
        grades = list(grades)
        values, _ = grade_uniforms(grades)
        paths = [str(getattr(grade, 'lut_path', '')) if grade is not None and getattr(grade, 'enabled', True) else ''
                 for grade in grades]
        return self.session.grade(self.slot, values, lut_paths=(paths+['', '', ''])[:3])

    def load(self, path, timeout=8.):
        self.start()
        previous = self.session.frame_state(self.slot)
        generation = int(previous.get('file_generation', -1))
        self._frame_floor = int(previous.get('render_generation', -1))
        self.command(['loadfile', str(path), 'replace'])
        self.command(['set_property', 'pause', True])
        deadline = time.monotonic()+timeout
        expected = os.path.normcase(str(Path(path).resolve()))
        while time.monotonic() < deadline:
            state = self.session.frame_state(self.slot, timeout=min(.5, max(.01, deadline-time.monotonic())))
            actual = state.get('path')
            loaded = actual and os.path.normcase(str(Path(actual).resolve())) == expected
            if loaded and int(state.get('file_generation', -1)) > generation:
                hwdec = str(state.get('hwdec') or '').lower()
                if hwdec and hwdec not in ('no', 'none') and 'copy' not in hwdec:
                    return {'hwdec': hwdec, 'renderer': 'native-d3d11-texture'}
            time.sleep(.01)
        raise RuntimeError('GPU 纹理未完成新素材硬件解码；未使用 CPU 回退。')

    def wait_for_frame(self, target, *, timeout=4., cancel_event=None):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set(): raise RuntimeError('正在关闭 GPU 预览。')
            state = self.session.frame_state(self.slot, timeout=min(.4, max(.01, deadline-time.monotonic())))
            position = state.get('time_pos')
            if (state.get('seeking') is False and isinstance(position, (int, float))
                    and abs(position-target) <= .15 and state.get('width', 0) > 0 and state.get('height', 0) > 0
                    and int(state.get('render_generation', -1)) > self._frame_floor):
                return float(position)
            if cancel_event is not None: cancel_event.wait(.008)
            else: time.sleep(.008)
        raise RuntimeError('等待 GPU 纹理新画面超时；保留当前成片画面。')

    def stop(self):
        if self._released: return
        self._released = True
        if self._acquired:
            self.session.release(self.slot, id(self))
            self._acquired = False
