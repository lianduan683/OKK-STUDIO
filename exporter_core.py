from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from effects_core import (
    EffectSettings,
    build_effect_filter_graph,
    build_effect_filters,
    overlay_active,
    validate_effects,
)
from gpu_profiles import GpuProfile, classify_gpu
from video_stack import stream_has_alpha
from hardware_encoders import HARDWARE_ENCODERS, build_portable_video_args, encoder_vendor


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
    has_video: bool = True
    has_alpha: bool = False


@dataclass(slots=True)
class Capabilities:
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    gpu_name: str = "GPU not detected"
    driver_version: str = "unknown"
    gpu_memory_mb: int = 0
    gpu_profile: GpuProfile = field(default_factory=lambda: classify_gpu(""))
    nvenc_codecs: set[str] = field(default_factory=set)
    cuda_decode: bool = False
    scale_cuda: bool = False
    split_encode: set[str] = field(default_factory=set)
    raw_encoder_help: dict[str, str] = field(default_factory=dict)
    amf_codecs: set[str] = field(default_factory=set)
    qsv_codecs: set[str] = field(default_factory=set)
    software_codecs: set[str] = field(default_factory=lambda: {'libx264', 'libx265'})
    gpu_vendor: str = 'unknown'
    gpu_names: list[str] = field(default_factory=list)
    ten_bit_codecs: set[str] = field(default_factory=set)
    hardware_probe_complete: bool = False
    encoder_probe_errors: dict[str, str] = field(default_factory=dict)
    compiled_hardware_codecs: set[str] = field(default_factory=set)

    @property
    def hardware_codecs(self) -> set[str]:
        return self.nvenc_codecs | self.amf_codecs | self.qsv_codecs

    def __post_init__(self) -> None:
        if not self.gpu_profile.raw_name and self.gpu_name not in {"NVIDIA GPU not detected", "GPU not detected"}:
            self.gpu_profile = classify_gpu(self.gpu_name)
        if self.gpu_vendor == 'unknown':
            self.gpu_vendor = _gpu_vendor(self.gpu_name)


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
    profile: str = ""
    pixel_format: str = ""
    audio_sample_rate: int | None = 48000
    parallel_workers: int = 1
    auto_parallel: bool = False


class ExportCancelled(RuntimeError):
    pass


def _run_text(args: Sequence[str], timeout: float = 15, *, structured: bool = False) -> tuple[int, str]:
    try:
        result = subprocess.run(
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if structured else subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        # ffprobe can successfully return JSON while a decoder prints a
        # warning. Keep that diagnostic channel out of the JSON document.
        output = result.stdout
        if structured and result.returncode and result.stderr:
            output += "\n" + result.stderr
        return result.returncode, output
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


def _media_probe_command(ffprobe: str, path: str) -> list[str]:
    return [
        ffprobe,
        "-v", "error",
        "-show_entries",
        "format=duration:format_tags=timecode:"
        "stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,pix_fmt,"
        "bits_per_raw_sample,color_primaries,color_transfer,color_space,color_range,"
        "sample_aspect_ratio,duration:stream_tags=timecode,alpha_mode:stream_disposition=attached_pic",
        "-of", "json",
        path,
    ]


def probe_media(ffprobe: str, path: str, *, allow_audio_only: bool = False) -> MediaInfo:
    args = _media_probe_command(ffprobe, path)
    code, output = _run_text(args, timeout=30, structured=True)
    if code:
        raise RuntimeError(f"ffprobe could not read the input:\n{output.strip()}")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned invalid JSON") from exc
    return _media_info_from_payload(path, payload, allow_audio_only=allow_audio_only)


def _media_info_from_payload(path: str, payload: dict, *, allow_audio_only: bool = False) -> MediaInfo:
    """Shared interpretation for regular and explicitly cancellable probes."""
    streams = payload.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"
                  and not (item.get("disposition") or {}).get("attached_pic")), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if video is None and (not allow_audio_only or audio is None):
        raise RuntimeError("No video stream was found in this file")
    has_video = video is not None
    video = video or {}
    format_info = payload.get("format", {})
    # Camera audio can extend beyond the last video frame. Timeline cuts and
    # camera-off gaps must be based on the picture duration, not that audio tail.
    try:
        duration = float(video.get("duration") or ((audio or {}).get("duration") if not has_video else None)
                         or format_info.get("duration") or 0)
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
        video_codec=str(video.get("codec_name") or ("unknown" if has_video else "none")),
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
        has_video=has_video,
        has_alpha=has_video and stream_has_alpha(video),
    )


