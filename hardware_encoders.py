"""Vendor-neutral codec selection and software/AMF/QSV encoder arguments.

Encoder names in FFmpeg's build are not proof that a driver can use them.
Capabilities supplies only initialized hardware encoders to this module.
"""
from __future__ import annotations

import re

HARDWARE_ENCODERS = frozenset(f'{family}_{backend}' for backend in ('nvenc', 'amf', 'qsv')
                            for family in ('h264', 'hevc', 'av1'))
CODEC_LABELS = {
    'hevc_nvenc': 'HEVC / H.265 (NVENC)', 'av1_nvenc': 'AV1 (NVENC)', 'h264_nvenc': 'H.264 (NVENC)',
    'hevc_amf': 'HEVC / H.265 (AMD AMF)', 'av1_amf': 'AV1 (AMD AMF)', 'h264_amf': 'H.264 (AMD AMF)',
    'hevc_qsv': 'HEVC / H.265 (Intel QSV)', 'av1_qsv': 'AV1 (Intel QSV)', 'h264_qsv': 'H.264 (Intel QSV)',
    'libx265': 'HEVC / H.265 (CPU)', 'libx264': 'H.264 (CPU)',
}


def encoder_vendor(codec):
    if codec in HARDWARE_ENCODERS:
        return {'nvenc': 'nvidia', 'amf': 'amd', 'qsv': 'intel'}[codec.rsplit('_', 1)[-1]]
    return 'cpu' if codec in {'libx264', 'libx265', 'prores_ks', 'dnxhd'} else 'unknown'


def is_hardware_codec(codec):
    return codec in HARDWARE_ENCODERS


def available_codecs(caps):
    hardware = set(getattr(caps, 'hardware_codecs', ()) or ())
    for name in ('nvenc_codecs', 'amf_codecs', 'qsv_codecs'):
        hardware.update(getattr(caps, name, ()) or ())
    software = getattr(caps, 'software_codecs', {'libx264', 'libx265'})
    return (hardware & HARDWARE_ENCODERS) | (set(software or ()) & {'libx264', 'libx265'})


def preferred_codec(caps, family='hevc', vendor=None):
    if family not in {'h264', 'hevc', 'av1'}:
        raise ValueError('视频编码格式必须为 H.264、HEVC 或 AV1。')
    available = available_codecs(caps)
    preferred_vendor = vendor or getattr(caps, 'gpu_vendor', 'nvidia')
    vendors = list(dict.fromkeys([preferred_vendor, 'nvidia', 'amd', 'intel']))
    families = [family] if family == 'h264' else list(dict.fromkeys([family, 'hevc', 'h264', 'av1']))
    for candidate_family in families:
        for current in vendors:
            backend = {'nvidia': 'nvenc', 'amd': 'amf', 'intel': 'qsv'}.get(current)
            candidate = f'{candidate_family}_{backend}'
            if candidate in available:
                return candidate
    # CPU fallback uses the requested family when that encoder exists.
    for candidate in (['libx264'] if family == 'h264' else ['libx265', 'libx264']):
        if candidate in available:
            return candidate
    raise ValueError('没有可用的视频编码器，请检查 FFmpeg 和显卡驱动。')


def codec_label(codec):
    return CODEC_LABELS.get(codec, codec)


def codec_from_label(label):
    if label in CODEC_LABELS:
        return label
    for codec, display in CODEC_LABELS.items():
        if label == display:
            return codec
    raise ValueError('请选择有效的视频编码器。')


def _quality_preset(preset, vendor):
    match = re.fullmatch(r'p([1-7])', str(preset))
    if match:
        number = int(match.group(1))
        return ({1:'veryfast',2:'faster',3:'fast',4:'medium',5:'slow',6:'slower',7:'veryslow'}[number]
                if vendor != 'amd' else 'speed' if number <= 2 else 'balanced' if number <= 4 else 'quality')
    allowed = ({'speed','balanced','quality','high_quality'} if vendor == 'amd' else
               {'ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow','placebo'})
    if preset not in allowed:
        raise ValueError('编码预设无效。')
    if vendor == 'intel':
        return {'ultrafast':'veryfast','superfast':'veryfast','placebo':'veryslow'}.get(preset,preset)
    return preset


def build_portable_video_args(settings, depth):
    """Return arguments for AMF/QSV/x264/x265; NVENC retains its original path."""
    codec = settings.codec
    vendor = encoder_vendor(codec)
    if vendor not in {'amd', 'intel', 'cpu'} or codec not in CODEC_LABELS:
        raise ValueError('此编码器没有跨厂商参数配置。')
    if codec.startswith('h264_') and depth > 8:
        raise ValueError('H.264 硬件编码使用 8-bit；10-bit 请选择 HEVC 或 AV1。')
    preset = _quality_preset(settings.preset, vendor)
    quality = max(0, min(51, int(settings.quality)))
    args = ['-c:v', codec]
    if vendor == 'amd':
        args += ['-usage','transcoding','-quality',preset,'-async_depth','1']
        if settings.video_bitrate.strip() or settings.maxrate.strip():
            args += ['-rc','vbr_peak','-b:v',settings.video_bitrate.strip() or settings.maxrate.strip()]
        else:
            # AMF AV1 quantizers have a 0..255 range; AVC/HEVC use 0..51.
            qp = round(quality * 255 / 51) if codec == 'av1_amf' else quality
            args += ['-rc','cqp','-qp_i',str(qp),'-qp_p',str(qp)]
        args += ['-bf','0']
        if depth > 8:
            args += ['-bitdepth','10']
            if codec == 'hevc_amf': args += ['-profile:v','main10']
    elif vendor == 'intel':
        args += ['-preset',preset,'-async_depth','1']
        if settings.video_bitrate.strip() or settings.maxrate.strip():
            args += ['-b:v',settings.video_bitrate.strip() or settings.maxrate.strip()]
        else:
            # QSV ICQ accepts 1..51; zero selects a different rate-control
            # path in FFmpeg instead of the intended highest ICQ quality.
            args += ['-global_quality',str(max(1,quality))]
        args += ['-bf',str(settings.bframes)]
        if codec == 'hevc_qsv' and depth > 8: args += ['-profile:v','main10']
    else:
        args += ['-preset',preset,'-bf',str(settings.bframes)]
        args += (['-b:v',settings.video_bitrate.strip()] if settings.video_bitrate.strip()
                 else ['-crf',str(quality)])
    if settings.maxrate.strip():
        args += ['-maxrate',settings.maxrate.strip(),'-bufsize',settings.maxrate.strip()]
    args += ['-g',str(settings.gop),'-fps_mode','passthrough']
    args += list(settings.extra_args)
    return args
