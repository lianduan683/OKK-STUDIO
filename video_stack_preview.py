"""Bounded, cancellable live RGBA preview using the export compositor graph.

Two bounded FFmpeg slots stream the current segment and prepare its successor. No timeline-length
cache is generated. The caller owns audio/time; decoded video follows that
clock and a single latest-frame mailbox, never a queue of image buffers.
"""
from __future__ import annotations

from dataclasses import replace
from contextlib import ExitStack, contextmanager
from fractions import Fraction
import json
import math
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from exporter_core import CREATE_NO_WINDOW, _media_probe_command, _media_info_from_payload
from multicam_frame_clock import canonical_frame_rate
from video_stack_render import build_stack_command, plan_stack_segments, validate_stack_media
from proxy_media import PreviewProxyResolver, validate_proxy_media


class PreviewCancelled(Exception):
    pass


class PreviewCatchUp(Exception):
    """Replace only a stale continuous decoder, keeping its preview owner."""
    def __init__(self, position):
        super().__init__('continuous preview fell behind the master clock')
        self.position = position


def preview_segments(project, position, *, single_frame=False):
    """Trim on the original project frame clock, including pauses at 60 fps."""
    project = replace(project, fps=float(canonical_frame_rate(project.fps)), range_start=0., range_end=None)
    fps = float(project.fps)
    frame = min(max(0, math.floor(position*fps+.5)), max(0, math.ceil(project.duration*fps)-1))
    for segment in plan_stack_segments(project):
        left = round(segment.start*fps); right = left+segment.frames
        if frame >= right: continue
        first = max(frame, left); count = 1 if single_frame else right-first
        delta = (first-left)/fps
        yield replace(segment, start=first/fps, duration=count/fps, frames=count,
                      layers=tuple(replace(layer, source_start=layer.source_start+delta) for layer in segment.layers))
        if single_frame: return
        frame = right