def _gpu_vendor(name: str) -> str:
    name = str(name or '').casefold()
    if any(word in name for word in ('nvidia', 'geforce', 'quadro', 'rtx ')): return 'nvidia'
    if any(word in name for word in ('amd', 'radeon', 'advanced micro devices')): return 'amd'
    if 'intel' in name: return 'intel'
    return 'unknown'


def _windows_adapters() -> list[dict]:
    if os.name != 'nt': return []
    code, output = _run_text(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
        'Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion | ConvertTo-Json -Compress'], timeout=6)
    if code: return []
    try:
        payload = json.loads(output)
        rows = payload if isinstance(payload, list) else [payload]
        return [row for row in rows if isinstance(row, dict) and isinstance(row.get('Name'), str)]
    except (ValueError, TypeError): return []


def _probe_hardware_encoder(ffmpeg, ffprobe, codec, depth=8):
    """Initialize, encode and inspect two frames; never infer support from -encoders."""
    try:
        with tempfile.TemporaryDirectory(prefix='okk_encoder_probe_') as temporary:
            destination = str(Path(temporary)/'probe.mkv')
            settings = ExportSettings(codec=codec, preset='p2', quality=26, bframes=0,
                                      decode_mode='cpu', bit_depth=f'{depth}-bit')
            reference = MediaInfo('probe', duration=.1, width=256, height=144, fps=30, bit_depth=depth)
            args = build_video_args(settings, reference, Capabilities())
            command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                '-threads', '1', '-filter_threads', '1', '-f', 'lavfi', '-i',
                'testsrc2=size=256x144:rate=30:duration=0.1', '-frames:v', '2', '-an',
                '-vf', 'format=p010le' if depth == 10 else 'format=nv12', *args, destination]
            code, output = _run_text(command, timeout=6)
            if code or not Path(destination).is_file() or Path(destination).stat().st_size <= 0:
                return False, (output.strip()[-1200:] or '编码器未输出可验证的视频帧。')
            code, output = _run_text(_media_probe_command(ffprobe, destination), timeout=4, structured=True)
            if code: return False, output.strip()[-1200:]
            media = _media_info_from_payload(destination, json.loads(output))
            expected = codec.split('_', 1)[0]
            if not media.has_video or media.video_codec != expected or media.duration <= 0 or media.bit_depth != depth:
                return False, f'探测输出格式不符：{media.video_codec} / {media.bit_depth}-bit'
            return True, ''
    except (OSError, ValueError, RuntimeError) as error:
        return False, str(error)[-1200:]


