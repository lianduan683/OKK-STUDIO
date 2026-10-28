from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GpuProfile:
    """Hardware profile used to tune the UI without relying on marketing names."""

    raw_name: str
    display_name: str
    model: int | None
    series: int | None
    architecture: str
    form_factor: str
    nvenc_generation: int | None
    nvenc_engines: int
    nvdec_engines: int
    hardware_av1: bool
    in_supported_range: bool
    recommended_proxy_parallel: int
    vendor: str = 'unknown'

    @property
    def engine_text(self) -> str:
        return (f"{self.nvenc_engines} 组 NVENC" if self.vendor == 'nvidia' else
                {'amd':'AMD AMF', 'intel':'Intel QSV'}.get(self.vendor, '软件兼容模式'))


def _clean_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", (name or "").strip())
    cleaned = re.sub(r"^NVIDIA\s+GeForce\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^NVIDIA\s+", "", cleaned, flags=re.IGNORECASE)
    return cleaned or "未检测到 GPU"


def _consumer_model(name: str) -> int | None:
    # Match known GeForce product numbers exactly so workstation names such as
    # "RTX 5000 Ada Generation" are not mistaken for consumer 50-series GPUs.
    known_models = {
        2050, 2060, 2070, 2080,
        3050, 3060, 3070, 3080, 3090,
        4050, 4060, 4070, 4080, 4090,
        5050, 5060, 5070, 5080, 5090,
    }
    matches = re.findall(r"\bRTX\s*(\d{4})\b", name, flags=re.IGNORECASE)
    for raw in matches:
        model = int(raw)
        if model in known_models or model >= 6060:
            return model
    return None


def classify_gpu(name: str) -> GpuProfile:
    """Classify supported GeForce RTX 20/30/40/50 desktop and Laptop GPUs.

    Engine counts follow NVIDIA's Video Encode/Decode Support Matrix. Unknown
    future RTX models intentionally fall back to one engine and driver-managed
    scheduling instead of guessing an unsafe multi-engine setting.
    """

    raw_name = (name or "").strip()
    display_name = _clean_name(raw_name)
    upper = raw_name.upper()
    vendor = ('nvidia' if any(word in upper for word in ('NVIDIA', 'GEFORCE', 'QUADRO', 'RTX ')) else
              'amd' if any(word in upper for word in ('AMD', 'RADEON')) else
              'intel' if 'INTEL' in upper else 'unknown')
    model = _consumer_model(raw_name)
    series = model // 100 if model is not None else None
    is_laptop = model == 2050 or any(
        token in upper for token in ("LAPTOP", "NOTEBOOK", "MAX-Q", "MAX Q")
    )
    form_factor = "Laptop" if is_laptop else "台式"

    architecture = {'amd':'AMD Radeon', 'intel':'Intel GPU', 'unknown':'未知 GPU'}.get(vendor, '未知 NVIDIA')
    nvenc_generation: int | None = None
    nvenc_engines = 1 if vendor == 'nvidia' else 0
    nvdec_engines = 1
    hardware_av1 = False
    supported = False

    if series == 20:
        # RTX 2050 Laptop is an Ampere part despite its 20-series product name.
        architecture = "Ampere" if model == 2050 else "Turing"
        nvenc_generation = 7
        hardware_av1 = False
        supported = model in {2050, 2060, 2070, 2080}
        nvenc_engines = 1
    elif series == 30:
        architecture = "Ampere"
        nvenc_generation = 7
        hardware_av1 = False
        supported = model in {3050, 3060, 3070, 3080, 3090}
        nvenc_engines = 1
    elif series == 40:
        architecture = "Ada Lovelace"
        nvenc_generation = 8
        hardware_av1 = True
        supported = model is not None and 4050 <= model <= 4090
        if model is not None:
            if model >= 4080:
                nvenc_engines = 2
            elif model == 4070 and "TI" in upper and not is_laptop:
                nvenc_engines = 2
    elif series == 50:
        architecture = "Blackwell"
        nvenc_generation = 9
        hardware_av1 = True
        supported = model is not None and 5050 <= model <= 5090
        if model is not None:
            if model >= 5090:
                nvenc_engines = 3
            elif model >= 5080:
                nvenc_engines = 2
            elif model == 5070 and "TI" in upper and not is_laptop:
                nvenc_engines = 2
            # NVIDIA's current matrix lists two NVDEC engines only for the
            # GeForce RTX 5080/5090 desktop and Laptop products.
            if model >= 5080:
                nvdec_engines = 2
    elif series is not None and series >= 60:
        architecture = "新一代 RTX（兼容模式）"
        hardware_av1 = True
        supported = True
        # Future products are capability-detected, but their physical engine
        # count is not guessed until NVIDIA publishes it.
        nvenc_engines = 1

    if is_laptop:
        recommended_proxy_parallel = min(2, nvenc_engines)
    else:
        recommended_proxy_parallel = min(3, nvenc_engines)
    recommended_proxy_parallel = max(1, recommended_proxy_parallel)

    return GpuProfile(
        raw_name=raw_name,
        display_name=display_name,
        model=model,
        series=series,
        architecture=architecture,
        form_factor=form_factor,
        nvenc_generation=nvenc_generation,
        nvenc_engines=nvenc_engines,
        nvdec_engines=nvdec_engines,
        hardware_av1=hardware_av1,
        in_supported_range=supported,
        recommended_proxy_parallel=recommended_proxy_parallel,
        vendor=vendor,
    )
