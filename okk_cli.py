from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

from exporter_core import (
    CREATE_NO_WINDOW,
    ExportSettings,
    build_encode_command,
    detect_capabilities,
    parse_progress_line,
    probe_media,
    progress_seconds,
    quote_command,
)
from editor_core import normalize_audio_bitrate, normalize_video_bitrate
from hardware_encoders import HARDWARE_ENCODERS, available_codecs, codec_label, preferred_codec


INTERMEDIATE_CPU_CODECS = {"dnxhd", "prores", "cfhd", "rawvideo"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OKK GROUP DaVinci bridge exporter")
    parser.add_argument("--headless-export", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--codec", choices=("auto", *sorted(HARDWARE_ENCODERS), "libx264", "libx265"), default="auto")
    parser.add_argument("--preset", choices=("p1", "p2", "p4", "p6", "p7"), default="p2")
    parser.add_argument("--quality", type=int, default=20)
    parser.add_argument("--bit-depth", choices=("auto", "8-bit", "10-bit"), default="10-bit")
    parser.add_argument("--resolution", choices=("original", "1080p", "1440p", "4k", "8k"), default="original")
    parser.add_argument("--video-bitrate", default="80M")
    parser.add_argument("--maxrate", default="")
    parser.add_argument("--audio", choices=("copy", "aac", "none"), default="copy")
    parser.add_argument("--audio-bitrate", default="320k")
    parser.add_argument("--decode", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument("--gpu-index", type=int, default=0)
    parser.add_argument("--split-encode", choices=("auto", "disabled", "2", "3", "4"), default="auto")
    parser.add_argument("--status-file", default="")
    parser.add_argument("--delete-input-on-success", action="store_true")
    return parser


def settings_from_args(args: argparse.Namespace, *, media_codec: str, cuda_available: bool, split_value: str) -> ExportSettings:
    codec = 'hevc_nvenc' if args.codec == 'auto' else args.codec
    decode_mode = args.decode
    if decode_mode == "auto":
        decode_mode = "cpu" if media_codec.lower() in INTERMEDIATE_CPU_CODECS else ("cuda" if cuda_available else "cpu")
    if not codec.endswith('_nvenc'):
        decode_mode = 'cpu'
        split_value = 'disabled'
    return ExportSettings(
        codec=codec,
        quality=max(0, min(51, int(args.quality))),
        preset=args.preset,
        multipass="disabled" if args.preset in {"p1", "p2"} else ("qres" if args.preset == "p4" else "fullres"),
        split_encode=split_value,
        resolution=args.resolution,
        bit_depth=args.bit_depth,
        audio=args.audio,
        audio_bitrate=normalize_audio_bitrate(args.audio_bitrate) if args.audio == "aac" else args.audio_bitrate,
        decode_mode=decode_mode,
        gpu_index=max(0, int(args.gpu_index)),
        video_bitrate=normalize_video_bitrate(args.video_bitrate),
        maxrate=normalize_video_bitrate(args.maxrate),
    )


def _write_status(path: str, **payload: object) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {"updated_at": time.time(), **payload}
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def _safe_delete_resolve_intermediate(source: Path) -> bool:
    """Delete only connector-created temporary media after an explicit request."""
    marker = source.parent / ".okk_resolve_temp"
    if not marker.is_file() or not source.parent.name.startswith("OKK_Resolve_"):
        return False
    source.unlink(missing_ok=True)
    marker.unlink(missing_ok=True)
    try:
        source.parent.rmdir()
    except OSError:
        pass
    return True


def run_headless_cli(argv: Sequence[str], app_dir: str | Path) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv))
    source = Path(args.input).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    status_file = str(args.status_file or "")

    try:
        if not source.is_file():
            raise RuntimeError(f"找不到达芬奇中间母版：{source}")
        if source == output:
            raise RuntimeError("最终输出文件不能覆盖中间母版。")
        output.parent.mkdir(parents=True, exist_ok=True)
        _write_status(status_file, stage="detect", percent=0.0, message="正在检测 FFmpeg 与显卡编码能力…")

        caps = detect_capabilities(app_dir)
        if not caps.ffmpeg_path or not caps.ffprobe_path:
            raise RuntimeError("未找到 ffmpeg.exe 或 ffprobe.exe；请把它们放进软件 bin 文件夹。")
        if args.codec == 'auto':
            args.codec = preferred_codec(caps)
        if args.codec not in available_codecs(caps):
            raise RuntimeError(f"当前设备无法初始化 {args.codec}，请检查显卡驱动或选择其他编码器。")

        media = probe_media(caps.ffprobe_path, str(source))
        split_value = args.split_encode
        if split_value == "auto":
            if args.codec in caps.split_encode and caps.gpu_profile.nvenc_engines > 1:
                split_value = str(caps.gpu_profile.nvenc_engines)
            elif caps.gpu_profile.nvenc_engines <= 1:
                split_value = "disabled"
        settings = settings_from_args(
            args,
            media_codec=media.video_codec,
            cuda_available=caps.cuda_decode,
            split_value=split_value,
        )
        if settings.decode_mode == "cuda" and not caps.cuda_decode:
            raise RuntimeError("当前 FFmpeg 没有 CUDA 硬件解码支持。")

        command = build_encode_command(
            caps.ffmpeg_path, str(source), str(output), settings, media, caps,
        )
        _write_status(
            status_file,
            stage="encode",
            percent=0.0,
            message=f"正在使用 {codec_label(settings.codec)} 导出…",
            command=quote_command(command),
        )
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        progress: dict[str, str] = {}
        errors: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            pair = parse_progress_line(line)
            if pair:
                progress[pair[0]] = pair[1]
                if pair[0] == "progress":
                    seconds = progress_seconds(progress)
                    percent = min(100.0, seconds / media.duration * 100.0) if media.duration > 0 else 0.0
                    speed = progress.get("speed", "--x")
                    _write_status(
                        status_file,
                        stage="encode",
                        percent=percent,
                        speed=speed,
                        message=f"OKK NVENC 导出 {percent:.1f}% · {speed}",
                    )
            elif line.strip():
                errors.append(line.strip())
                errors = errors[-80:]
        return_code = process.wait()
        if return_code:
            raise RuntimeError("FFmpeg 导出失败：\n" + ("\n".join(errors[-30:]) or f"exit code {return_code}"))
        if not output.is_file() or output.stat().st_size <= 0:
            raise RuntimeError("FFmpeg 没有生成有效的最终输出文件。")

        deleted = False
        if args.delete_input_on_success:
            deleted = _safe_delete_resolve_intermediate(source)
        _write_status(
            status_file,
            stage="done",
            percent=100.0,
            message="OKK 导出完成",
            output=str(output),
            intermediate_deleted=deleted,
        )
        return 0
    except Exception as exc:
        message = str(exc)
        _write_status(status_file, stage="error", percent=0.0, message=message)
        print(message, file=sys.stderr)
        return 1


__all__ = ["build_parser", "run_headless_cli", "settings_from_args"]