def detect_capabilities(app_dir: str | Path | None = None) -> Capabilities:
    ffmpeg = find_binary('ffmpeg', app_dir)
    ffprobe = find_binary('ffprobe', app_dir)
    caps = Capabilities(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)
    adapters = _windows_adapters()
    caps.gpu_names = list(dict.fromkeys(row['Name'].strip() for row in adapters if row['Name'].strip()))
    smi = find_binary('nvidia-smi', app_dir)
    if smi:
        code, output = _run_text([smi, '--query-gpu=name,driver_version,memory.total',
                                  '--format=csv,noheader,nounits'])
        if code == 0 and output.strip():
            rows = [line.split(',', 2) for line in output.strip().splitlines()]
            for row in rows:
                if row[0].strip() not in caps.gpu_names: caps.gpu_names.append(row[0].strip())
            parts = [part.strip() for part in rows[0]]
            caps.gpu_name = parts[0]
            if len(parts) > 1: caps.driver_version = parts[1]
            if len(parts) > 2:
                try: caps.gpu_memory_mb = int(float(parts[2]))
                except ValueError: pass
    if caps.gpu_name == 'GPU not detected' and caps.gpu_names:
        caps.gpu_name = caps.gpu_names[0]
        caps.driver_version = next((str(row.get('DriverVersion') or 'unknown') for row in adapters
                                    if row['Name'].strip() == caps.gpu_name), 'unknown')
    caps.gpu_profile = classify_gpu(caps.gpu_name)
    caps.gpu_vendor = _gpu_vendor(caps.gpu_name)
    if not ffmpeg: return caps
    _, encoders = _run_text([ffmpeg, '-hide_banner', '-encoders'])
    caps.software_codecs = {codec for codec in ('libx264', 'libx265') if re.search(rf'\b{codec}\b', encoders)}
    caps.compiled_hardware_codecs = {codec for codec in HARDWARE_ENCODERS if re.search(rf'\b{codec}\b', encoders)}
    for codec in sorted(caps.compiled_hardware_codecs):
        _, help_text = _run_text([ffmpeg, '-hide_banner', '-h', f'encoder={codec}'])
        caps.raw_encoder_help[codec] = help_text
        usable, error = _probe_hardware_encoder(ffmpeg, ffprobe, codec, 8) if ffprobe else (False, '缺少 ffprobe，无法验证编码器。')
        if not usable:
            caps.encoder_probe_errors[codec] = error
            continue
        target = {'nvidia':caps.nvenc_codecs, 'amd':caps.amf_codecs, 'intel':caps.qsv_codecs}[encoder_vendor(codec)]
        target.add(codec)
        if codec.endswith('_nvenc') and 'split_encode_mode' in help_text: caps.split_encode.add(codec)
        if not codec.startswith('h264_'):
            usable10, error10 = _probe_hardware_encoder(ffmpeg, ffprobe, codec, 10)
            if usable10: caps.ten_bit_codecs.add(codec)
            else: caps.encoder_probe_errors[codec+':10-bit'] = error10
    caps.hardware_probe_complete = True
    # Choose a usable hardware vendor rather than presenting an unavailable
    # discrete GPU as the only choice on a hybrid laptop.
    for vendor, codecs in ((caps.gpu_vendor, caps.hardware_codecs), ('nvidia', caps.nvenc_codecs),
                           ('amd', caps.amf_codecs), ('intel', caps.qsv_codecs)):
        if any(encoder_vendor(codec) == vendor for codec in codecs):
            caps.gpu_vendor = vendor
            name = next((name for name in caps.gpu_names if _gpu_vendor(name) == vendor), None)
            if name:
                if name != caps.gpu_name:
                    # Win32_VideoController.AdapterRAM truncates modern VRAM
                    # and is meaningless for UMA: do not reuse another GPU's.
                    caps.gpu_memory_mb = 0
                    caps.driver_version = next((str(row.get('DriverVersion') or 'unknown') for row in adapters
                                                if row['Name'].strip() == name), 'unknown')
                caps.gpu_name = name
            break
    caps.gpu_profile = classify_gpu(caps.gpu_name)
    _, hwaccels = _run_text([ffmpeg, '-hide_banner', '-hwaccels'])
    caps.cuda_decode = bool(caps.nvenc_codecs and re.search(r'(?m)^cuda\s*$', hwaccels))
    _, filters = _run_text([ffmpeg, '-hide_banner', '-filters'])
    caps.scale_cuda = caps.cuda_decode and 'scale_cuda' in filters
    return caps


def _resolution_height(value: str) -> int | None:
    lookup = {"1080p": 1080, "1440p": 1440, "4k": 2160, "8k": 4320}
    return lookup.get(value.lower())


def _target_depth(settings: ExportSettings, media: MediaInfo) -> int:
    if settings.bit_depth == "10-bit":
        return 10
    if settings.bit_depth == "8-bit":
        return 8
    if settings.codec in {"h264_nvenc", "h264_amf", "h264_qsv"}:
        return 8
    return 10 if media.bit_depth > 8 else 8


