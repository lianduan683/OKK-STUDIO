"""Conservative, deterministic parallel-export selection without GPU probes.

This estimates allocation capacity, not the fastest worker count or currently
free VRAM. Manual settings remain authoritative outside the CUDA/NVENC auto
route. Decoding still budgets the original source size after GPU prescaling.
"""
from __future__ import annotations

from collections.abc import Mapping
import math

MIB = 1024 ** 2
GIB = 1024 ** 3
MAX_AUTO_WORKERS = 4
UNKNOWN_MEMORY_BYTES = 8 * GIB
DECODE_SURFACES = 24
FLOAT_INTERMEDIATES = 8
ENCODE_SURFACES = 12
WORKER_OVERHEAD_BYTES = 512 * MIB
ALLOCATION_MARGIN = 1.25
RESERVE_FRACTION = .20
RESOLUTION_HEIGHTS = {"1080p": 1080, "1440p": 1440, "4k": 2160, "8k": 4320}


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _media_entries(media):
    if isinstance(media, Mapping):
        return tuple(media.values())
    if hasattr(media, "width"):
        return (media,)
    return tuple(media or ())


def _picture(info):
    width, height = getattr(info, "width", 0), getattr(info, "height", 0)
    known = _positive(width) and _positive(height)
    # Missing dimensions cannot justify optimistic four-way allocations.
    if not known:
        return (7680, 4320, 10, "unknown", False)
    depth = getattr(info, "bit_depth", 0)
    depth = depth if _positive(depth) else 10
    return (int(width), int(height), depth, str(getattr(info, "pixel_format", "")).lower(), True)


def _source_frame_bytes(picture):
    width, height, depth, pixel_format, _ = picture
    samples = 1.5  # NV12/P010 and camera 4:2:0 surfaces.
    if any(value in pixel_format for value in ("422", "nv16", "p210", "p212", "p216")):
        samples = 2.
    elif any(value in pixel_format for value in ("444", "gbr", "rgb", "bgr")):
        samples = 4. if any(value in pixel_format for value in ("rgba", "bgra", "argb", "abgr")) else 3.
    return width * height * samples * (2 if depth > 8 else 1)


def _worker_budget(settings, pictures, output_size=None):
    source = max(_source_frame_bytes(picture) for picture in pictures)
    target_height = RESOLUTION_HEIGHTS.get(str(getattr(settings, "resolution", "original")).lower())
    output_pixels = output_size[0] * output_size[1] if output_size is not None else max(
        max(2, round(width * target_height / height / 2) * 2) * target_height
        if target_height else width * height
        for width, height, _, _, _ in pictures)
    depth_setting = getattr(settings, "bit_depth", "auto")
    output_ten_bit = (getattr(settings, "codec", "") not in {"h264_nvenc", "h264_amf", "h264_qsv"}
                      and (depth_setting == "10-bit" or
                           depth_setting != "8-bit" and any(picture[2] > 8 for picture in pictures)))
    encoded_frame = output_pixels * (3 if output_ten_bit else 1.5)
    # RGBA16F intermediates remain float even for an 8-bit deliverable.
    floating_frame = output_pixels * 8
    return ALLOCATION_MARGIN * (DECODE_SURFACES * source +
        FLOAT_INTERMEDIATES * floating_frame + ENCODE_SURFACES * encoded_frame + WORKER_OVERHEAD_BYTES)


def choose_export_workers(settings, caps, media, *, output_size=None) -> int:
    """Choose 1..4 workers while leaving all caller objects unchanged.

    Automatic capacity selection applies only to an available NVENC encoder
    with CUDA decoding requested and detected. CPU/manual routes return the
    exact manual count. Unknown VRAM uses an 8-GiB assumption and a two-worker
    cap; unknown picture dimensions also cap automatic allocation at two.
    output_size optionally supplies the caller-resolved delivery dimensions;
    source allocations still use the largest original picture.
    """
    if output_size is not None:
        if (not isinstance(output_size, (tuple, list)) or len(output_size) != 2 or
                any(type(value) is not int or value < 2 or value % 2 for value in output_size)):
            raise ValueError("导出画幅必须包含两个不小于 2 的正偶数")
        output_size = tuple(output_size)
    manual = getattr(settings, "parallel_workers", 1)
    if type(manual) is not int or not 1 <= manual <= 4:
        raise ValueError("导出并行任务数必须是 1–4 的整数")
    automatic = getattr(settings, "auto_parallel", False)
    if type(automatic) is not bool:
        raise ValueError("自动导出并行设置必须为布尔值")
    codec = getattr(settings, "codec", "")
    if (not automatic or not str(codec).endswith("_nvenc") or
            getattr(settings, "decode_mode", "") != "cuda" or not getattr(caps, "cuda_decode", False) or
            codec not in (getattr(caps, "nvenc_codecs", ()) or ())):
        return manual
    pictures = tuple(_picture(info) for info in _media_entries(media)) or ((7680, 4320, 10, "unknown", False),)
    memory_mb = getattr(caps, "gpu_memory_mb", 0)
    known_memory = _positive(memory_mb)
    total = memory_mb * MIB if known_memory else UNKNOWN_MEMORY_BYTES
    reserve = max(2 * GIB, total * RESERVE_FRACTION)
    available = max(0, total - reserve)
    maximum = min(4, MAX_AUTO_WORKERS)
    if getattr(getattr(caps, 'gpu_profile', None), 'model', None) == 4050:
        # The 4050 Laptop has one NVENC engine and a small VRAM budget.
        # Automatic export stays single-job; an explicit manual choice remains
        # authoritative in the early return above.
        maximum = 1
    if not known_memory or not all(picture[4] for picture in pictures):
        maximum = min(maximum, 2)
    return max(1, min(maximum, int(available // _worker_budget(settings, pictures, output_size))))
