from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from gpu_profiles import GpuProfile, classify_gpu


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass(slots=True)
class MediaInfo:
    path: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = "unknown"
    pixel_format: str = "unknown"
    bit_depth: int = 8
    audio_codec: str = "none"
    has_audio: bool = False
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_space: str | None = None
    timecode: str | None = None
    sample_aspect_ratio: str = "1:1"
    color_range: str | None = None


@dataclass(slots=True)
class Capabilities:
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    gpu_name: str = "NVIDIA GPU not detected"
    driver_version: str = "unknown"
    gpu_memory_mb: int = 0
    gpu_profile: GpuProfile = field(default_factory=lambda: classify_gpu(""))
    nvenc_codecs: set[str] = field(default_factory=set)
    cuda_decode: bool = False
    scale_cuda: bool = False
    split_encode: set[str] = field(default_factory=set)
    raw_encoder_help: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.gpu_profile.raw_name and self.gpu_name != "NVIDIA GPU not detected":
            self.gpu_profile = classify_gpu(self.gpu_name)


@dataclass(slots=True)
class ExportSettings:
    codec: str = "hevc_nvenc"
    quality: int = 20
    preset: str = "p2"
    tune: str = "hq"
    multipass: str = "disabled"
    split_encode: str = "auto"
    resolution: str = "original"
    bit_depth: str = "auto"
    audio: str = "aac"
    audio_bitrate: str = "320k"
    decode_mode: str = "cuda"
    gpu_index: int = 0
    gop: int = 120
    bframes: int = 2
    video_bitrate: str = ""
    maxrate: str = ""
    extra_args: list[str] = field(default_factory=list)
    audio_sample_rate: int | None = 48000
    parallel_workers: int = 1
    auto_parallel: bool = False


class ExportCancelled(RuntimeError):
    pass