SOFTWARE_PROXY_ENCODERS = frozenset({"dnxhd", "prores_ks"})


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def _square_pixel_width(media: MediaInfo, height: int) -> int:
    sar = 1.0
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*[:/]\s*([0-9]+(?:\.[0-9]+)?)\s*", media.sample_aspect_ratio or "")
    if match:
        numerator, denominator = float(match.group(1)), float(match.group(2))
        if numerator > 0 and denominator > 0:
            sar = numerator / denominator
    return _even(media.width * sar * height / media.height)


def _software_pixel_format(settings: ExportSettings) -> str:
    if settings.pixel_format:
        return settings.pixel_format
    if settings.codec == "prores_ks" or settings.profile == "dnxhr_hqx":
        return "yuv422p10le"
    return "yuv422p"


def _software_frame_size(
    settings: ExportSettings,
    media: MediaInfo,
    rotation: int = 0,
) -> tuple[int, int]:
    height = _even(_resolution_height(settings.resolution) or media.height or 1080)
    if media.width > 0 and media.height > 0:
        width = _square_pixel_width(media, height)
    else:
        width = _even(1920 * height / 1080)
    if rotation in {90, 270}:
        # The fixed software-proxy scale runs after the effect chain.  Without
        # swapping the target dimensions, a 1920x1080 proxy scale would flatten
        # a rotated 1080x1920 frame back into landscape and visually cancel the
        # rotation.  Swap the target frame so the output keeps the rotated
        # orientation and square pixels.
        width, height = height, width
    if settings.codec == "dnxhd" and width < 256:
        width = 256
        if media.width > 0 and media.height > 0:
            if rotation in {90, 270}:
                height = max(120, _even(width * media.width / media.height))
            else:
                height = max(120, _even(width * media.height / media.width))
    return width, height


def build_video_filter(
    settings: ExportSettings,
    media: MediaInfo,
    caps: Capabilities,
    *,
    effects: EffectSettings | None = None,
    effect_duration: float | None = None,
) -> list[str]:
    """Build the single video filter graph used by one export.

    ``effects`` is opt-in.  Passing ``None`` (the default for every existing
    export workflow) produces exactly the same command as before, so ordinary
    export, trim, join and proxy output cannot be changed implicitly.
    """

    effect_filters: list[str] = []
    effect_rotation = 0
    validated_effects: EffectSettings | None = None
    resolved_duration: float | None = None
    if effects is not None:
        resolved_duration = effect_duration if effect_duration is not None else media.duration
        validated_effects = validate_effects(effects, duration=resolved_duration)
        effect_rotation = validated_effects.rotation
        if overlay_active(validated_effects) and not os.path.isfile(validated_effects.overlay_path):
            raise ValueError(f"叠加特效素材文件不存在：{validated_effects.overlay}")
        effect_filters = build_effect_filters(validated_effects, duration=resolved_duration)

    if settings.codec in SOFTWARE_PROXY_ENCODERS:
        width, height = _software_frame_size(settings, media, rotation=effect_rotation)
        scale = f"scale={width}:{height}:flags=lanczos"
        if media.color_range == "pc":
            scale += ":in_range=pc:out_range=tv"
        suffix = [scale, "setsar=1", f"format={_software_pixel_format(settings)}"]
        graph = build_effect_filter_graph(
            validated_effects, duration=resolved_duration, suffix=suffix,
        ) if validated_effects is not None else ""
        return ["-vf", graph] if graph else ["-vf", ",".join(suffix)]

    height = _resolution_height(settings.resolution)
    depth = _target_depth(settings, media)
    gpu_format = "p010le" if depth == 10 else "nv12"
    resize_needed = height is not None and height != media.height
    format_needed = depth != media.bit_depth

    if settings.decode_mode == "cuda" and settings.codec.endswith("_nvenc"):
        if not effect_filters and not overlay_active(validated_effects):
            # The common 8K 10-bit HEVC -> 8K 10-bit HEVC path can pass decoded
            # CUDA frames directly into NVENC. Avoiding a no-op scale/filter saves
            # a full-frame allocation and copy for every 8K frame.
            if not resize_needed and not format_needed:
                return []
            if caps.scale_cuda:
                dimensions = f"-2:{height}:" if resize_needed else ""
                return ["-vf", f"scale_cuda={dimensions}format={gpu_format}:passthrough=0"]
            # hwdownload must match the decoder surface, not the requested
            # output depth. A P010 -> NV12 conversion is a separate CPU step
            # when this FFmpeg build does not contain scale_cuda.
            source_format = "p016le" if media.bit_depth > 10 else "p010le" if media.bit_depth > 8 else "nv12"
            filters = ["hwdownload", f"format={source_format}"]
            if resize_needed:
                filters.append(f"scale=-2:{height}")
            if resize_needed or source_format != gpu_format:
                filters.append(f"format={gpu_format}")
            filters.append("hwupload_cuda")
            return ["-vf", ",".join(filters)]

        # Effects such as eq/unsharp/vignette/fade run in libavfilter. A CUDA
        # frame therefore has to be downloaded once, processed on the CPU and
        # uploaded again for NVENC. The UI says so explicitly instead of
        # pretending the effect stack is GPU realtime.  hwdownload must target
        # the decoded frame's own layout (source depth), while the final
        # format follows the requested output depth.
        if media.bit_depth > 10:
            source_gpu_format = "p016le"
        elif media.bit_depth > 8:
            source_gpu_format = "p010le"
        else:
            source_gpu_format = "nv12"
        prefix = ["hwdownload", f"format={source_gpu_format}"]
        suffix = ([f"scale=-2:{height}"] if resize_needed else [])
        suffix += [f"format={gpu_format}", "hwupload_cuda"]
        if validated_effects is not None:
            graph = build_effect_filter_graph(
                validated_effects, duration=resolved_duration,
                prefix=prefix, suffix=suffix,
            )
            if graph:
                return ["-vf", graph]
        return ["-vf", ",".join(prefix + suffix)]

    suffix = ([f"scale=-2:{height}"] if resize_needed else []) + [f"format={gpu_format}"]
    if validated_effects is not None:
        graph = build_effect_filter_graph(
            validated_effects, duration=resolved_duration, suffix=suffix,
        )
        if graph:
            return ["-vf", graph]
    return ["-vf", ",".join(suffix)]