def preview_command(segment, project, ffmpeg, media, width=1280, height=720,
                    *, single_frame=False, max_fps=30., ass_path=None, source_frame_rates=None):
    """Compose at project fps, then sample playback; pause keeps its exact frame."""
    rate = canonical_frame_rate(project.fps)
    project = replace(project, fps=float(rate))
    # A nominal MediaInfo.fps alone cannot prove a CFR source. Only use the
    # exact avg/r/time-base gate collected by the cancellable metadata probe.
    source_rates = source_frame_rates or {}
    command = build_stack_command(segment, project,
        [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin'], width, height,
        'rgba', str(rate), ['-c:v', 'rawvideo', '-threads:v', '1', '-f', 'rawvideo'],
        'pipe:1', ass_path, source_frame_rates=source_rates, media_by_path=media)
    output_fps = float(rate) if single_frame else min(float(rate), float(max_fps))
    if output_fps < float(rate):
        # Input trim/prefix stays on the project clock. Only the fully composed
        # final picture is resampled, so a 60 fps paused seek does not jump.
        graph_index = command.index('-filter_complex')+1
        command[graph_index] = command[graph_index].replace('[picture]', '[project_picture]') + \
            f';[project_picture]fps=fps={output_fps:.12g}:start_time=0[picture]'
        command[command.index('-r')+1] = f'{output_fps:.12g}'
        command[command.index('-frames:v')+1] = str(max(1, math.ceil(segment.duration*output_fps-1e-8)))
    timescale = command.index('-video_track_timescale')
    del command[timescale:timescale+2]
    return command, output_fps


class StackPreview:
    """Own current/next FFmpeg processes and a single latest RGBA mailbox."""
    CATCHUP_LAG = .5
    CATCHUP_INTERVAL = 2.5
    def __init__(self, ffmpeg, ffprobe, notify, *, width=1280, height=720, max_fps=30.):
        self.ffmpeg, self.ffprobe, self.notify = str(ffmpeg), str(ffprobe), notify
        self.width, self.height, self.max_fps = width, height, max_fps
        self._condition = threading.Condition()
        self._pending = None
        self._cancel = threading.Event()
        self._closed = False
        self._process = None
        self._processes = set()
        self._latest = None
        self._notified = False
        self._generation = 0
        self._epoch = None
        self._clock = 0.
        self._playing = False
        self._stream_generation = None
        self._decoded_time = None
        self._decoded_fps = self.max_fps
        self._last_catchup = time.monotonic()
        self._catchup_count = 0
        self._startup_seconds = .1
        self._media = {}
        self._rates = {}
        self._proxies = PreviewProxyResolver()
        self._shutdown_error = None
        self._thread = threading.Thread(target=self._run, name='stack-preview-ffmpeg', daemon=False)
        self._thread.start()

    def update_clock(self, position):
        with self._condition: self._clock = float(position)

    def set_playing(self, playing):
        """Pause pipe consumption; keep decoder state and its bounded cache."""
        with self._condition:
            self._playing = bool(playing)
            self._condition.notify_all()
            return self._stream_generation == self._generation

    def refresh_presentation(self, position, playing):
        """Discard a hidden-window mailbox; report a lagging decode stream.

        A healthy or paused stream keeps both processes and their pipe cache.
        FFmpeg rawvideo has no interactive seek command, so the caller may
        restart only a substantially late stream at the current master time.
        Metadata/proxy caches remain owned by this preview in either case.
        """
        with self._condition:
            self._clock = float(position)
            self._playing = bool(playing)
            self._condition.notify_all()
            if not playing: return False
            self._latest = None
            self._notified = False
            threshold = max(.2, 3./max(1., self._decoded_fps))
            return (self._stream_generation == self._generation
                    and self._decoded_time is not None
                    and position-self._decoded_time > threshold)

    def request(self, project, position, playing, request_id, *, ass_path=None, use_proxies=True, preview_epoch=None):
        with self._condition:
            if self._closed: raise RuntimeError('图层预览已经关闭。')
            if preview_epoch is None or preview_epoch != self._epoch:
                self._cancel.set()
                self._terminate_locked()
                self._generation += 1
                self._latest = None
            self._epoch = preview_epoch
            self._clock = float(position)
            self._playing = bool(playing)
            self._stream_generation = self._generation if playing else None
            self._decoded_time = None
            self._last_catchup = time.monotonic()
            self._pending = (self._generation, project, float(position), bool(playing), int(request_id),
                             ass_path, bool(use_proxies), preview_epoch)
            self._condition.notify()

    def cancel(self):
        with self._condition:
            self._generation += 1; self._pending = None; self._latest = None
            self._epoch = None
            self._stream_generation = None
            self._decoded_time = None
            self._cancel.set(); self._terminate_locked()

    def _terminate_locked(self):
        for process in tuple(self._processes):
            if process.poll() is None:
                try: process.terminate()
                except OSError: pass  # The owning worker always waits/reaps below.

    def take_frame(self):
        with self._condition:
            result = self._latest; self._latest = None; self._notified = False
            if result is not None and result.get('playback_frame'):
                clock = self._clock
                # Tk may have been busy after the worker published this
                # mailbox. Recheck the decoder's latest master clock when
                # taking the frame, not an older UI state-event timestamp.
                if self._playing and result['time'] < clock-1.5/result['fps']:
                    return None
                result = {**result, 'master_time': clock, 'playing': self._playing}
            return result

    def close(self, timeout=5.):
        with self._condition:
            self._closed = True; self._pending = None; self._latest = None
            self._cancel.set(); self._terminate_locked(); self._condition.notify()
        self._thread.join(timeout)
        if self._thread.is_alive(): raise RuntimeError('图层预览进程尚未退出，请重试释放缓存。')
        if self._processes:
            errors = []
            for process in tuple(self._processes):
                try: self._reap(process)
                except Exception as error: errors.append(error)
            if errors: raise errors[0]
            self._shutdown_error = None
        if self._shutdown_error: raise RuntimeError(self._shutdown_error)

    def _check(self, cancel):
        if cancel.is_set(): raise PreviewCancelled()

    def _wait_playing(self, cancel):
        with self._condition:
            while not self._playing:
                self._check(cancel)
                self._condition.wait(.05)
            self._check(cancel)

    def _catchup_target(self, generation, stamp, fps, duration):
        with self._condition:
            if (not self._playing or generation != self._generation
                    or self._pending is not None or self._cancel.is_set()): return None
            clock = self._clock
            if clock-stamp <= max(self.CATCHUP_LAG, 3./fps): return None
            now = time.monotonic()
            if now-self._last_catchup < self.CATCHUP_INTERVAL: return None
            last_frame = max(0., duration-1./fps)
            if clock >= last_frame: return None
            self._last_catchup = now
            # Prepare just ahead by the measured cold-start cost. Seeking
            # only to "now" can already be late when FFmpeg's first frame
            # arrives, particularly when composition is close to real time.
            lead = min(.5, max(1./fps, self._startup_seconds+1./fps))
            return min(last_frame, clock+lead)

    def _spawn(self, command, cancel, **kwargs):
        self._check(cancel)
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, **kwargs)
        with self._condition:
            self._processes.add(process)
            self._process = process
            if cancel.is_set(): self._terminate_locked()
        return process

    def _reap(self, process):
        try:
            if process.poll() is None: process.terminate()
            try: process.wait(timeout=2.)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=2.)
            if process.stdout: process.stdout.close()
            if process.stderr: process.stderr.close()
        except Exception as error:
            self._shutdown_error = f'图层预览进程未释放：{error}'
            raise
        else:
            with self._condition:
                self._processes.discard(process)
                if self._process is process: self._process = next(iter(self._processes), None)

    @contextmanager
    def _segment_stream(self, segment, project, media, width, height, playing, ass_path, rates, cancel):
        command, fps = preview_command(segment, project, self.ffmpeg, media,
            width, height, single_frame=not playing, max_fps=self.max_fps, ass_path=ass_path,
            source_frame_rates=rates)
        with tempfile.TemporaryFile() as error_log, tempfile.TemporaryDirectory(prefix='okk_stack_graph_') as graph_dir:
            # Script files also keep 36-input filter graphs below Windows'
            # command-line limit. Their owner stays alive until this process exits.
            graph_index = command.index('-filter_complex')
            graph_path = Path(graph_dir)/'preview.ffgraph'
            graph_path.write_text(command[graph_index+1], encoding='utf-8')
            command[graph_index:graph_index+2] = ['-/filter_complex', str(graph_path)]
            process = self._spawn(command, cancel, stdout=subprocess.PIPE, stderr=error_log)
            try: yield process, fps, error_log
            finally: self._reap(process)

    def _probe(self, path, cancel):
        stat = Path(path).stat(); key = (path, stat.st_size, stat.st_mtime_ns)
        if key in self._media: return self._media[key]
        command = _media_probe_command(self.ffprobe, path)
        entries = command.index('-show_entries')+1
        command[entries] = command[entries].replace('avg_frame_rate,r_frame_rate,', 'avg_frame_rate,r_frame_rate,time_base,')
        process = self._spawn(command, cancel,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic()+30
            while True:
                self._check(cancel)
                try: output, error = process.communicate(timeout=.05); break
                except subprocess.TimeoutExpired:
                    if time.monotonic()>deadline: raise RuntimeError('读取图层素材信息超时。')
            self._check(cancel)
            if process.returncode: raise RuntimeError(error.decode('utf-8', errors='replace'))
            payload = json.loads(output.decode('utf-8'))
            info = _media_info_from_payload(path, payload)
            self._media[key] = info
            try:
                stream = next(item for item in payload['streams'] if item.get('codec_type') == 'video'
                              and not (item.get('disposition') or {}).get('attached_pic'))
                average, nominal, base = (Fraction(str(stream[name])) for name in
                    ('avg_frame_rate', 'r_frame_rate', 'time_base'))
                ticks = 1 / average / base
                if average > 0 and average == nominal and base > 0 and ticks.denominator == 1 and ticks >= 1:
                    self._rates[key] = average
            except (KeyError, StopIteration, ValueError, ZeroDivisionError, TypeError): pass
            return info
        finally: self._reap(process)

    def _publish(self, generation, payload):
        with self._condition:
            if generation != self._generation or self._closed: return
            self._latest = payload
            if self._notified: return
            self._notified = True
        self.notify('stack_frame', None)

    def _render(self, job, cancel):
        generation, project, position, playing, request_id, ass_path, use_proxies, preview_epoch = job
        project = replace(project, fps=float(canonical_frame_rate(project.fps)), range_start=0., range_end=None)
        segments = tuple(preview_segments(project, position, single_frame=not playing))
        media = {}
        if project.clips:
            reference_path = project.clips[0].path
            media[reference_path] = self._probe(reference_path, cancel)
        for segment in segments:
            for layer in segment.layers:
                for path in (layer.original_path, layer.path):
                    if path not in media: media[path] = self._probe(path, cancel)
        validate_stack_media(segments, media)
        if use_proxies:
            candidates = {clip.path: clip.proxy_path for clip in project.clips}
            replacements = {}
            for segment in segments:
                for layer in segment.layers:
                    original = media[layer.original_path]
                    if layer.path != layer.original_path or layer.has_alpha or original.has_alpha:
                        continue
                    proxy = self._proxies(layer.original_path, candidates.get(layer.original_path, ''))
                    if proxy == layer.original_path or layer.original_path in replacements: continue
                    try:
                        info = validate_proxy_media(self.ffprobe, layer.original_path, proxy,
                            original.duration, project.fps, probe=lambda _tool,path:self._probe(path,cancel))
                        # Proxies must preserve framing as well as time scale.
                        if abs(info.width/info.height-original.width/original.height) > 2/min(info.height,original.height):
                            raise ValueError('代理画幅与原片不一致。')
                    except PreviewCancelled: raise
                    except (ValueError, OSError, RuntimeError):
                        self._proxies.reject(proxy)
                        continue
                    media[proxy] = info
                    replacements[layer.original_path] = proxy
            segments = tuple(replace(segment, layers=tuple(replace(layer,path=replacements[layer.original_path])
                if layer.path == layer.original_path and layer.original_path in replacements else layer
                for layer in segment.layers)) for segment in segments)
        width, height = self.width, self.height
        if project.clips:
            reference = media[project.clips[0].path]
            ratio = min(width / reference.width, height / reference.height)
            width = max(2, round(reference.width * ratio / 2) * 2)
            height = max(2, round(reference.height * ratio / 2) * 2)
        rates = {}
        for path in media:
            stat = Path(path).stat()
            rate = self._rates.get((path, stat.st_size, stat.st_mtime_ns))
            if rate is not None: rates[path] = rate
        # Alpha identity comes from an actual probe, including old projects.
        # We intentionally use original/full-quality treatment paths, never an
        # opaque H264 preview proxy for a formerly transparent layer.
        last_status = time.monotonic()
        iterator = iter(segments)
        current = following = None

        def prepare(segment):
            owner = ExitStack()
            try:
                process, fps, error_log = owner.enter_context(self._segment_stream(
                    segment, project, media, width, height, playing, ass_path, rates, cancel))
                return segment, process, fps, error_log, owner
            except BaseException:
                owner.close()
                raise

        try:
            segment = next(iterator, None)
            opened_at = time.monotonic()
            first_decoded = False
            if segment is not None: current = prepare(segment)
            while current is not None:
                self._check(cancel)
                segment, process, fps, error_log, owner = current
                # Start only the immediate successor. Its bounded stdout pipe
                # fills with the first frame, then backpressure stops decoding.
                # Nothing reads or queues the rest of its timeline in memory.
                next_segment = next(iterator, None) if playing else None
                if next_segment is not None: following = prepare(next_segment)
                frame_bytes = width*height*4; index = 0
                while True:
                    self._check(cancel)
                    if playing: self._wait_playing(cancel)
                    parts = bytearray()
                    while len(parts) < frame_bytes:
                        data = process.stdout.read(frame_bytes-len(parts))
                        if not data: break
                        parts.extend(data)
                    self._check(cancel)
                    if not parts: break
                    if len(parts) != frame_bytes: raise RuntimeError('图层预览返回了不完整的视频帧。')
                    stamp = segment.start + index/fps; index += 1
                    with self._condition:
                        if generation == self._generation:
                            self._decoded_time, self._decoded_fps = stamp, fps
                            if not first_decoded:
                                self._startup_seconds = min(.5, max(0., time.monotonic()-opened_at))
                    first_decoded = True
                    if playing:
                        # Audio remains the master. Never enqueue old frames
                        # if decoding or the Tk paint loop falls behind.
                        while True:
                            self._wait_playing(cancel)
                            with self._condition: clock = self._clock
                            if stamp <= clock + .5/fps: break
                            if cancel.wait(.004): raise PreviewCancelled()
                        if stamp < clock - 1.5/fps:
                            target = self._catchup_target(generation, stamp, fps, project.duration)
                            if target is not None: raise PreviewCatchUp(target)
                            if time.monotonic()-last_status > 3.:
                                self.notify('status', '图层预览解码跟不上播放时钟，正在跳过旧帧；可减少图层或使用保留透明通道的轻量素材。')
                                last_status = time.monotonic()
                            continue
                    self._publish(generation, {'request_id': request_id, 'time': stamp,
                        'width': width, 'height': height, 'rgba': bytes(parts), 'preview_epoch': preview_epoch,
                        'fps': fps, 'playback_frame': playing})
                process.wait(timeout=3.)
                if process.returncode:
                    error_log.seek(0); raise RuntimeError(error_log.read(8192).decode('utf-8', errors='replace'))
                owner.close()
                current, following = following, None
        finally:
            # Cancellation, navigation, cache unloading and failed successor
            # startup must reap both slots, even if one cleanup raises.
            try:
                if current is not None: current[4].close()
            finally:
                if following is not None: following[4].close()

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._closed: self._condition.wait()
                if self._closed: return
                job = self._pending; self._pending = None
                cancel = self._cancel = threading.Event()
            try: self._render(job, cancel)
            except PreviewCancelled: pass
            except PreviewCatchUp as recovery:
                with self._condition:
                    if (not self._closed and not cancel.is_set() and self._playing
                            and job[0] == self._generation and self._pending is None):
                        self._latest = None
                        self._notified = False
                        self._decoded_time = None
                        self._catchup_count += 1
                        self._pending = (job[0], job[1], recovery.position, True, *job[4:])
                        recovered = True
                    else: recovered = False
                if recovered:
                    self.notify('status', '图层实时合成持续落后，已跳过旧画面追上播放时钟；缓存保持有界，复杂图层可能无法满帧播放。')
            except Exception as error:
                if not cancel.is_set(): self.notify('error', f'透明图层预览失败：{error}')
                if self._shutdown_error:
                    with self._condition: self._closed = True
                    return  # Keep the failed process owner for explicit close retries.
            finally:
                with self._condition:
                    if self._stream_generation == job[0] and self._pending is None:
                        self._stream_generation = None
