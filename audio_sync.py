"""Bounded-memory audio synchronization with cached energy and short probes.

PCM is consumed in small chunks and discarded immediately. Only 50 Hz RMS
energy (about 0.7 MiB/hour) is retained. Fast mode reads one 45-second window centered on each camera recording.
The original normalized waveform FFT searches the complete master recording.
Only silent or weak windows try adjacent short samples; fast mode never reads
the entire camera recording as an automatic fallback.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from typing import Callable, Sequence

from exporter_core import CREATE_NO_WINDOW, ExportCancelled
from cache_paths import cache_dir as app_cache_dir

# Energy-cache schema; waveform matching can reuse the existing RMS samples.
ALGORITHM_VERSION = "rms-stream-probes-v1"
MATCH_METHOD = "middle-waveform-legacy-fft-v2"
PROBE_SECONDS = 45.0
WAVEFORM_SCORE_THRESHOLD = 0.30
CACHE_LIMIT_BYTES = 512 * 1024 * 1024
PCM_CHUNK_BYTES = 65536


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ExportCancelled("音频同步已取消")


def _stop_process(process) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


class _Reporter:
    def __init__(self, callback):
        self.callback = callback
        self.value = 0.0
        self.phase = ""
        self.last_time = 0.0

    def __call__(self, value, phase, *, force=False):
        value = min(100.0, max(self.value, float(value)))
        now = time.perf_counter()
        if self.callback is not None and (force or phase != self.phase or now - self.last_time > 0.15):
            self.callback(value, phase)
            self.last_time = now
        self.value, self.phase = value, phase


def _duration(ffmpeg: str, path: str, cancel_event=None) -> float:
    suffix = ".exe" if os.name == "nt" else ""
    sibling = Path(ffmpeg).resolve().with_name("ffprobe" + suffix)
    ffprobe = str(sibling) if sibling.is_file() else shutil.which("ffprobe")
    if ffprobe:
        command = [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path]
    else:
        command = [ffmpeg, "-hide_banner", "-nostdin", "-i", path]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 30
        while True:
            _check_cancel(cancel_event)
            if time.monotonic() > deadline:
                raise RuntimeError("读取音频时长超时")
            try:
                out, err = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                pass
        if ffprobe:
            try:
                value = float(out.strip())
            except (ValueError, TypeError):
                value = 0.0
        else:
            match = re.search(rb"Duration:\s*(\d+):(\d+):([\d.]+)", err)
            value = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3]) if match else 0.0
        return value if math.isfinite(value) and value > 0 else 0.0
    finally:
        _stop_process(process)
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()


def _stream_energy(ffmpeg: str, path: str, rate: int, *, start=0.0,
                   duration=None, cancel_event=None, on_decode=None):
    """Decode with bounded pipes/queue; no whole-track PCM or float array."""
    import numpy as np

    _check_cancel(cancel_event)
    samples_per_bin = 80
    pcm_rate = rate * samples_per_bin
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    if start > 0:
        command += ["-ss", f"{start:.6f}"]
    command += ["-i", path]
    if duration is not None:
        command += ["-t", f"{duration:.6f}"]
    command += ["-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(pcm_rate),
                "-f", "s16le", "pipe:1"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    chunks = queue.Queue(maxsize=4)
    stopping = threading.Event()
    errors: list[bytes] = []
    reader_errors: list[Exception] = []

    def read_pcm():
        try:
            assert process.stdout is not None
            while not stopping.is_set():
                chunk = process.stdout.read(PCM_CHUNK_BYTES)
                if not chunk:
                    break
                while not stopping.is_set():
                    try:
                        chunks.put(chunk, timeout=0.1)
                        break
                    except queue.Full:
                        pass
        except (OSError, ValueError) as exc:
            if not stopping.is_set():
                reader_errors.append(exc)
        finally:
            while not stopping.is_set():
                try:
                    chunks.put(None, timeout=0.1)
                    break
                except queue.Full:
                    pass

    def read_errors():
        assert process.stderr is not None
        for line in iter(process.stderr.readline, b""):
            errors.append(line[-2000:])
            del errors[:-20]

    reader = threading.Thread(target=read_pcm, daemon=True)
    diagnostic_reader = threading.Thread(target=read_errors, daemon=True)
    reader.start()
    diagnostic_reader.start()
    bins = []
    pending = b""
    total_samples = 0
    try:
        while True:
            _check_cancel(cancel_event)
            try:
                chunk = chunks.get(timeout=0.1)
            except queue.Empty:
                continue
            if chunk is None:
                break
            raw = pending + chunk
            usable = len(raw) // (samples_per_bin * 2) * (samples_per_bin * 2)
            pending = raw[usable:]
            if usable:
                # This temporary float array is at most one 64 KiB PCM chunk.
                values = np.frombuffer(raw, dtype="<i2", count=usable // 2).astype(np.float32)
                blocks = values.reshape(-1, samples_per_bin)
                energy = np.sqrt(np.mean(blocks * blocks, axis=1))
                bins.append(energy.astype(np.float32, copy=False))
                total_samples += usable // 2
                if on_decode is not None:
                    on_decode(total_samples / pcm_rate)
        process.wait(timeout=10)
        diagnostic_reader.join(timeout=1)
        _check_cancel(cancel_event)
        if process.returncode or reader_errors:
            detail = b"".join(errors).decode("utf-8", "replace")
            raise RuntimeError(f"无法读取音频：{Path(path).name}\n{detail[-1800:]}")
        if not bins:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(bins)
    finally:
        stopping.set()
        _stop_process(process)
        reader.join(timeout=2)
        diagnostic_reader.join(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()


class _EnergyCache:
    def __init__(self, root, rate, ffmpeg, cancel_event):
        self.root = Path(root) if root is not None else app_cache_dir('audio_sync')
        self.rate, self.ffmpeg, self.cancel_event = rate, ffmpeg, cancel_event
        self.hits = 0
        self.decodes = 0
        self.enabled = True
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.enabled = False

    def energy(self, path, *, start=0.0, duration=None, on_decode=None):
        import numpy as np

        _check_cancel(self.cancel_event)
        source = Path(path).resolve()
        stat = source.stat()
        identity = dict(version=ALGORITHM_VERSION, path=os.path.normcase(str(source)),
                        size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                        rate=self.rate, start=round(start, 6),
                        duration=None if duration is None else round(duration, 6))
        key = json.dumps(identity, ensure_ascii=False, sort_keys=True)
        entry = self.root / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".npz")
        if self.enabled and entry.is_file():
            try:
                if entry.stat().st_size <= 128 * 1024 * 1024:
                    with np.load(entry, allow_pickle=False) as cached:
                        energy = cached["energy"]
                        valid = (str(cached["identity"].item()) == key and energy.ndim == 1
                                 and energy.dtype == np.float32 and energy.size <= 32_000_000
                                 and np.isfinite(energy).all() and (energy >= 0).all())
                    if valid:
                        self.hits += 1
                        _check_cancel(self.cancel_event)
                        return energy
            except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
                pass
        self.decodes += 1
        energy = _stream_energy(self.ffmpeg, str(source), self.rate, start=start,
                                duration=duration, cancel_event=self.cancel_event, on_decode=on_decode)
        _check_cancel(self.cancel_event)
        # Source mutation during decode must never be cached under old stats.
        after = source.stat()
        if self.enabled and (after.st_size, after.st_mtime_ns) == (stat.st_size, stat.st_mtime_ns):
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.root, prefix=".energy_", suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    np.savez(handle, identity=np.array(key), energy=energy)
                _check_cancel(self.cancel_event)
                os.replace(temporary, entry)
                self._prune()
            except OSError:
                pass  # A read-only/full cache must not block synchronization.
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return energy

    def _prune(self):
        try:
            entries = [(item.stat().st_mtime_ns, item.stat().st_size, item)
                       for item in self.root.glob("*.npz")]
            size = sum(item[1] for item in entries)
            for _time, count, item in sorted(entries):
                if size <= CACHE_LIMIT_BYTES:
                    break
                item.unlink(missing_ok=True)
                size -= count
        except OSError:
            pass


def _feature(energy, factor=1):
    import numpy as np

    if factor > 1:
        usable = len(energy) // factor * factor
        energy = energy[:usable].reshape(-1, factor).mean(axis=1)
    return np.diff(np.log1p(energy)).astype(np.float32, copy=False)


def _has_signal(energy, rate):
    import numpy as np

    if len(energy) < rate * 5 or float(np.max(energy, initial=0)) < 2.0:
        return False
    feature = _feature(energy)
    return float(np.std(feature)) > 0.0002


def _normalized_waveform(energy):
    """The same log-RMS change waveform and normalization as editor_core."""
    import numpy as np

    feature = _feature(energy).astype(np.float64)
    feature -= np.mean(feature)
    deviation = float(np.std(feature))
    if not math.isfinite(deviation) or deviation < 1e-6:
        raise RuntimeError("音频波形变化过小，请选择其他片段或手动对齐")
    return feature / deviation


def _match_waveform_window(start, energy, master_energy, rate, maximum, cancel_event):
    """Locate a middle sample, then recover the complete camera's delay.

    The old matcher treats its reference as starting at zero. A camera sample
    actually starts at ``start``: delay = sample_start + local_lag / rate.
    Bounds must therefore be shifted before selecting the correlation peak.
    """
    from editor_core import _normalized_fft_correlation

    _check_cancel(cancel_event)
    reference = _normalized_waveform(energy)
    master = _normalized_waveform(master_energy)
    minimum = max(5.0, 0.7 * min(len(reference), len(master)) / rate)
    low = (-maximum-start)*rate if maximum > 0 else None
    high = (maximum-start)*rate if maximum > 0 else None
    lag, score, second = _normalized_fft_correlation(
        reference, master, rate, 0.0, min_lag=low, max_lag=high,
        min_overlap_seconds=minimum, return_second_peak=True)
    _check_cancel(cancel_event)
    offset = start + lag/rate
    score = max(-1.0, min(1.0, float(score)))
    uniqueness = min(1.0, max(0.03, (score-second)/0.06))
    confidence = max(0.0, score) * uniqueness
    if score < WAVEFORM_SCORE_THRESHOLD:
        confidence = min(confidence, 0.099)
    return float(offset), float(confidence), score, float(second)


def _match_probes(probes, master_energy, rate, maximum, cancel_event):
    """Compatibility helper, using the original waveform matcher per window."""
    matches = [_match_waveform_window(start, energy, master_energy, rate, maximum, cancel_event)
               for start, energy in probes]
    if not matches:
        raise RuntimeError("没有可供对齐的音频波形")
    best = max(matches, key=lambda item: (item[1], item[2]))
    agreement = sum(abs(item[0]-best[0]) <= 2/rate for item in matches) / len(matches)
    return best[0], best[1]*agreement, best[2], agreement


def align_audio_segments_fast(
    ffmpeg: str,
    videos: Sequence[str],
    external_audio: str,
    *,
    envelope_rate: int = 50,
    max_offset_seconds: float = 0.0,
    mode: str = "fast",
    cache_dir: Path | str | None = None,
    cancel_event=None,
    progress: Callable[[float, str], None] | None = None,
    continue_on_error: bool = False,
):
    """Return existing AlignmentResult objects, with the original offset sign.

    Positive offset delays external audio (camera starts before master zero).
    A zero maximum searches the complete recording, including after two hours.
    Cancellation always raises ExportCancelled and never triggers a fallback.
    With continue_on_error, unreadable/silent camera files return zero-confidence
    manual-alignment placeholders; an invalid master remains a fatal error.
    """
    from editor_core import AlignmentResult

    if mode not in {"fast", "full"}:
        raise ValueError("音频同步模式必须是 fast 或 full")
    if isinstance(envelope_rate, bool) or not isinstance(envelope_rate, int) or not 5 <= envelope_rate <= 200:
        raise ValueError("包络采样率应是 5 到 200 的整数")
    if not isinstance(max_offset_seconds, (int, float)) or not math.isfinite(max_offset_seconds) or max_offset_seconds < 0:
        raise ValueError("最大偏移必须是非负有限数字")
    if not videos:
        raise ValueError("至少需要一个待对齐的视频")
    for path in [external_audio, *(videos if not continue_on_error else [])]:
        if not Path(path).is_file():
            raise ValueError(f"音频同步文件不存在：{path}")
    _check_cancel(cancel_event)
    report = _Reporter(progress)
    report(0, "准备中段波形同步", force=True)
    try:
        import numpy as np
    except ImportError:
        from audio_sync_fallback import align_without_numpy

        return align_without_numpy(
            ffmpeg, videos, external_audio, envelope_rate=envelope_rate,
            max_offset_seconds=max_offset_seconds, mode=mode, cancel_event=cancel_event,
            progress=progress, continue_on_error=continue_on_error)
    cache = _EnergyCache(cache_dir, envelope_rate, ffmpeg, cancel_event)
    started = time.perf_counter()
    master_duration = _duration(ffmpeg, external_audio, cancel_event)
    report(1, "读取主录音低频包络（首次分析会缓存）", force=True)
    master = cache.energy(external_audio, on_decode=lambda seconds: report(
        1 + 34 * min(1.0, seconds / max(1.0, master_duration)), "正在流式分析主录音"))
    if not _has_signal(master, envelope_rate):
        raise RuntimeError("主录音静音、过短或变化过小，无法可靠自动对齐")
    report(35, f"主录音就绪 · 缓存命中 {cache.hits} · {time.perf_counter() - started:.1f} 秒", force=True)
    results = []
    for index, video in enumerate(videos):
        _check_cancel(cancel_event)
        item_started = time.perf_counter()
        base, span = 35 + 65 * index / len(videos), 65 / len(videos)
        name = Path(video).name
        item_hits, item_decodes = cache.hits, cache.decodes
        report(base, f"分析 {index + 1}/{len(videos)}：{name}", force=True)
        try:
            if Path(video).resolve() == Path(external_audio).resolve():
                result = AlignmentResult(0.0, 1.0, envelope_rate, "同一音轨 · 缓存复用")
            else:
                if mode == "full":
                    report(base+span*0.1, f"{name} · 完整波形分析（手动选择）", force=True)
                    energy = cache.energy(video, on_decode=lambda seconds: report(
                        base+span*0.55, f"{name} · 流式读取完整音频"))
                    if not _has_signal(energy, envelope_rate):
                        raise RuntimeError(f"{name}：音轨静音、过短或变化过小，请手动对齐")
                    report(base+span*0.65, f"{name} · 原版归一化波形互相关", force=True)
                    offset, confidence, score, second = _match_waveform_window(
                        0.0, energy, master, envelope_rate, max_offset_seconds, cancel_event)
                    label = "完整波形"
                else:
                    duration = _duration(ffmpeg, video, cancel_event)
                    if duration < 5:
                        raise RuntimeError(f"{name}：无法读取有效时长或音频不足5秒，请检查素材")
                    window = min(PROBE_SECONDS, duration)
                    middle = round(max(0.0, (duration-window)/2)*envelope_rate)/envelope_rate
                    # The middle is always attempted first. Nearby bounded
                    # samples are only used if it is silent or unreliable.
                    starts = [middle]
                    starts += [round(max(0.0, middle-window)*envelope_rate)/envelope_rate,
                               round(min(duration-window, middle+window)*envelope_rate)/envelope_rate]
                    starts = list(dict.fromkeys(starts))
                    matches = []
                    failure = None
                    for index_window, sample_start in enumerate(starts):
                        _check_cancel(cancel_event)
                        phase = "中间波形" if index_window == 0 else "附近有声片段"
                        report(base+span*(0.1+0.2*index_window),
                               f"{name} · 截取{phase} {sample_start:.2f}秒起 / {window:.0f}秒", force=True)
                        energy = cache.energy(video, start=sample_start, duration=window,
                            on_decode=lambda seconds: report(base+span*0.25,
                                f"{name} · 读取{phase}（只读截取区间）"))
                        if not _has_signal(energy, envelope_rate):
                            failure = "中段及附近片段静音或波形变化太小"
                            continue
                        report(base+span*(0.3+0.2*index_window),
                               f"{name} · 按原版波形在主录音中搜索", force=True)
                        try:
                            match = _match_waveform_window(sample_start, energy, master,
                                envelope_rate, max_offset_seconds, cancel_event)
                        except ExportCancelled:
                            raise
                        except RuntimeError as exc:
                            failure = str(exc)
                            continue
                        matches.append((sample_start, match))
                        if match[1] >= 0.2 and match[2] >= WAVEFORM_SCORE_THRESHOLD:
                            break
                    if not matches:
                        raise RuntimeError(f"{name}：{failure or '中段波形未找到可靠重叠'}；可手动选择完整分析或精调")
                    sample_start, (offset, confidence, score, second) = max(
                        matches, key=lambda item: (item[1][1], item[1][2]))
                    label = f"中段波形 {sample_start:.2f}s+{window:.0f}s · 原版互相关"
                elapsed = time.perf_counter()-item_started
                engine = label + f" · 缓存 {cache.hits-item_hits}/{cache.hits-item_hits+cache.decodes-item_decodes} · {elapsed:.1f} 秒"
                if confidence < 0.2:
                    engine += " · 波形弱或重复，待检查"
                result = AlignmentResult(float(offset), float(confidence), envelope_rate, engine)
        except ExportCancelled:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            if not continue_on_error:
                raise
            result = AlignmentResult(0.0, 0.0, envelope_rate, f"待手动：{str(exc)[:300]}")
        results.append(result)
        report(base + span, f"完成 {index + 1}/{len(videos)} · 偏移 {result.offset_seconds:+.3f} 秒 · 可信度 {result.confidence:.3f} · {result.engine}", force=True)
    _check_cancel(cancel_event)
    report(100, f"同步完成 · 缓存命中 {cache.hits} · 解码 {cache.decodes} 次 · 总耗时 {time.perf_counter() - started:.1f} 秒", force=True)
    return results