def _supported_option(help_text: str, option: str) -> bool:
    return re.search(rf"(?m)^\s*-{re.escape(option)}\s", help_text) is not None


# CUDA hwframes only download cleanly from these common camera layouts.  Other
# 4:2:2/4:4:4 formats are decoded on the CPU when effects are required; the
# encoder still uses the unified NVENC settings.
CUDA_EFFECT_DOWNLOAD_FORMATS = frozenset({
    "nv12", "yuv420p", "yuvj420p", "p010le", "yuv420p10le", "p016le",
})


def cuda_effects_download_supported(media: MediaInfo) -> bool:
    return str(media.pixel_format or "").lower() in CUDA_EFFECT_DOWNLOAD_FORMATS


def _compatible_av1_level(settings: ExportSettings, media: MediaInfo) -> str | None:
    """Avoid reserved auto levels emitted by recent NVENC drivers.

    AV1 Annex A defines levels through 6.3; 7.x remains unspecified and is
    rejected by the bundled libaom decoder. Use high-tier limits, with ample
    bitrate headroom for CQ jobs, and leave unsupported sizes to the encoder.
    https://aomediacodec.github.io/av1-spec/#levels
    """
    if media.width <= 0 or media.height <= 0 or not math.isfinite(media.fps) or media.fps <= 0:
        return None
    height = _resolution_height(settings.resolution) or media.height
    width = _even(media.width * height / media.height)
    bitrate = 160_000_000.0 if not settings.video_bitrate and not settings.maxrate else 0.0
    for value in (settings.video_bitrate, settings.maxrate):
        if not value:
            continue
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)\s*", value)
        if not match:
            return None
        bitrate = max(bitrate, float(match[1]) * {"":1, "k":1e3, "m":1e6, "g":1e9}[match[2].lower()])
    for level, max_pixels, max_width, max_height, rate, mbps in (
        ("5.1", 8912896, 8192, 4352, 534773760, 160),
        ("5.2", 8912896, 8192, 4352, 1069547520, 240),
        ("6.0", 35651584, 16384, 8704, 1069547520, 240),
        ("6.1", 35651584, 16384, 8704, 2139095040, 480),
        ("6.2", 35651584, 16384, 8704, 4278190080, 800),
    ):
        if (width <= max_width and height <= max_height and width * height <= max_pixels
                and width * height * media.fps <= rate and bitrate <= mbps * 1e6):
            return level
    return None


