"""Dependency-free middle-window synchronization using the original waveform FFT.

Only compact RMS bins are retained. The master is decoded once per batch and
fast mode never silently expands a camera probe to the complete recording.
"""
from __future__ import annotations

from array import array
import math
from pathlib import Path
import queue
import subprocess
import sys
import threading

from exporter_core import CREATE_NO_WINDOW, ExportCancelled


PROBE_SECONDS = 45.0
PCM_CHUNK_BYTES = 65536


def _stream_energy_python(ffmpeg, path, rate, *, start=0.0, duration=None,
                          cancel_event=None, on_decode=None):
    """Consume bounded PCM chunks without NumPy or a whole-track PCM buffer."""
    from audio_sync import _check_cancel, _stop_process

    _check_cancel(cancel_event)
    samples_per_bin = max(1, 2000 // rate)
    pcm_rate = rate * samples_per_bin
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    if start > 0:
        command += ["-ss", f"{start:.6f}"]
    command += ["-i", str(path)]
    if duration is not None:
        command += ["-t", f"{duration:.6f}"]
    command += ["-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1",
                "-ar", str(pcm_rate), "-f", "s16le", "pipe:1"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    chunks = queue.Queue(maxsize=4)
    stopping = threading.Event()
    errors = []
    reader_errors = []

    def put(item):
        while not stopping.is_set():
            try:
                chunks.put(item, timeout=0.1)
                return
            except queue.Full:
                pass

    def read_pcm():
        try:
            while not stopping.is_set():
                chunk = process.stdout.read(PCM_CHUNK_BYTES)
                if not chunk:
                    break
                put(chunk)
        except (OSError, ValueError) as exc:
            if not stopping.is_set():
                reader_errors.append(exc)
        finally:
            put(None)

    def read_errors():
        try:
            while not stopping.is_set():
                chunk = process.stderr.read(4096)
                if not chunk:
                    break
                errors.append(chunk[-2000:])
                del errors[:-20]
        except (OSError, ValueError):
            pass

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
            pcm = array("h")
            pcm.frombytes(raw[:usable])
            if sys.byteorder != "little":
                pcm.byteswap()
            energy = 0
            count = 0
            for value in pcm:
                energy += value * value
                count += 1
                if count == samples_per_bin:
                    bins.append(math.sqrt(energy / samples_per_bin))
                    energy = count = 0
            total_samples += len(pcm)
            if on_decode is not None:
                on_decode(total_samples / pcm_rate)
        while process.poll() is None:
            _check_cancel(cancel_event)
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        diagnostic_reader.join(timeout=1)
        _check_cancel(cancel_event)
        if process.returncode or reader_errors:
            detail = b"".join(errors).decode("utf-8", "replace")
            raise RuntimeError(f"无法读取音频：{Path(path).name}\n{detail[-1800:]}")
        return bins
    finally:
        stopping.set()
        _stop_process(process)
        reader.join(timeout=2)
        diagnostic_reader.join(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()


def _waveform_feature(energy, rate):
    from editor_core import _normalize_feature

    if len(energy) < rate * 5 or max(energy, default=0.0) < 1e-4:
        raise RuntimeError("音频静音或不足 5 秒，无法可靠自动对齐")
    feature = [math.log1p(current) - math.log1p(previous)
               for previous, current in zip(energy, energy[1:])]
    return _normalize_feature(feature)


def _match_window(probe, master, rate, start, maximum, cancel_event):
    from audio_sync import _check_cancel
    from editor_core import _normalized_fft_correlation_python

    _check_cancel(cancel_event)
    options = dict(min_overlap_seconds=max(5.0, 0.7 * min(len(probe), len(master)) / rate),
                   return_second_peak=True)
    if maximum > 0:
        # The limit constrains the whole camera offset, not the local probe lag.
        options.update(min_lag=math.ceil((-maximum - start) * rate),
                       max_lag=math.floor((maximum - start) * rate))
    lag, score, second = _normalized_fft_correlation_python(probe, master, rate, 0.0, **options)
    _check_cancel(cancel_event)
    score = min(1.0, max(0.0, float(score)))
    uniqueness = min(1.0, max(0.03, (score - max(0.0, float(second))) / 0.06))
    confidence = score * uniqueness
    if score < 0.30:
        confidence = min(confidence, 0.099)
    return start + lag / rate, confidence


def align_without_numpy(ffmpeg, videos, external_audio, *, envelope_rate=50,
                        max_offset_seconds=0, mode="fast", cancel_event=None,
                        progress=None, continue_on_error=False):
    """Align camera middles to one master waveform without importing NumPy.

    Positive offsets retain the existing convention (camera before master).
    Silent middles may try at most the two adjacent 45-second windows. An
    ambiguous audible probe requires manual review and never triggers full
    camera decoding; callers must explicitly select ``mode='full'`` for that.
    """
    from audio_sync import _Reporter, _check_cancel, _duration
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
    report(0, "内置兼容同步 · 读取主录音波形", force=True)
    master_duration = _duration(ffmpeg, external_audio, cancel_event)
    energy = _stream_energy_python(ffmpeg, external_audio, envelope_rate, cancel_event=cancel_event,
                                   on_decode=lambda seconds: report(
                                       34 * min(1.0, seconds / max(1.0, master_duration)), "内置兼容同步 · 分析主录音"))
    try:
        master = _waveform_feature(energy, envelope_rate)
    except RuntimeError as exc:
        raise RuntimeError(f"主录音无法可靠自动对齐：{exc}") from exc
    del energy
    report(35, "主录音波形就绪", force=True)
    results = []
    for index, video in enumerate(videos):
        _check_cancel(cancel_event)
        base, span = 35 + 65 * index / len(videos), 65 / len(videos)
        name = Path(video).name
        try:
            if Path(video).resolve() == Path(external_audio).resolve():
                result = AlignmentResult(0.0, 1.0, envelope_rate, "内置兼容 · 同一音轨")
            else:
                if not Path(video).is_file():
                    raise ValueError(f"音频同步文件不存在：{video}")
                if mode == "full":
                    starts, window = [0.0], None
                else:
                    duration = _duration(ffmpeg, video, cancel_event)
                    if not math.isfinite(duration) or duration <= 0:
                        raise RuntimeError("无法读取视频时长，请检查素材或显式选择完整分析")
                    window = min(PROBE_SECONDS, duration)
                    middle = round(max(0.0, (duration - window) / 2) * envelope_rate) / envelope_rate
                    maximum_start = max(0.0, duration - window)
                    starts = [middle]
                    for nearby in (max(0.0, middle - PROBE_SECONDS), min(maximum_start, middle + PROBE_SECONDS)):
                        nearby = round(nearby * envelope_rate) / envelope_rate
                        if nearby not in starts:
                            starts.append(nearby)
                probe = None
                start = 0.0
                for probe_index, start in enumerate(starts):
                    _check_cancel(cancel_event)
                    report(base + span * 0.15 * probe_index,
                           f"{name} · {'完整波形' if window is None else '中段波形' if not probe_index else '附近有声波形'}", force=True)
                    energy = _stream_energy_python(ffmpeg, video, envelope_rate, start=start,
                                                   duration=window, cancel_event=cancel_event)
                    try:
                        probe = _waveform_feature(energy, envelope_rate)
                    except RuntimeError:
                        continue
                    break  # Only silence permits another short camera read.
                if probe is None:
                    raise RuntimeError("中间及附近音频静音、过短或变化过小，请手动对齐")
                report(base + span * 0.55, f"{name} · 在完整主录音中匹配波形", force=True)
                offset, confidence = _match_window(probe, master, envelope_rate, start,
                                                    max_offset_seconds, cancel_event)
                engine = "内置兼容 · " + ("完整波形" if mode == "full" else "中段波形")
                if confidence < 0.1:
                    if not continue_on_error:
                        raise RuntimeError("波形匹配不唯一或相似度过低，请手动对齐")
                    engine += " · 匹配不唯一/待手动"
                result = AlignmentResult(float(offset), float(confidence), envelope_rate, engine)
        except ExportCancelled:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            if not continue_on_error:
                raise
            result = AlignmentResult(0.0, 0.0, envelope_rate, f"待手动：{str(exc)[:300]}")
        results.append(result)
        report(base + span, f"完成 {index + 1}/{len(videos)} · 偏移 {result.offset_seconds:+.3f} 秒 · 可信度 {result.confidence:.3f}", force=True)
    _check_cancel(cancel_event)
    report(100, "内置兼容同步完成", force=True)
    return results
