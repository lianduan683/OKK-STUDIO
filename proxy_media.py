"""Checks for manually attached proxies, performed away from the UI thread."""
from __future__ import annotations

import math
import os
from pathlib import Path
import stat
import time

from exporter_core import ExportCancelled, MediaInfo, probe_media
from proxy_cache import find_proxy


class PreviewProxyResolver:
    """Bound disk lookups during playback; never used by an export renderer."""

    def __init__(self, refresh_seconds: float = 0.75):
        self.refresh_seconds = refresh_seconds
        self._cache = {}
        self._rejected = {}

    @staticmethod
    def _identity(path):
        return os.path.normcase(os.path.abspath(path))

    def reset(self):
        self._cache.clear()

    def invalidate_source(self, source):
        identity = self._identity(source)
        for key in list(self._cache):
            if self._identity(key[0]) == identity:
                self._cache.pop(key, None)

    @staticmethod
    def _stamp(path):
        try:
            info = Path(path).stat()
            return (info.st_size, info.st_mtime_ns) if stat.S_ISREG(info.st_mode) else None
        except (OSError, ValueError):
            return None

    def reject(self, path):
        self._rejected[self._identity(path)] = self._stamp(path)
        self._cache.clear()

    def __call__(self, source: str, explicit: str = '') -> str:
        key = (source, explicit)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and now < cached[0]:
            return cached[1]
        candidate = explicit
        if not candidate and source:
            candidate = find_proxy(source)
            if not candidate:
                item = Path(source)
                candidate = str(item.parent / 'ProxyMedia' / (item.stem + '.mp4'))
        result = source
        try:
            identity = self._identity(candidate) if candidate else ''
            stamp = self._stamp(candidate) if candidate else None
            if identity in self._rejected and self._rejected[identity] != stamp:
                del self._rejected[identity]
            if candidate and identity not in self._rejected and stamp is not None:
                result = str(candidate)
        except (OSError, ValueError):
            pass
        if len(self._cache) >= 2048:
            self._cache.clear()
        self._cache[key] = (now + self.refresh_seconds, result)
        return result


def validate_proxy_media(ffprobe: str, source_path: str, proxy_path: str,
                         expected_duration: float, fps: float = 30.0, *, probe=None) -> MediaInfo:
    """Require a complete video proxy with the source's time scale.

    A different frame rate is allowed: editing and preview address seconds,
    never frame indices in the proxy. This cannot prove visual identity; the
    caller still needs to choose a proxy made from the same untrimmed source.
    ``probe`` optionally supplies an equivalent cancellable media reader.
    """
    source, proxy = Path(source_path).resolve(), Path(proxy_path).resolve()
    if source == proxy:
        raise ValueError('请选择由原片生成的代理文件，不能把原片本身挂为代理。')
    if not proxy.is_file():
        raise ValueError('代理文件不存在或不是普通文件。')
    if not ffprobe:
        raise ValueError('ffprobe 尚未就绪，暂时无法校验代理。')
    try:
        expected = float(expected_duration)
        if not math.isfinite(expected) or expected <= 0:
            raise ValueError('原片时长无效，无法绑定代理。')
        media = (probe or probe_media)(ffprobe, str(proxy))
    except ExportCancelled:
        raise
    except (RuntimeError, OSError) as exc:
        raise ValueError(f'无法读取代理视频：{exc}') from exc
    if media.width <= 0 or media.height <= 0 or not math.isfinite(media.duration) or media.duration <= 0:
        raise ValueError('代理必须包含具有有效时长的视频画面。')
    rates = [float(value) for value in (fps, media.fps)
             if isinstance(value, (int, float)) and math.isfinite(value) and value > 0]
    tolerance = max([0.12, *(2.0 / value for value in rates)])
    if abs(media.duration - expected) > tolerance:
        raise ValueError(f'代理时长 {media.duration:.3f} 秒与原片 {expected:.3f} 秒不一致。'
                         '请选择由同一完整原片生成、没有裁切或变速的代理。')
    return media