def build_video_args(
    settings: ExportSettings,
    media: MediaInfo,
    caps: Capabilities,
) -> list[str]:
    if settings.codec in SOFTWARE_PROXY_ENCODERS:
        args = ["-c:v", settings.codec]
        if settings.profile:
            args += ["-profile:v", settings.profile]
        args += ["-fps_mode", "passthrough"]
        args += list(settings.extra_args)
        return args

    depth = _target_depth(settings, media)
    if (settings.codec in HARDWARE_ENCODERS and depth > 8
            and caps.hardware_probe_complete and settings.codec not in caps.ten_bit_codecs):
        raise ValueError('当前显卡/驱动未通过此编码器的 10-bit 验证，请选择其他编码器或明确设置 8-bit。')
    if settings.codec.startswith('h264_') and depth > 8:
        raise ValueError('H.264 硬件编码使用 8-bit；10-bit 请选择 HEVC 或 AV1。')
    if not settings.codec.endswith('_nvenc'):
        return build_portable_video_args(settings, depth)

    help_text = caps.raw_encoder_help.get(settings.codec, "")
    throughput_mode = settings.preset in {"p1", "p2"}
    args = ["-c:v", settings.codec, "-preset", settings.preset]
    if (settings.codec == "av1_nvenc" and _supported_option(help_text, "level")
            and not any(value in {"-level", "-level:v", "-tier", "-tier:v"} for value in settings.extra_args)):
        level = _compatible_av1_level(settings, media)
        if level is not None:
            args += ["-level:v", level, "-tier", "1"]
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
    if settings.codec == "hevc_nvenc" and depth == 10:
        # main10 is an HEVC profile; AV1 has main/high only, so passing
        # main10 to av1_nvenc makes FFmpeg reject the job.  The 10-bit pixel
        # format is already selected in build_video_filter.
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
    effects: EffectSettings | None = None,
) -> list[str]:
    command = [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "warning",
        "-stats_period", "0.25", "-progress", "pipe:1", "-nostats",
    ]
    if start is not None:
        command += ["-ss", f"{start:.6f}"]
    if settings.decode_mode == "cuda" and settings.codec.endswith("_nvenc"):
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
    effect_duration = duration
    if effect_duration is None:
        effect_duration = float(media.duration or 0.0)
        if start is not None:
            effect_duration = max(0.0, effect_duration - float(start))
    command += build_video_filter(
        settings, media, caps,
        effects=effects,
        effect_duration=effect_duration,
    )
    command += build_video_args(settings, media, caps)

    if video_only:
        command += ["-an"]
    elif settings.audio == "copy":
        command += ["-c:a", "copy"]
    elif settings.audio == "none":
        command += ["-an"]
    elif settings.audio in {"pcm", "pcm24", "pcm_s24le"}:
        command += ["-c:a", "pcm_s24le"]
    else:
        command += ["-c:a", "aac", "-b:a", settings.audio_bitrate]

    if not video_only and settings.audio not in {"copy", "none"} and settings.audio_sample_rate:
        command += ["-ar", str(settings.audio_sample_rate)]

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
    elif settings.audio in {"pcm", "pcm24", "pcm_s24le"}:
        command += ["-c:a", "pcm_s24le"]
    else:
        command += ["-c:a", "aac", "-b:a", settings.audio_bitrate]
    if settings.audio not in {"copy", "none"} and settings.audio_sample_rate:
        command += ["-ar", str(settings.audio_sample_rate)]
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