def _run_text(args: Sequence[str], timeout: float = 15) -> tuple[int, str]:
    try:
        result = subprocess.run(
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return result.returncode, result.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def find_binary(name: str, app_dir: str | Path | None = None) -> str | None:
    suffix = ".exe" if os.name == "nt" else ""
    candidates: list[Path] = []
    if app_dir:
        root = Path(app_dir)
        candidates.extend((root / "bin" / f"{name}{suffix}", root / f"{name}{suffix}"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return shutil.which(name)


def _parse_rate(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            return float(numerator) / float(denominator)
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _infer_depth(stream: dict) -> int:
    raw = stream.get("bits_per_raw_sample")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    pixel_format = str(stream.get("pix_fmt", ""))
    match = re.search(r"(?:p|le|be)(10|12|16)(?:le|be)?$", pixel_format)
    if match:
        return int(match.group(1))
    return 8


def probe_media(ffprobe: str, path: str) -> MediaInfo:
    args = [
        ffprobe,
        "-v", "error",
        "-show_entries",
        "format=duration:format_tags=timecode:"
        "stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,pix_fmt,"
        "bits_per_raw_sample,color_primaries,color_transfer,color_space,color_range,"
        "sample_aspect_ratio,duration:stream_tags=timecode",
        "-of", "json",
        path,
    ]
    code, output = _run_text(args, timeout=30)
    if code:
        raise RuntimeError(f"ffprobe could not read the input:\n{output.strip()}")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned invalid JSON") from exc

    streams = payload.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("No video stream was found in this file")
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    format_info = payload.get("format", {})
    # Camera audio can extend beyond the last video frame. Timeline cuts and
    # camera-off gaps must be based on the picture duration, not that audio tail.
    try:
        duration = float(video.get("duration") or format_info.get("duration") or 0)
    except (TypeError, ValueError):
        duration = float(format_info.get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        duration = float(format_info.get("duration") or 0)
    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    video_tags = video.get("tags") or {}
    format_tags = format_info.get("tags") or {}
    timecode = video_tags.get("timecode") or format_tags.get("timecode")
    if not timecode:
        timecode = next(
            (
                (stream.get("tags") or {}).get("timecode")
                for stream in streams
                if (stream.get("tags") or {}).get("timecode")
            ),
            None,
        )
    return MediaInfo(
        path=path,
        duration=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=fps,
        video_codec=str(video.get("codec_name") or "unknown"),
        pixel_format=str(video.get("pix_fmt") or "unknown"),
        bit_depth=_infer_depth(video),
        audio_codec=str(audio.get("codec_name") if audio else "none"),
        has_audio=audio is not None,
        color_primaries=video.get("color_primaries"),
        color_transfer=video.get("color_transfer"),
        color_space=video.get("color_space"),
        timecode=str(timecode) if timecode else None,
        sample_aspect_ratio=str(video.get("sample_aspect_ratio") or "1:1"),
        color_range=video.get("color_range"),
    )


def detect_capabilities(app_dir: str | Path | None = None) -> Capabilities:
    ffmpeg = find_binary("ffmpeg", app_dir)
    ffprobe = find_binary("ffprobe", app_dir)
    caps = Capabilities(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)

    smi = find_binary("nvidia-smi", app_dir)
    if smi:
        code, output = _run_text([
            smi,
            "--query-gpu=name,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ])
        if code == 0 and output.strip():
            first = output.strip().splitlines()[0]
            parts = [part.strip() for part in first.split(",", 2)]
            caps.gpu_name = parts[0]
            if len(parts) > 1:
                caps.driver_version = parts[1]
            if len(parts) > 2:
                try:
                    caps.gpu_memory_mb = int(float(parts[2]))
                except ValueError:
                    caps.gpu_memory_mb = 0

    caps.gpu_profile = classify_gpu(caps.gpu_name)

    if not ffmpeg:
        return caps
    _, encoders = _run_text([ffmpeg, "-hide_banner", "-encoders"])
    for codec in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
        if re.search(rf"\b{re.escape(codec)}\b", encoders):
            # FFmpeg lists every encoder compiled into the executable, not the
            # codecs implemented by the installed GPU. Known RTX 20/30 models
            # have no AV1 encoder, so do not expose an option that must fail.
            if (
                codec == "av1_nvenc"
                and caps.gpu_profile.in_supported_range
                and not caps.gpu_profile.hardware_av1
            ):
                continue
            caps.nvenc_codecs.add(codec)
            _, help_text = _run_text([ffmpeg, "-hide_banner", "-h", f"encoder={codec}"])
            caps.raw_encoder_help[codec] = help_text
            if "split_encode_mode" in help_text:
                caps.split_encode.add(codec)
    _, hwaccels = _run_text([ffmpeg, "-hide_banner", "-hwaccels"])
    caps.cuda_decode = bool(re.search(r"(?m)^cuda\s*$", hwaccels))
    _, filters = _run_text([ffmpeg, "-hide_banner", "-filters"])
    caps.scale_cuda = "scale_cuda" in filters
    caps.gpu_profile = classify_gpu(caps.gpu_name)
    return caps


def _resolution_height(value: str) -> int | None:
    lookup = {"1080p": 1080, "1440p": 1440, "4k": 2160, "8k": 4320}
    return lookup.get(value.lower())


def _target_depth(settings: ExportSettings, media: MediaInfo) -> int:
    if settings.bit_depth == "10-bit":
        return 10
    if settings.bit_depth == "8-bit":
        return 8
    if settings.codec == "h264_nvenc":
        return 8
    return 10 if media.bit_depth > 8 else 8


def build_video_filter(settings: ExportSettings, media: MediaInfo, caps: Capabilities) -> list[str]:
    height = _resolution_height(settings.resolution)
    depth = _target_depth(settings, media)
    gpu_format = "p010le" if depth == 10 else "nv12"
    resize_needed = height is not None and height != media.height
    format_needed = depth != media.bit_depth

    if settings.decode_mode == "cuda":
        # The common 8K 10-bit HEVC -> 8K 10-bit HEVC path can pass decoded
        # CUDA frames directly into NVENC. Avoiding a no-op scale/filter saves
        # a full-frame allocation and copy for every 8K frame.
        if not resize_needed and not format_needed:
            return []
        if caps.scale_cuda:
            dimensions = f"-2:{height}:" if resize_needed else ""
            return ["-vf", f"scale_cuda={dimensions}format={gpu_format}:passthrough=0"]
        cpu_format = "p010le" if depth == 10 else "nv12"
        filters = ["hwdownload", f"format={cpu_format}"]
        if resize_needed:
            filters += [f"scale=-2:{height}", f"format={cpu_format}"]
        filters.append("hwupload_cuda")
        return ["-vf", ",".join(filters)]

    filters: list[str] = []
    if resize_needed:
        filters.append(f"scale=-2:{height}")
    filters.append(f"format={gpu_format}")
    return ["-vf", ",".join(filters)]


def _supported_option(help_text: str, option: str) -> bool:
    return re.search(rf"(?m)^\s*-{re.escape(option)}\s", help_text) is not None


def build_video_args(
    settings: ExportSettings,
    media: MediaInfo,
    caps: Capabilities,
) -> list[str]:
    help_text = caps.raw_encoder_help.get(settings.codec, "")
    throughput_mode = settings.preset in {"p1", "p2"}
    args = ["-c:v", settings.codec, "-preset", settings.preset]
    if _supported_option(help_text, "tune") or not help_text:
        args += ["-tune", settings.tune]
    if settings.video_bitrate.strip():
        args += ["-rc", "vbr", "-b:v", settings.video_bitrate.strip()]
    else:
        args += ["-rc", "vbr", "-cq", str(settings.quality), "-b:v", "0"]
    if settings.maxrate.strip():
        args += ["-maxrate", settings.maxrate.strip(), "-bufsize", settings.maxrate.strip()]
    if _supported_option(help_text, "multipass") or not help_text:
        args += ["-multipass", settings.multipass]
    # AQ and B-frames improve compression efficiency but reduce peak fps.
    # P1/P2 are explicitly throughput-first modes for time-critical delivery.
    if not throughput_mode:
        if _supported_option(help_text, "spatial_aq") or not help_text:
            args += ["-spatial_aq", "1", "-aq-strength", "8"]
        if _supported_option(help_text, "temporal_aq") or not help_text:
            args += ["-temporal_aq", "1"]
    bframes = 0 if throughput_mode else settings.bframes
    args += ["-g", str(settings.gop), "-bf", str(bframes)]

    depth = _target_depth(settings, media)
    if settings.codec in {"hevc_nvenc", "av1_nvenc"} and depth == 10:
        args += ["-profile:v", "main10"]
    split_encode = settings.split_encode
    if split_encode.isdigit() and caps.gpu_profile.in_supported_range:
        requested = int(split_encode)
        if caps.gpu_profile.nvenc_engines <= 1:
            split_encode = "disabled"
        else:
            split_encode = str(min(requested, caps.gpu_profile.nvenc_engines))
    if (
        settings.codec in caps.split_encode
        and settings.codec in {"hevc_nvenc", "av1_nvenc"}
        and split_encode != "auto"
    ):
        args += ["-split_encode_mode", split_encode]
    args += ["-fps_mode", "passthrough"]
    args += list(settings.extra_args)
    return args


def build_encode_command(
    ffmpeg: str,
    input_path: str,
    output_path: str,
    settings: ExportSettings,
    media: MediaInfo,
    caps: Capabilities,
    *,
    start: float | None = None,
    duration: float | None = None,
    video_only: bool = False,
) -> list[str]:
    command = [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "warning",
        "-stats_period", "0.25", "-progress", "pipe:1", "-nostats",
    ]
    if start is not None:
        command += ["-ss", f"{start:.6f}"]
    if settings.decode_mode == "cuda":
        command += [
            "-hwaccel", "cuda",
            "-hwaccel_device", str(settings.gpu_index),
            "-hwaccel_output_format", "cuda",
        ]
    command += ["-i", input_path]
    if duration is not None:
        command += ["-t", f"{duration:.6f}"]
    command += ["-map", "0:v:0"]
    if not video_only:
        command += ["-map", "0:a?"]
    command += build_video_filter(settings, media, caps)
    command += build_video_args(settings, media, caps)

    if video_only:
        command += ["-an"]
    elif settings.audio == "copy":
        command += ["-c:a", "copy"]
    elif settings.audio == "none":
        command += ["-an"]
    else:
        command += ["-c:a", "aac", "-b:a", settings.audio_bitrate]

    suffix = Path(output_path).suffix.lower()
    if suffix in {".mp4", ".mov", ".m4v"}:
        command += ["-movflags", "+faststart"]
    command += ["-map_metadata", "0", "-max_muxing_queue_size", "4096", output_path]
    return command


def build_mux_command(
    ffmpeg: str,
    encoded_video: str,
    original_input: str,
    output_path: str,
    settings: ExportSettings,
) -> list[str]:
    command = [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "warning",
        "-stats_period", "0.25", "-progress", "pipe:1", "-nostats",
        "-i", encoded_video,
        "-i", original_input,
        "-map", "0:v:0",
        "-map", "1:a?",
        "-map_metadata", "1",
        "-c:v", "copy",
    ]
    if settings.audio == "copy":
        command += ["-c:a", "copy"]
    elif settings.audio == "none":
        command += ["-an"]
    else:
        command += ["-c:a", "aac", "-b:a", settings.audio_bitrate]
    if Path(output_path).suffix.lower() in {".mp4", ".mov", ".m4v"}:
        command += ["-movflags", "+faststart"]
    command += ["-shortest", output_path]
    return command


def quote_command(command: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(command))


def parse_progress_line(line: str) -> tuple[str, str] | None:
    if "=" not in line:
        return None
    key, value = line.rstrip("\r\n").split("=", 1)
    if re.fullmatch(r"[a-zA-Z0-9_]+", key):
        return key, value
    return None


def progress_seconds(progress: dict[str, str]) -> float:
    for key, divisor in (("out_time_us", 1_000_000), ("out_time_ms", 1_000_000)):
        raw = progress.get(key)
        if raw:
            try:
                return int(raw) / divisor
            except ValueError:
                pass
    raw_time = progress.get("out_time")
    if raw_time:
        try:
            hours, minutes, seconds = raw_time.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except (ValueError, TypeError):
            pass
    return 0.0


def parse_speed_multiplier(value: str | None) -> float:
    if not value:
        return 0.0
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)x", value)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


def parse_fps_value(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(value.strip()))
    except (TypeError, ValueError):
        return 0.0


def progress_fps_value(progress: dict[str, str], source_fps: float = 0.0) -> float:
    """Return FFmpeg's FPS, deriving it from speed when short jobs report 0."""
    reported = parse_fps_value(progress.get("fps"))
    if reported > 0:
        return reported
    return max(0.0, float(source_fps or 0.0)) * parse_speed_multiplier(progress.get("speed"))


class RollingProgressRate:
    """Throughput over recent progress samples, excluding pre-first-frame wait.

    FFmpeg's fps/speed are lifetime averages. Keep those values in the raw
    progress packet, and calculate the UI's recent rate from frame/time deltas.
    Each process (including each parallel proxy) owns its own tracker.
    """

    def __init__(self, window_seconds: float = 3.0, clock=time.monotonic):
        self.window_seconds = max(0.5, float(window_seconds))
        self.clock = clock
        self.samples: deque[tuple[float, float, float | None]] = deque()

    def update(self, progress: dict[str, str], source_fps: float = 0.0) -> tuple[float, float]:
        now = self.clock()
        seconds = max(0.0, progress_seconds(progress))
        try:
            frames = float(progress["frame"])
            if not math.isfinite(frames) or frames < 0:
                frames = None
        except (KeyError, TypeError, ValueError):
            frames = None
        if not math.isfinite(seconds):
            return 0.0, 0.0
        if not self.samples and seconds <= 0 and not frames:
            return 0.0, 0.0
        if self.samples and (
            seconds < self.samples[-1][1] - 0.001
            or (frames is not None and self.samples[-1][2] is not None
                and frames < self.samples[-1][2])
        ):
            self.samples.clear()
        self.samples.append((now, seconds, frames))
        cutoff = now - self.window_seconds
        while len(self.samples) > 2 and self.samples[1][0] <= cutoff:
            self.samples.popleft()
        first_time, first_seconds, first_frames = self.samples[0]
        elapsed = now - first_time
        if elapsed < 0.2:
            return 0.0, 0.0
        # Video timestamps can be held back by audio interleaving. Frame
        # deltas give matching FPS and x values for a constant-rate video.
        if frames is not None and first_frames is not None:
            fps = max(0.0, frames - first_frames) / elapsed
            speed = fps / source_fps if source_fps > 0 else max(0.0, seconds - first_seconds) / elapsed
        else:
            speed = max(0.0, seconds - first_seconds) / elapsed
            fps = speed * max(0.0, source_fps)
        return fps, speed


def split_ranges(duration: float, segments: int) -> list[tuple[float, float]]:
    if duration <= 0:
        raise ValueError("duration must be positive")
    segments = max(1, int(segments))
    unit = duration / segments
    ranges = []
    for index in range(segments):
        start = index * unit
        end = duration if index == segments - 1 else (index + 1) * unit
        ranges.append((start, end - start))
    return ranges


def read_gpu_sample(nvidia_smi: str | None = None, gpu_index: int = 0) -> dict[str, str]:
    smi = nvidia_smi or find_binary("nvidia-smi")
    if not smi:
        return {}
    fields = [
        "utilization.gpu", "utilization.encoder", "utilization.decoder",
        "memory.used", "temperature.gpu", "power.draw",
    ]
    code, output = _run_text([
        smi,
        f"--id={gpu_index}",
        f"--query-gpu={','.join(fields)}",
        "--format=csv,noheader,nounits",
    ], timeout=5)
    if code or not output.strip():
        return {}
    values = [value.strip() for value in output.strip().splitlines()[0].split(",")]
    return dict(zip(fields, values))
