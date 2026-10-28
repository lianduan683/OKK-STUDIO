"""Lossless FFmpeg command construction and ffprobe metadata readers.

The main application owns process execution and progress handling.  Every
function in this module only *builds* a command list (or returns a plan that
contains command lists).  The only short-lived processes started here are
ffprobe metadata queries, because stream mapping and keyframe snapping need
the probe result before a command can be constructed.

The data model deliberately mirrors ffprobe's JSON payload: streams keep every
field, all Disposition flags are normalised to integers, chapters and format
tags are preserved, and the original dictionaries remain available through
``raw`` / ``to_dict()``.

``lossless_core.CutSegment`` instances (and compatible mappings/tuples) are
accepted by duck typing.  Selected ranges are exported; ``end=None`` markers
are ignored because they are not cut ranges.

Windows notes:
  * commands are ``list[str]`` and are passed to ``subprocess`` without a
    shell, so spaces and non-ASCII characters stay untouched;
  * ``ffconcat`` manifests use absolute forward-slash paths and quote the
    FFmpeg way (a single quote becomes ``'\\''``);
  * single quotes are legal in Windows file names but need that quote dance.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DEFAULT_SEGMENT_TEMPLATE = "{stem}_{index:03d}"
DEFAULT_SEGMENT_EXTENSION = ".mkv"

LOSSLESS_CONTAINERS = frozenset({
    ".mkv", ".mp4", ".mov", ".m4v", ".ts", ".m2ts", ".webm",
})
AUDIO_CONTAINERS = frozenset({
    ".mka", ".m4a", ".flac", ".wav", ".mp3", ".aac", ".opus", ".ogg",
})
REMUX_CONTAINERS = LOSSLESS_CONTAINERS | AUDIO_CONTAINERS | frozenset({".avi", ".flv"})

AUDIO_CONTAINER_CODECS: dict[str, frozenset[str] | None] = {
    ".mka": None,
    ".m4a": frozenset({"aac", "alac", "mp3", "ac3", "eac3", "opus", "flac"}),
    ".flac": frozenset({"flac"}),
    ".wav": frozenset({
        "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le",
        "pcm_u8", "pcm_alaw", "pcm_mulaw",
    }),
    ".mp3": frozenset({"mp3"}),
    ".aac": frozenset({"aac"}),
    ".opus": frozenset({"opus"}),
    ".ogg": frozenset({"vorbis", "opus"}),
}

MP4_CONTAINERS = frozenset({".mp4", ".mov", ".m4v"})
TS_CONTAINERS = frozenset({".ts", ".m2ts"})
MKV_CONTAINERS = frozenset({".mkv", ".mka"})
CHAPTER_CONTAINERS = frozenset({
    ".mkv", ".mka", ".mp4", ".mov", ".m4v", ".m4a", ".webm",
})

DISPOSITION_FLAGS = (
    "default", "dub", "original", "comment", "lyrics", "karaoke",
    "forced", "hearing_impaired", "visual_impaired", "clean_effects",
    "attached_pic", "timed_thumbnails", "captions", "descriptions",
    "metadata", "dependent", "still_image",
)

# Containers accept only these subtitle codecs when streams are copied.
MP4_SUBTITLE_CODECS = frozenset({
    "mov_text", "tx3g", "eia_608", "c608", "webvtt", "ttml",
})
TS_SUBTITLE_CODECS = frozenset({
    "dvb_subtitle", "dvb_teletext", "hdmv_pgs_subtitle", "aac_latm",
})
WEBM_VIDEO_CODECS = frozenset({"vp8", "vp9", "av1"})
WEBM_AUDIO_CODECS = frozenset({"opus", "vorbis"})

STREAM_TYPE_LETTERS = {
    "video": "v",
    "audio": "a",
    "subtitle": "s",
    "attachment": "t",
    "data": "d",
}
LETTER_STREAM_TYPES = {value: key for key, value in STREAM_TYPE_LETTERS.items()}


# ---------------------------------------------------------------------------
# small value helpers
# ---------------------------------------------------------------------------


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def _tag_value(tags: Mapping[str, str], key: str) -> str:
    if key in tags:
        return tags[key]
    lowered = key.lower()
    for name, value in tags.items():
        if str(name).lower() == lowered:
            return value
    return ""


def _seconds(value: float) -> str:
    return f"{float(value):.6f}"


def parse_frame_rate(value: Any) -> float:
    """Return a frame rate as float, accepting ``30000/1001`` and ``N/A``."""
    text = str(value or "").strip()
    if not text or text in {"0/0", "N/A"}:
        return 0.0
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            result = float(numerator) / float(denominator)
        else:
            result = float(text)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def normalize_frame_rate(fps: float | int | str) -> str:
    """Normalise an FPS value for the ``fps`` filter."""
    if isinstance(fps, str) and "/" in fps:
        numerator, denominator = fps.split("/", 1)
        try:
            num = float(numerator)
            den = float(denominator)
        except ValueError as exc:
            raise ValueError("帧率格式错误，请填写 30、29.97 或 30000/1001。") from exc
        if num <= 0 or den <= 0 or not math.isfinite(num) or not math.isfinite(den):
            raise ValueError("帧率必须大于零。")
        return fps.strip()
    try:
        value = float(fps)
    except (TypeError, ValueError) as exc:
        raise ValueError("帧率格式错误，请填写 30、29.97 或 30000/1001。") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("帧率必须大于零。")
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


# ---------------------------------------------------------------------------
# ffprobe JSON data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StreamInfo:
    index: int
    codec_type: str = ""
    codec_name: str = ""
    codec_long_name: str = ""
    profile: str = ""
    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    sample_fmt: str = ""
    sample_rate: int = 0
    channels: int = 0
    channel_layout: str = ""
    avg_frame_rate: str = "0/0"
    r_frame_rate: str = "0/0"
    time_base: str = ""
    start_time: float = 0.0
    duration: float = 0.0
    bit_rate: int = 0
    nb_frames: int = 0
    bits_per_raw_sample: int = 0
    bits_per_sample: int = 0
    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""
    color_range: str = ""
    sample_aspect_ratio: str = ""
    disposition: Mapping[str, int] = field(default_factory=dict)
    tags: Mapping[str, str] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StreamInfo":
        disposition_raw = payload.get("disposition") or {}
        disposition = {
            flag: _as_int(disposition_raw.get(flag, 0)) for flag in DISPOSITION_FLAGS
        }
        for key, value in disposition_raw.items():
            disposition.setdefault(str(key), _as_int(value))
        tags = {
            str(key): _as_text(value)
            for key, value in (payload.get("tags") or {}).items()
        }
        return cls(
            index=_as_int(payload.get("index")),
            codec_type=_as_text(payload.get("codec_type")),
            codec_name=_as_text(payload.get("codec_name")),
            codec_long_name=_as_text(payload.get("codec_long_name")),
            profile=_as_text(payload.get("profile")),
            width=_as_int(payload.get("width")),
            height=_as_int(payload.get("height")),
            pix_fmt=_as_text(payload.get("pix_fmt")),
            sample_fmt=_as_text(payload.get("sample_fmt")),
            sample_rate=_as_int(payload.get("sample_rate")),
            channels=_as_int(payload.get("channels")),
            channel_layout=_as_text(payload.get("channel_layout")),
            avg_frame_rate=_as_text(payload.get("avg_frame_rate")),
            r_frame_rate=_as_text(payload.get("r_frame_rate")),
            time_base=_as_text(payload.get("time_base")),
            start_time=_as_float(payload.get("start_time")),
            duration=_as_float(payload.get("duration")),
            bit_rate=_as_int(payload.get("bit_rate")),
            nb_frames=_as_int(payload.get("nb_frames")),
            bits_per_raw_sample=_as_int(payload.get("bits_per_raw_sample")),
            bits_per_sample=_as_int(payload.get("bits_per_sample")),
            color_primaries=_as_text(payload.get("color_primaries")),
            color_transfer=_as_text(payload.get("color_transfer")),
            color_space=_as_text(payload.get("color_space")),
            color_range=_as_text(payload.get("color_range")),
            sample_aspect_ratio=_as_text(payload.get("sample_aspect_ratio")),
            disposition=disposition,
            tags=tags,
            raw=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.raw) if self.raw else {
            "index": self.index,
            "codec_type": self.codec_type,
            "codec_name": self.codec_name,
            "disposition": dict(self.disposition),
            "tags": dict(self.tags),
        }

    @property
    def is_video(self) -> bool:
        return self.codec_type == "video"

    @property
    def is_audio(self) -> bool:
        return self.codec_type == "audio"

    @property
    def is_subtitle(self) -> bool:
        return self.codec_type == "subtitle"

    @property
    def is_attachment(self) -> bool:
        return self.codec_type == "attachment"

    @property
    def is_data(self) -> bool:
        return self.codec_type == "data"

    @property
    def is_attached_pic(self) -> bool:
        return bool(self.disposition.get("attached_pic", 0))

    @property
    def is_default(self) -> bool:
        return bool(self.disposition.get("default", 0))

    @property
    def is_forced(self) -> bool:
        return bool(self.disposition.get("forced", 0))

    @property
    def language(self) -> str:
        return _tag_value(self.tags, "language")

    @property
    def title(self) -> str:
        return _tag_value(self.tags, "title")

    @property
    def filename(self) -> str:
        return _tag_value(self.tags, "filename")

    @property
    def mimetype(self) -> str:
        return _tag_value(self.tags, "mimetype")

    @property
    def fps(self) -> float:
        return parse_frame_rate(self.avg_frame_rate) or parse_frame_rate(self.r_frame_rate)

    @property
    def letter(self) -> str:
        return STREAM_TYPE_LETTERS.get(self.codec_type, "?")

    @property
    def description(self) -> str:
        bits = [f"#{self.index}", self.codec_type or "unknown", self.codec_name or "?"]
        if self.language:
            bits.append(self.language)
        if self.title:
            bits.append(self.title)
        return " ".join(bits)


@dataclass(frozen=True, slots=True)
class ChapterInfo:
    index: int
    start: float
    end: float
    time_base: str = "1/1000"
    tags: Mapping[str, str] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ChapterInfo":
        tags = {
            str(key): _as_text(value)
            for key, value in (payload.get("tags") or {}).items()
        }
        return cls(
            index=_as_int(payload.get("id", payload.get("index"))),
            start=_as_float(payload.get("start_time")),
            end=_as_float(payload.get("end_time")),
            time_base=_as_text(payload.get("time_base")) or "1/1000",
            tags=tags,
            raw=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.raw) if self.raw else {
            "id": self.index,
            "start_time": self.start,
            "end_time": self.end,
            "time_base": self.time_base,
            "tags": dict(self.tags),
        }

    @property
    def id(self) -> int:
        return self.index

    @property
    def start_time(self) -> float:
        return self.start

    @property
    def end_time(self) -> float:
        return self.end

    @property
    def title(self) -> str:
        return _tag_value(self.tags, "title")

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True, slots=True)
class FormatInfo:
    filename: str = ""
    format_name: str = ""
    format_long_name: str = ""
    duration: float = 0.0
    size: int = 0
    bit_rate: int = 0
    tags: Mapping[str, str] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FormatInfo":
        tags = {
            str(key): _as_text(value)
            for key, value in (payload.get("tags") or {}).items()
        }
        return cls(
            filename=_as_text(payload.get("filename")),
            format_name=_as_text(payload.get("format_name")),
            format_long_name=_as_text(payload.get("format_long_name")),
            duration=_as_float(payload.get("duration")),
            size=_as_int(payload.get("size")),
            bit_rate=_as_int(payload.get("bit_rate")),
            tags=tags,
            raw=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.raw) if self.raw else {
            "filename": self.filename,
            "format_name": self.format_name,
            "duration": self.duration,
            "size": self.size,
            "bit_rate": self.bit_rate,
            "tags": dict(self.tags),
        }

    @property
    def title(self) -> str:
        return _tag_value(self.tags, "title")

    @property
    def timecode(self) -> str:
        return _tag_value(self.tags, "timecode")


@dataclass(frozen=True, slots=True)
class MediaProbe:
    path: str
    streams: tuple[StreamInfo, ...] = ()
    chapters: tuple[ChapterInfo, ...] = ()
    format: FormatInfo = field(default_factory=FormatInfo)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)
    # True when a sampled packet-timestamp probe found uneven frame spacing;
    # None means it was not measured (manually built probes, audio-only, HLS).
    vfr: bool | None = None

    @property
    def video_streams(self) -> tuple[StreamInfo, ...]:
        return tuple(stream for stream in self.streams if stream.is_video)

    @property
    def audio_streams(self) -> tuple[StreamInfo, ...]:
        return tuple(stream for stream in self.streams if stream.is_audio)

    @property
    def subtitle_streams(self) -> tuple[StreamInfo, ...]:
        return tuple(stream for stream in self.streams if stream.is_subtitle)

    @property
    def attachment_streams(self) -> tuple[StreamInfo, ...]:
        return tuple(stream for stream in self.streams if stream.is_attachment)

    @property
    def data_streams(self) -> tuple[StreamInfo, ...]:
        return tuple(stream for stream in self.streams if stream.is_data)

    @property
    def primary_video(self) -> StreamInfo | None:
        for stream in self.video_streams:
            if not stream.is_attached_pic:
                return stream
        return self.video_streams[0] if self.video_streams else None

    @property
    def primary_audio(self) -> StreamInfo | None:
        for stream in self.audio_streams:
            if stream.is_default:
                return stream
        return self.audio_streams[0] if self.audio_streams else None

    @property
    def duration(self) -> float:
        if self.format.duration > 0:
            return self.format.duration
        return max((stream.duration for stream in self.streams), default=0.0)

    @property
    def format_tags(self) -> Mapping[str, str]:
        return self.format.tags

    @property
    def title(self) -> str:
        return self.format.title

    @property
    def is_hls(self) -> bool:
        suffix = Path(self.path).suffix.lower()
        return suffix in {".m3u8", ".m3u"} or "://" in self.path and ".m3u8" in self.path

    def to_dict(self) -> dict[str, Any]:
        return dict(self.raw) if self.raw else {
            "streams": [stream.to_dict() for stream in self.streams],
            "chapters": [chapter.to_dict() for chapter in self.chapters],
            "format": self.format.to_dict(),
        }


def stream_is_cfr(stream: StreamInfo) -> bool:
    """Return True only when *stream* looks like a single constant rate video.

    ffprobe reports the average and the guessed base frame rate; for VFR media
    they differ.  Both must be known and equal, and the frame count/duration
    cross-check (when available) must agree as well.  Unknown rates stay
    ``False`` so callers never trim packets they cannot predict.
    """
    average = parse_frame_rate(stream.avg_frame_rate)
    real = parse_frame_rate(stream.r_frame_rate)
    if average <= 0 or real <= 0:
        return False
    tolerance = max(0.001, max(average, real) * 0.0005)
    if abs(average - real) > tolerance:
        return False
    if stream.nb_frames > 0 and stream.duration > 0:
        measured = stream.nb_frames / stream.duration
        if abs(measured - average) > max(0.01, average * 0.001):
            return False
    return True


def media_is_cfr(probe: MediaProbe | None) -> bool:
    """True when *probe* has exactly one real video stream at constant rate."""
    if probe is None:
        return False
    vfr = getattr(probe, "vfr", None)
    if vfr:
        return False
    videos = [
        stream for stream in probe.streams
        if stream.is_video and not stream.is_attached_pic
    ]
    if len(videos) != 1:
        return False
    stream = videos[0]
    if vfr is False:
        # Packet timestamps were sampled and looked constant; a usable frame
        # rate is all that is needed for the frame cap.
        return stream.fps > 0
    return stream_is_cfr(stream)


def parse_probe_json(path: str, payload: Mapping[str, Any] | str) -> MediaProbe:
    """Build a :class:`MediaProbe` from an ffprobe JSON payload."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("ffprobe 返回的 JSON 无法解析。") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("ffprobe 返回的数据不是 JSON 对象。")
    streams = tuple(
        StreamInfo.from_dict(item)
        for item in (payload.get("streams") or [])
        if isinstance(item, Mapping)
    )
    chapters = tuple(
        ChapterInfo.from_dict(item)
        for item in (payload.get("chapters") or [])
        if isinstance(item, Mapping)
    )
    format_payload = payload.get("format")
    format_info = (
        FormatInfo.from_dict(format_payload)
        if isinstance(format_payload, Mapping)
        else FormatInfo()
    )
    return MediaProbe(
        path=str(path),
        streams=streams,
        chapters=chapters,
        format=format_info,
        raw=dict(payload),
    )


def build_probe_command(ffprobe: str, path: str) -> list[str]:
    return [
        ffprobe, "-v", "error",
        "-show_format", "-show_streams", "-show_chapters",
        "-of", "json", str(path),
    ]


def _run_text(args: Sequence[str], timeout: float = 30.0) -> tuple[int, str]:
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


def probe_vfr(
    ffprobe: str,
    path: str,
    *,
    video_stream: int = 0,
    sample_seconds: float = 2.0,
) -> bool | None:
    """Sample video packet timestamps and report uneven frame spacing.

    MKV often reports identical ``avg_frame_rate``/``r_frame_rate`` even for
    variable-rate files, so the JSON probe alone is not enough.  Reading the
    first couple of seconds of packet timestamps is cheap (demux only) and
    gives a reliable CFR/VFR answer.  ``None`` means the sample was unusable.
    """
    command = [
        ffprobe, "-v", "error", "-select_streams", f"v:{int(video_stream)}",
        "-read_intervals", f"%+{float(sample_seconds):g}",
        "-show_packets", "-show_entries", "packet=pts_time",
        "-of", "csv=p=0", str(path),
    ]
    code, output = _run_text(command, timeout=30.0)
    if code:
        return None
    times: list[float] = []
    for line in output.splitlines():
        token = line.strip().strip(",")
        if not token:
            continue
        try:
            times.append(float(token))
        except ValueError:
            continue
    times.sort()
    gaps = [right - left for left, right in zip(times, times[1:]) if right > left]
    if len(gaps) < 3:
        return None
    ordered = sorted(gaps)
    median = ordered[len(ordered) // 2]
    if median <= 0:
        return None
    tolerance = max(0.002, median * 0.05)
    return any(abs(gap - median) > tolerance for gap in gaps)


def find_binary(name: str, app_dir: str | Path | None = None) -> str | None:
    suffix = ".exe" if os.name == "nt" else ""
    roots: list[Path] = []
    if app_dir:
        roots.append(Path(app_dir))
    roots.append(Path(__file__).resolve().parent)
    for root in roots:
        for candidate in (root / "bin" / f"{name}{suffix}", root / f"{name}{suffix}"):
            if candidate.is_file():
                return str(candidate.resolve())
    return shutil.which(name)


def probe_media(ffprobe: str, path: str) -> MediaProbe:
    """Read streams/chapters/format tags plus a sampled CFR-vs-VFR hint."""
    code, output = _run_text(build_probe_command(ffprobe, path), timeout=60.0)
    if code:
        raise RuntimeError(f"ffprobe 无法读取媒体信息：\n{output.strip()[-1600:]}")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe 返回了无效的 JSON。") from exc
    probe = parse_probe_json(path, payload)
    videos = [
        stream for stream in probe.streams
        if stream.is_video and not stream.is_attached_pic
    ]
    if (
        len(videos) == 1
        and not probe.is_hls
        and stream_is_cfr(videos[0])
    ):
        vfr = probe_vfr(ffprobe, path)
        if vfr is not None:
            probe = replace(probe, vfr=vfr)
    return probe


# ---------------------------------------------------------------------------
# keyframes / lossless cut limits
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Keyframe:
    time: float
    pts: int | None = None
    frame_type: str = "I"


@dataclass(frozen=True, slots=True)
class KeyframeIndex:
    stream_index: int = 0
    frames: tuple[Keyframe, ...] = ()

    def __post_init__(self) -> None:
        frames = tuple(
            sorted(
                (
                    frame if isinstance(frame, Keyframe) else Keyframe(float(frame))
                    for frame in self.frames
                    if math.isfinite(float(frame.time if isinstance(frame, Keyframe) else frame))
                ),
                key=lambda frame: frame.time,
            )
        )
        deduped: list[Keyframe] = []
        for frame in frames:
            if deduped and abs(frame.time - deduped[-1].time) < 1e-9:
                continue
            deduped.append(frame)
        object.__setattr__(self, "frames", tuple(deduped))

    @property
    def times(self) -> tuple[float, ...]:
        return tuple(frame.time for frame in self.frames)

    @property
    def max_interval(self) -> float:
        times = self.times
        if len(times) < 2:
            return 0.0
        return max(times[index + 1] - times[index] for index in range(len(times) - 1))

    @property
    def average_interval(self) -> float:
        times = self.times
        if len(times) < 2:
            return 0.0
        return (times[-1] - times[0]) / (len(times) - 1)

    def before(self, seconds: float, tolerance: float = 0.0) -> Keyframe | None:
        selected: Keyframe | None = None
        for frame in self.frames:
            if frame.time <= seconds + tolerance:
                selected = frame
            else:
                break
        return selected

    def after(self, seconds: float, tolerance: float = 0.0) -> Keyframe | None:
        for frame in self.frames:
            if frame.time >= seconds - tolerance:
                return frame
        return None

    def is_exact(self, seconds: float, tolerance: float = 0.001) -> bool:
        frame = self.before(seconds, tolerance)
        return frame is not None and abs(frame.time - seconds) <= tolerance

    def snap(self, seconds: float, tolerance: float = 0.001) -> float:
        previous = self.before(seconds, tolerance)
        if previous is not None:
            return previous.time
        following = self.after(seconds, tolerance)
        return following.time if following is not None else float(seconds)


@dataclass(frozen=True, slots=True)
class CutRange:
    requested_start: float
    start: float
    end: float
    snapped: bool = False
    max_keyframe_interval: float = 0.0
    warning: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def build_keyframe_probe_command(
    ffprobe: str, path: str, video_stream: int = 0,
) -> list[str]:
    if isinstance(video_stream, bool) or not isinstance(video_stream, int) or video_stream < 0:
        raise ValueError("视频流序号必须是从 0 开始的整数。")
    return [
        ffprobe, "-v", "error",
        "-select_streams", f"v:{video_stream}",
        "-skip_frame", "nokey",
        "-show_frames",
        "-show_entries",
        "frame=best_effort_timestamp_time,pts_time,pkt_pts_time,pkt_dts_time,"
        "key_frame,pict_type",
        "-of", "json", str(path),
    ]


def parse_keyframes(
    payload: Mapping[str, Any] | str, stream_index: int = 0,
) -> KeyframeIndex:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("ffprobe 关键帧 JSON 无法解析。") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("ffprobe 关键帧数据不是 JSON 对象。")
    frames: list[Keyframe] = []
    for item in payload.get("frames") or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("key_frame", 1)) not in {"1", "True", "true"}:
            continue
        timestamp = 0.0
        for key in (
            "best_effort_timestamp_time", "pts_time",
            "pkt_pts_time", "pkt_dts_time",
        ):
            value = item.get(key)
            if value in (None, "", "N/A"):
                continue
            timestamp = _as_float(value)
            break
        if not math.isfinite(timestamp):
            continue
        frames.append(
            Keyframe(
                time=timestamp,
                pts=_as_int(item.get("pts")) or None,
                frame_type=_as_text(item.get("pict_type")) or "I",
            )
        )
    return KeyframeIndex(stream_index=stream_index, frames=tuple(frames))


def probe_keyframes(ffprobe: str, path: str, video_stream: int = 0) -> KeyframeIndex:
    code, output = _run_text(
        build_keyframe_probe_command(ffprobe, path, video_stream),
        timeout=120.0,
    )
    if code:
        raise RuntimeError(f"ffprobe 无法读取关键帧：\n{output.strip()[-1600:]}")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe 返回了无效的关键帧 JSON。") from exc
    return parse_keyframes(payload, stream_index=video_stream)


def _keyframe_times(
    keyframes: KeyframeIndex | Iterable[Keyframe] | Iterable[float] | None,
) -> tuple[float, ...]:
    if keyframes is None:
        return ()
    if isinstance(keyframes, KeyframeIndex):
        return keyframes.times
    times: list[float] = []
    for item in keyframes:
        value = item.time if isinstance(item, Keyframe) else float(item)
        if math.isfinite(value) and value >= 0:
            times.append(value)
    return tuple(sorted(set(times)))


def materialize_keyframes(
    keyframes: KeyframeIndex | Iterable[Keyframe] | Iterable[float] | None,
) -> KeyframeIndex | None:
    """Freeze an iterable of keyframes so several commands can reuse it."""
    if keyframes is None or isinstance(keyframes, KeyframeIndex):
        return keyframes
    return KeyframeIndex(
        stream_index=0,
        frames=tuple(Keyframe(time=value) for value in _keyframe_times(keyframes)),
    )


def _validate_cut(start: float, end: float) -> tuple[float, float]:
    start = float(start)
    end = float(end)
    if not math.isfinite(start) or not math.isfinite(end):
        raise ValueError("片段的起点和终点必须是有限数值。")
    if start < 0:
        raise ValueError("片段起点不能小于 0。")
    if end <= start:
        raise ValueError("片段终点必须晚于起点。")
    return start, end


def resolve_cut_range(
    start: float,
    end: float,
    keyframes: KeyframeIndex | Iterable[Keyframe] | Iterable[float] | None = None,
    *,
    tolerance: float = 0.001,
) -> CutRange:
    """Snap a cut start to the previous keyframe and explain the limit.

    With ``-c copy`` FFmpeg can only start decoding at a keyframe.  The end
    stays where the caller asked; only the start is snapped, so a lossless cut
    may include up to one GOP more than requested.
    """
    start, end = _validate_cut(start, end)
    tolerance = max(0.0, float(tolerance))
    times = _keyframe_times(keyframes)
    if not times:
        return CutRange(start, start, end)
    max_interval = max(
        (times[index + 1] - times[index] for index in range(len(times) - 1)),
        default=0.0,
    )
    exact = next((time for time in times if abs(time - start) <= tolerance), None)
    if exact is not None:
        return CutRange(start, exact, end, max_keyframe_interval=max_interval)
    previous: float | None = None
    following: float | None = None
    for time in times:
        if time < start:
            previous = time
        elif time > start:
            following = time
            break
    if previous is not None:
        snapped = previous
        direction = "提前"
        delta = start - previous
    elif following is not None:
        snapped = following
        direction = "后移"
        delta = following - start
    else:  # pragma: no cover - defensive; times is non-empty
        return CutRange(start, start, end, max_keyframe_interval=max_interval)
    if snapped >= end:
        raise ValueError(
            f"起点吸附到关键帧 {snapped:.3f}s 后已不早于终点 {end:.3f}s，"
            "请延长片段或改用重编码裁切。"
        )
    warning = (
        f"起点 {start:.3f}s 不是关键帧；无损流复制只能从关键帧（I 帧）开始，"
        f"已{direction}到 {snapped:.3f}s（相差 {delta:.3f}s，"
        f"源最大关键帧间隔约 {max_interval:.3f}s）。"
    )
    return CutRange(
        requested_start=start,
        start=snapped,
        end=end,
        snapped=True,
        max_keyframe_interval=max_interval,
        warning=warning,
    )


# ---------------------------------------------------------------------------
# CutSegment duck typing (lossless_core.CutSegment compatible)
# ---------------------------------------------------------------------------


def segment_is_marker(segment: Any) -> bool:
    """``lossless_core.CutSegment(end=None)`` is a marker, not a cut range."""
    if isinstance(segment, Mapping):
        return segment.get("end") is None
    if hasattr(segment, "end"):
        return getattr(segment, "end") is None
    if isinstance(segment, Sequence) and not isinstance(segment, (str, bytes)):
        return len(segment) < 2 or segment[1] is None
    return True


def cut_segment_range(segment: Any) -> tuple[float, float]:
    if isinstance(segment, Mapping):
        start = segment.get("start")
        end = segment.get("end")
    elif hasattr(segment, "start") and hasattr(segment, "end"):
        start = getattr(segment, "start")
        end = getattr(segment, "end")
    elif isinstance(segment, Sequence) and not isinstance(segment, (str, bytes)) and len(segment) >= 2:
        start, end = segment[0], segment[1]
    else:
        raise TypeError("片段必须提供 start 和 end（lossless_core.CutSegment 或二元组）。")
    if end is None:
        raise ValueError("标记点（end 为空）不是可导出的裁切片段。")
    return _validate_cut(start, end)


def segment_is_selected(segment: Any) -> bool:
    if isinstance(segment, Mapping):
        value = segment.get("selected", True)
    else:
        value = getattr(segment, "selected", True)
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "否"}
    return bool(value)


def selected_segments(
    segments: Iterable[Any], *, respect_selection: bool = True,
) -> tuple[Any, ...]:
    return tuple(
        segment for segment in segments
        if not segment_is_marker(segment)
        and (not respect_selection or segment_is_selected(segment))
    )


def segment_name(segment: Any, index: int = 1) -> str:
    if isinstance(segment, Mapping):
        name = segment.get("name")
    else:
        name = getattr(segment, "name", "")
    text = str(name or "").strip()
    return text or f"片段{index:02d}"


def segment_tags(segment: Any) -> dict[str, str]:
    if isinstance(segment, Mapping):
        tags = segment.get("tags") or {}
    else:
        tags = getattr(segment, "tags", {}) or {}
    if not isinstance(tags, Mapping):
        return {}
    return {str(key): _as_text(value) for key, value in tags.items()}


# ---------------------------------------------------------------------------
# output names / overwrite protection
# ---------------------------------------------------------------------------


def format_filename_time(seconds: float) -> str:
    value = float(seconds)
    if not math.isfinite(value):
        raise ValueError("时间必须是有限数值。")
    sign = "-" if value < 0 else ""
    total_ms = int(round(abs(value) * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{sign}{hours:02d}-{minutes:02d}-{secs:02d}-{millis:03d}"


_WINDOWS_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def sanitize_filename(name: str) -> str:
    """Make a single Windows-safe file name; non-ASCII text is preserved."""
    text = _WINDOWS_INVALID.sub("_", str(name))
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text or text in {".", ".."}:
        text = "未命名"
    path = Path(text)
    stem = path.stem or text
    suffix = path.suffix
    if suffix and stem.upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    if not suffix and text.upper() in _WINDOWS_RESERVED:
        stem = f"_{text}"
        suffix = ""
    if len(stem) > 180:
        stem = stem[:180].rstrip(" .")
    return f"{stem}{suffix}"


class _TagProxy:
    def __init__(self, tags: Mapping[str, str]) -> None:
        self._tags = tags

    def __getattr__(self, key: str) -> str:
        return self._tags.get(key, "")


class _NameFields(dict[str, Any]):
    def __missing__(self, key: str) -> Any:
        raise ValueError(f"未知的命名占位符：{{{key}}}")


def render_output_name(
    template: str,
    segment: Any,
    index: int,
    source: str,
    extension: str = DEFAULT_SEGMENT_EXTENSION,
) -> str:
    """Render a per-segment output name.

    Supported placeholders: ``{name}``, ``{index}`` (1-based), ``{start}``,
    ``{end}``, ``{duration}``, ``{stem}``, ``{source}``, ``{ext}``,
    ``{tags[KEY]}`` and ``{tag.KEY}``.  Time placeholders use the
    Windows-safe ``HH-MM-SS-mmm`` form.
    """
    if not isinstance(template, str) or not template.strip():
        raise ValueError("输出命名模板不能为空。")
    if index < 1:
        raise ValueError("片段序号必须从 1 开始。")
    start, end = cut_segment_range(segment)
    tags = segment_tags(segment)
    ext = extension if extension.startswith(".") else f".{extension}" if extension else ""
    fields = _NameFields(
        name=segment_name(segment, index),
        index=int(index),
        start=format_filename_time(start),
        end=format_filename_time(end),
        duration=format_filename_time(end - start),
        stem=Path(source).stem,
        source=Path(source).name,
        ext=ext,
        tags=tags,
        tag=_TagProxy(tags),
    )
    try:
        rendered = template.format_map(fields)
    except (KeyError, AttributeError, IndexError) as exc:
        raise ValueError(f"命名模板中的占位符无效：{exc}") from exc
    safe = sanitize_filename(rendered)
    if not Path(safe).suffix:
        safe = f"{safe}{ext}"
    return safe


def normalize_extension(extension: str) -> str:
    text = str(extension or "").strip().lower()
    if not text:
        return DEFAULT_SEGMENT_EXTENSION
    if not text.startswith("."):
        text = f".{text}"
    return text


def container_supports_chapters(output: str | Path) -> bool:
    return Path(str(output)).suffix.lower() in CHAPTER_CONTAINERS


def _ensure_output_extension(
    output: str,
    default_extension: str,
    allowed: Iterable[str],
) -> str:
    path = Path(str(output))
    suffix = path.suffix.lower()
    allowed_set = {item.lower() for item in allowed}
    if not suffix:
        suffix = normalize_extension(default_extension)
        if suffix not in allowed_set:
            raise ValueError(f"不支持输出扩展名：{suffix}")
        path = path.with_name(f"{path.name}{suffix}")
    elif suffix not in allowed_set:
        raise ValueError(f"不支持输出扩展名：{suffix}")
    return str(path)


def check_output_available(path: str | Path, overwrite: bool = False) -> Path:
    target = Path(path)
    if target.is_dir():
        raise ValueError(f"输出路径是文件夹，不能作为文件写入：{target}")
    if target.exists() and not overwrite:
        raise FileExistsError(
            f"输出文件已存在：{target}；如需覆盖请启用覆盖选项。"
        )
    return target


def check_outputs_available(
    paths: Iterable[str | Path], overwrite: bool = False,
) -> tuple[Path, ...]:
    result = tuple(check_output_available(path, overwrite) for path in paths)
    seen: dict[str, Path] = {}
    for path in result:
        key = os.path.normcase(str(path))
        if key in seen:
            raise ValueError(f"输出文件名重复：{seen[key]} / {path}")
        seen[key] = path
    return result


def _output_identity(path: str | Path) -> str:
    return os.path.normcase(str(Path(str(path)).resolve(strict=False)))


def _reject_duplicate_final_outputs(
    segment_outputs: Sequence[str],
    merged_output: str | None,
    part_outputs: Sequence[str] = (),
) -> None:
    """Reject plans whose merged file collides with a segment/part file."""
    if not merged_output:
        return
    merged_key = _output_identity(merged_output)
    for path in (*segment_outputs, *part_outputs):
        if _output_identity(path) == merged_key:
            raise ValueError(
                f"合并输出与分段输出路径重复：{path}；请修改合并文件名或输出目录。"
            )


def plan_segment_outputs(
    source: str,
    segments: Sequence[Any],
    output_dir: str | Path,
    *,
    template: str = DEFAULT_SEGMENT_TEMPLATE,
    extension: str = DEFAULT_SEGMENT_EXTENSION,
    overwrite: bool = False,
    respect_selection: bool = True,
) -> tuple[str, ...]:
    """Render output paths for selected segments and reject collisions."""
    ext = normalize_extension(extension)
    if ext not in LOSSLESS_CONTAINERS:
        raise ValueError(f"不支持片段输出扩展名：{ext}")
    items = selected_segments(segments, respect_selection=respect_selection)
    if not items:
        raise ValueError("没有选中的片段。")
    folder = Path(output_dir)
    paths: list[str] = []
    for index, segment in enumerate(items, start=1):
        name = render_output_name(template, segment, index, source, ext)
        suffix = Path(name).suffix.lower()
        if suffix not in LOSSLESS_CONTAINERS:
            raise ValueError(f"命名模板产生了不支持的扩展名：{name}")
        paths.append(str(folder / name))
    check_outputs_available(paths, overwrite=overwrite)
    return tuple(paths)


# ---------------------------------------------------------------------------
# track selection / suitability
# ---------------------------------------------------------------------------


def _normalize_selector(value: Any, field_name: str) -> bool | tuple[int, ...]:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{field_name} 的轨道序号不能为负数。")
        return (value,)
    try:
        items = tuple(int(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是布尔值或轨道序号列表。") from exc
    if any(item < 0 for item in items):
        raise ValueError(f"{field_name} 的轨道序号不能为负数。")
    return items


@dataclass(frozen=True, slots=True)
class TrackSelection:
    """Per-type track selection using absolute input stream indices."""

    video: bool | Sequence[int] = True
    audio: bool | Sequence[int] = True
    subtitle: bool | Sequence[int] = True
    attachment: bool | Sequence[int] = True
    data: bool | Sequence[int] = True
    include_attached_pic: bool = False

    def __post_init__(self) -> None:
        for name in ("video", "audio", "subtitle", "attachment", "data"):
            object.__setattr__(
                self, name, _normalize_selector(getattr(self, name), name),
            )

    @classmethod
    def all(cls) -> "TrackSelection":
        return cls()

    @classmethod
    def video_only(cls) -> "TrackSelection":
        return cls(video=True, audio=False, subtitle=False, attachment=False, data=False)

    def selector(self, kind: str) -> bool | tuple[int, ...]:
        return getattr(self, kind)


def stream_suitable(
    stream: StreamInfo,
    extension: str,
    *,
    include_attached_pic: bool = False,
) -> bool:
    """Whether stream copy into *extension* is expected to be muxable."""
    text = str(extension or "")
    suffix = Path(text).suffix.lower()
    ext = suffix or (
        text.lower() if text.lower().startswith(".") else normalize_extension(text)
    )
    if ext in AUDIO_CONTAINERS:
        if not stream.is_audio:
            return False
        allowed = AUDIO_CONTAINER_CODECS.get(ext)
        return allowed is None or stream.codec_name.lower() in allowed
    if stream.is_video:
        if stream.is_attached_pic and not include_attached_pic:
            return False
        if ext == ".webm":
            return stream.codec_name.lower() in WEBM_VIDEO_CODECS
        return True
    if stream.is_audio:
        if ext == ".webm":
            return stream.codec_name.lower() in WEBM_AUDIO_CODECS
        return True
    if stream.is_subtitle:
        codec = stream.codec_name.lower()
        if ext in MKV_CONTAINERS:
            return True
        if ext in MP4_CONTAINERS:
            return codec in MP4_SUBTITLE_CODECS
        if ext in TS_CONTAINERS:
            return codec in TS_SUBTITLE_CODECS
        if ext == ".webm":
            return codec == "webvtt"
        return False
    if stream.is_attachment:
        return ext in MKV_CONTAINERS
    if stream.is_data:
        if ext in MKV_CONTAINERS:
            return True
        if ext in MP4_CONTAINERS:
            return stream.codec_name.lower() in {"tmcd", "c608", "c708"}
        return False
    return False


@dataclass(frozen=True, slots=True)
class StreamMapPlan:
    args: tuple[str, ...]
    streams: tuple[StreamInfo, ...]
    skipped: tuple[StreamInfo, ...] = ()
    warnings: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.args)


def _selection_indices(
    selection: TrackSelection,
    probe: MediaProbe,
    extension: str,
    *,
    include_attached_pic: bool,
) -> tuple[set[int], dict[int, str]]:
    """Return selected indices and warnings for explicit/unsuitable streams."""
    indices: set[int] = set()
    warnings: dict[int, str] = {}
    for kind in ("video", "audio", "subtitle", "attachment", "data"):
        selector = selection.selector(kind)
        if selector is False:
            continue
        candidates = [
            stream for stream in probe.streams if stream.codec_type == kind
        ]
        if selector is True:
            chosen = [
                stream for stream in candidates
                if stream_suitable(
                    stream, extension,
                    include_attached_pic=include_attached_pic
                    or selection.include_attached_pic,
                )
            ]
        else:
            by_index = {stream.index: stream for stream in candidates}
            chosen = []
            for index in selector:
                stream = by_index.get(index)
                if stream is None:
                    raise ValueError(
                        f"轨道 #{index} 不是 {kind} 流或不存在。"
                    )
                chosen.append(stream)
                if not stream_suitable(
                    stream, extension,
                    include_attached_pic=include_attached_pic
                    or selection.include_attached_pic,
                ):
                    warnings[index] = (
                        f"流 #{stream.index}（{stream.codec_type}/{stream.codec_name}）"
                        f"按选择映射到 {extension}，封装可能失败。"
                    )
        for stream in chosen:
            indices.add(stream.index)
    return indices, warnings


def plan_stream_maps(
    probe: MediaProbe,
    output: str,
    selection: TrackSelection | None = None,
    *,
    include_attached_pic: bool = False,
) -> StreamMapPlan:
    """Map every suitable stream, or exactly the explicitly selected tracks."""
    ext = Path(str(output)).suffix.lower() or DEFAULT_SEGMENT_EXTENSION
    include_pic = include_attached_pic or bool(
        selection and selection.include_attached_pic
    )
    warnings: list[str] = []
    if selection is None:
        chosen = tuple(
            stream for stream in probe.streams
            if stream_suitable(stream, ext, include_attached_pic=include_pic)
        )
        skipped = tuple(stream for stream in probe.streams if stream not in chosen)
        for stream in skipped:
            if stream.codec_type in STREAM_TYPE_LETTERS:
                reason = "封面图" if stream.is_attached_pic else "不适合该封装"
                warnings.append(
                    f"流 #{stream.index}（{stream.codec_type}/{stream.codec_name}）"
                    f"{reason}，已从 {ext} 输出中跳过。"
                )
    else:
        indices, reasons = _selection_indices(
            selection, probe, ext, include_attached_pic=include_pic,
        )
        chosen = tuple(
            stream for stream in probe.streams if stream.index in indices
        )
        skipped = tuple(stream for stream in probe.streams if stream not in chosen)
        warnings.extend(reasons[stream.index] for stream in chosen if stream.index in reasons)
    if not chosen:
        raise ValueError(
            f"没有可映射到 {ext} 的轨道；请更换封装或手动选择轨道。"
        )
    args: list[str] = []
    for stream in chosen:
        args += ["-map", f"0:{stream.index}"]
    return StreamMapPlan(
        args=tuple(args),
        streams=chosen,
        skipped=skipped,
        warnings=tuple(warnings),
    )


def generic_map_args(
    output: str,
    selection: TrackSelection | None = None,
) -> list[str]:
    """Map args used when no probe is available."""
    ext = Path(str(output)).suffix.lower()
    if selection is None:
        if ext in AUDIO_CONTAINERS:
            letters = ["a"]
        else:
            letters = ["v", "a"]
            if ext in MKV_CONTAINERS:
                letters += ["s", "t", "d"]
            elif ext in MP4_CONTAINERS:
                letters += ["s", "d"]
            elif ext in TS_CONTAINERS:
                letters += ["s"]
        args: list[str] = []
        for letter in letters:
            args += ["-map", f"0:{letter}?"]
        return args
    args = []
    for kind, letter in STREAM_TYPE_LETTERS.items():
        selector = selection.selector(kind)
        if selector is False:
            continue
        if selector is True:
            args += ["-map", f"0:{letter}?"]
        else:
            for index in selector:
                args += ["-map", f"0:{index}"]
    if not args:
        raise ValueError("没有选择任何轨道。")
    return args


# ---------------------------------------------------------------------------
# metadata overrides
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetadataOptions:
    title: str | None = None
    language: str | None = None
    rotate: int | None = None
    hvc1: bool = False
    global_tags: Mapping[str, str] = field(default_factory=dict)


def _normalise_rotation(rotate: int) -> int:
    if isinstance(rotate, bool) or not isinstance(rotate, int):
        raise ValueError("旋转角度必须是 0、90、180 或 270。")
    angle = rotate % 360
    if angle not in {0, 90, 180, 270}:
        raise ValueError("旋转角度必须是 0、90、180 或 270。")
    return angle


def build_metadata_args(
    probe: MediaProbe | None,
    output: str,
    options: MetadataOptions | None,
    *,
    mapped_streams: Sequence[StreamInfo] | None = None,
) -> list[str]:
    """Build title/language/rotate/hvc1 and arbitrary global tag arguments."""
    if options is None:
        return []
    mapped_streams = tuple(mapped_streams) if mapped_streams is not None else None
    args: list[str] = []
    if options.title is not None:
        args += ["-metadata", f"title={options.title}"]
    for key, value in options.global_tags.items():
        key_text = str(key).strip()
        if not key_text or "=" in key_text or "\n" in key_text or "\r" in key_text:
            raise ValueError(f"无效的元数据键：{key!r}")
        args += ["-metadata", f"{key_text}={value}"]
    if options.language is not None:
        language = str(options.language).strip()
        if not language:
            raise ValueError("语言代码不能为空。")
        streams = tuple(mapped_streams) if mapped_streams is not None else (
            probe.streams if probe is not None else ()
        )
        if streams:
            counters = {"audio": 0, "subtitle": 0}
            for stream in streams:
                if stream.codec_type not in counters:
                    continue
                index = counters[stream.codec_type]
                counters[stream.codec_type] += 1
                args += [
                    f"-metadata:s:{stream.letter}:{index}",
                    f"language={language}",
                ]
        else:
            args += [
                "-metadata:s:a", f"language={language}",
                "-metadata:s:s", f"language={language}",
            ]
    if options.rotate is not None:
        angle = _normalise_rotation(options.rotate)
        args += ["-metadata:s:v:0", f"rotate={angle}"]
    if options.hvc1 and Path(str(output)).suffix.lower() in MP4_CONTAINERS:
        if mapped_streams is not None:
            video = next(
                (
                    stream for stream in mapped_streams
                    if stream.is_video and not stream.is_attached_pic
                ),
                None,
            )
            applies = video is not None and video.codec_name.lower() in {"hevc", "h265"}
        elif probe is not None:
            video = probe.primary_video
            applies = video is not None and video.codec_name.lower() in {"hevc", "h265"}
        else:
            applies = True
        if applies:
            args += ["-tag:v:0", "hvc1"]
    return args


# ---------------------------------------------------------------------------
# command headers
# ---------------------------------------------------------------------------


def build_ffmpeg_base(
    ffmpeg: str,
    *,
    overwrite: bool = False,
    progress: bool = True,
    loglevel: str = "warning",
) -> list[str]:
    command = [
        ffmpeg, "-hide_banner", "-nostdin",
        "-y" if overwrite else "-n",
        "-loglevel", loglevel,
    ]
    if progress:
        command += ["-stats_period", "0.25", "-progress", "pipe:1", "-nostats"]
    return command


def _append_mux_flags(command: list[str], output: str) -> None:
    if Path(str(output)).suffix.lower() in MP4_CONTAINERS:
        command += ["-movflags", "+faststart"]


def _append_extra(command: list[str], extra_args: Sequence[str] | None) -> None:
    if extra_args:
        command += [str(argument) for argument in extra_args]


# ---------------------------------------------------------------------------
# single segment / separate segments / ffconcat merge
# ---------------------------------------------------------------------------


def build_segment_copy_command(
    ffmpeg: str,
    source: str,
    segment: Any,
    output: str,
    *,
    probe: MediaProbe | None = None,
    keyframes: KeyframeIndex | Iterable[Keyframe] | Iterable[float] | None = None,
    selection: TrackSelection | None = None,
    metadata: MetadataOptions | None = None,
    overwrite: bool = False,
    snap_start: bool = True,
    copy_chapters: bool = False,
    include_attached_pic: bool = False,
    progress: bool = True,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Stream-copy one CutSegment (start snapped to the previous keyframe)."""
    keyframes = materialize_keyframes(keyframes)
    output = _ensure_output_extension(
        output, DEFAULT_SEGMENT_EXTENSION, LOSSLESS_CONTAINERS,
    )
    check_output_available(output, overwrite=overwrite)
    start, end = cut_segment_range(segment)
    cut = (
        resolve_cut_range(start, end, keyframes)
        if snap_start
        else CutRange(start, start, end)
    )
    if probe is not None:
        map_plan = plan_stream_maps(
            probe, output, selection,
            include_attached_pic=include_attached_pic,
        )
        map_args = list(map_plan.args)
        mapped = map_plan.streams
    else:
        map_args = generic_map_args(output, selection)
        mapped = ()
    command = build_ffmpeg_base(ffmpeg, overwrite=overwrite, progress=progress)
    command += [
        "-ss", _seconds(cut.start), "-i", str(source),
        "-t", _seconds(cut.duration),
    ]
    video_selected = selection is None or selection.video is not False
    if video_selected and cut.duration > 0 and media_is_cfr(probe):
        video = next(
            (
                stream for stream in mapped
                if stream.is_video and not stream.is_attached_pic
            ),
            None,
        )
        if video is None:
            video = probe.primary_video if probe is not None else None
        fps = video.fps if video is not None else 0.0
        if fps > 0:
            # Matroska is read in clusters, so ``-t`` alone can copy an extra
            # half second of B-frames.  For a single CFR video stream the
            # exact frame count is predictable, so cap video packets (rounding
            # up) while audio still stops at ``-t``.  VFR/unknown/multi-video
            # sources must not be capped: their frame count cannot be derived
            # from a nominal frame rate.
            frames = max(1, math.ceil(cut.duration * fps - 1e-6))
            command += ["-frames:v", str(frames)]
    command += map_args
    command += ["-c", "copy", "-map_metadata", "0"]
    if (
        copy_chapters and probe is not None and probe.chapters
        and container_supports_chapters(output)
    ):
        command += ["-map_chapters", "0"]
    else:
        command += ["-map_chapters", "-1"]
    command += build_metadata_args(
        probe, output, metadata, mapped_streams=mapped or None,
    )
    command += ["-avoid_negative_ts", "make_zero"]
    command += ["-max_muxing_queue_size", "4096"]
    _append_mux_flags(command, output)
    _append_extra(command, extra_args)
    command.append(output)
    return command


def build_segments_copy_commands(
    ffmpeg: str,
    source: str,
    segments: Sequence[Any],
    outputs: Sequence[str],
    *,
    probe: MediaProbe | None = None,
    keyframes: KeyframeIndex | Iterable[Keyframe] | Iterable[float] | None = None,
    selection: TrackSelection | None = None,
    metadata: MetadataOptions | None = None,
    overwrite: bool = False,
    snap_start: bool = True,
    copy_chapters: bool = False,
    include_attached_pic: bool = False,
    respect_selection: bool = True,
    progress: bool = True,
    extra_args: Sequence[str] = (),
) -> list[list[str]]:
    """Build one stream-copy command per selected segment."""
    keyframes = materialize_keyframes(keyframes)
    items = selected_segments(segments, respect_selection=respect_selection)
    if not items:
        raise ValueError("没有选中的片段。")
    if len(items) != len(outputs):
        raise ValueError(
            f"片段数量（{len(items)}）与输出数量（{len(outputs)}）不一致。"
        )
    return [
        build_segment_copy_command(
            ffmpeg, source, segment, output,
            probe=probe, keyframes=keyframes, selection=selection,
            metadata=metadata, overwrite=overwrite, snap_start=snap_start,
            copy_chapters=copy_chapters,
            include_attached_pic=include_attached_pic,
            progress=progress, extra_args=extra_args,
        )
        for segment, output in zip(items, outputs)
    ]


def _concat_quote(path: str | Path) -> str:
    text = str(Path(path).resolve()).replace("\\", "/")
    if "\n" in text or "\r" in text:
        raise ValueError("ffconcat 文件名不能包含换行符。")
    # FFmpeg closes the quote, emits an escaped quote, then re-opens it.
    return "'" + text.replace("'", "'\\''") + "'"


def build_concat_manifest(parts: Iterable[str | Path]) -> str:
    items = list(parts)
    if not items:
        raise ValueError("ffconcat 合并清单至少需要一个片段。")
    lines = ["ffconcat version 1.0"]
    for part in items:
        lines.append(f"file {_concat_quote(part)}")
    return "\n".join(lines) + "\n"


def write_concat_manifest(
    manifest: str | Path,
    parts: Iterable[str | Path] | str,
) -> Path:
    """Write a UTF-8 (LF) ffconcat manifest; the caller still runs FFmpeg."""
    if isinstance(parts, str):
        text = parts if parts.lstrip().startswith("ffconcat") else build_concat_manifest([parts])
    else:
        text = build_concat_manifest(parts)
    target = Path(manifest)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return target


def _concat_map_plan(
    probe: MediaProbe | None,
    output: str,
    selection: TrackSelection | None,
    *,
    attachment_source: str | None,
    include_attached_pic: bool,
) -> tuple[list[str], tuple[StreamInfo, ...]]:
    """Map concat v/a/s plus source attachment/data streams."""
    ext = Path(str(output)).suffix.lower()
    args: list[str] = []
    mapped: list[StreamInfo] = []
    source_input = 1 if attachment_source else None

    if selection is not None:
        if probe is None:
            raise ValueError("按轨道选择合并时必须提供源文件 probe。")
        indices, _ = _selection_indices(
            selection, probe, ext,
            include_attached_pic=include_attached_pic or selection.include_attached_pic,
        )
        selected = [stream for stream in probe.streams if stream.index in indices]
        for kind in ("video", "audio", "subtitle"):
            streams = [stream for stream in selected if stream.codec_type == kind]
            for ordinal, stream in enumerate(streams):
                args += ["-map", f"0:{STREAM_TYPE_LETTERS[kind]}:{ordinal}"]
                mapped.append(stream)
        for kind in ("attachment", "data"):
            if source_input is None:
                continue
            for stream in [item for item in selected if item.codec_type == kind]:
                ordinal = sum(
                    1 for item in probe.streams
                    if item.codec_type == kind and item.index < stream.index
                )
                args += ["-map", f"{source_input}:{STREAM_TYPE_LETTERS[kind]}:{ordinal}"]
                mapped.append(stream)
        return args, tuple(mapped)

    if probe is None:
        letters = ("a",) if ext in AUDIO_CONTAINERS else ("v", "a", "s")
        for letter in letters:
            args += ["-map", f"0:{letter}?"]
        if source_input is not None and ext == ".mkv":
            args += ["-map", f"{source_input}:t?"]
            args += ["-map", f"{source_input}:d?"]
        return args, ()

    for kind in ("video", "audio", "subtitle"):
        streams = [
            stream for stream in probe.streams
            if stream.codec_type == kind
            and stream_suitable(
                stream, ext,
                include_attached_pic=include_attached_pic,
            )
        ]
        for ordinal, stream in enumerate(streams):
            args += ["-map", f"0:{STREAM_TYPE_LETTERS[kind]}:{ordinal}"]
            mapped.append(stream)
    if source_input is not None:
        for kind in ("attachment", "data"):
            streams = [
                stream for stream in probe.streams
                if stream.codec_type == kind
                and stream_suitable(stream, ext)
            ]
            for stream in streams:
                ordinal = sum(
                    1 for item in probe.streams
                    if item.codec_type == kind and item.index < stream.index
                )
                args += [
                    "-map",
                    f"{source_input}:{STREAM_TYPE_LETTERS[kind]}:{ordinal}",
                ]
                mapped.append(stream)
    return args, tuple(mapped)


def build_concat_merge_command(
    ffmpeg: str,
    manifest: str,
    output: str,
    *,
    probe: MediaProbe | None = None,
    selection: TrackSelection | None = None,
    metadata: MetadataOptions | None = None,
    overwrite: bool = False,
    attachment_source: str | None = None,
    chapters_path: str | None = None,
    include_attached_pic: bool = False,
    progress: bool = True,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Merge parts via the concat demuxer with ``-c copy``.

    The concat demuxer cannot carry Matroska attachments, so attachments to be
    preserved are mapped from ``attachment_source`` (the original file) as a
    second input.  Chapters can be added in the same command from an
    ffmetadata input.
    """
    output = _ensure_output_extension(
        output, DEFAULT_SEGMENT_EXTENSION, LOSSLESS_CONTAINERS,
    )
    check_output_available(output, overwrite=overwrite)
    map_args, mapped = _concat_map_plan(
        probe, output, selection,
        attachment_source=attachment_source,
        include_attached_pic=include_attached_pic,
    )
    if not map_args:
        raise ValueError("合并命令没有任何可映射的轨道。")
    command = build_ffmpeg_base(ffmpeg, overwrite=overwrite, progress=progress)
    command += ["-f", "concat", "-safe", "0", "-i", str(manifest)]
    if attachment_source:
        command += ["-i", str(attachment_source)]
    chapter_input: int | None = None
    if chapters_path:
        if not container_supports_chapters(output):
            raise ValueError(f"{Path(output).suffix} 封装不支持章节，请改用 MKV 或 MP4。")
        chapter_input = 1 + (1 if attachment_source else 0)
        command += ["-i", str(chapters_path)]
    command += map_args
    command += ["-ignore_unknown"]
    command += ["-c", "copy"]
    # The concat demuxer does not expose the parts' global tags reliably, so
    # the original file (input 1) is the metadata source when available.
    command += ["-map_metadata", "1" if attachment_source else "0"]
    command += ["-map_chapters", str(chapter_input) if chapter_input is not None else "-1"]
    command += build_metadata_args(
        probe, output, metadata, mapped_streams=mapped or None,
    )
    command += ["-fflags", "+genpts", "-avoid_negative_ts", "make_zero"]
    command += ["-max_muxing_queue_size", "4096"]
    _append_mux_flags(command, output)
    _append_extra(command, extra_args)
    command.append(output)
    return command


# ---------------------------------------------------------------------------
# whole-file remux / external tracks / extraction
# ---------------------------------------------------------------------------


def build_remux_command(
    ffmpeg: str,
    source: str,
    output: str,
    *,
    probe: MediaProbe | None = None,
    selection: TrackSelection | None = None,
    metadata: MetadataOptions | None = None,
    overwrite: bool = False,
    preserve_chapters: bool = True,
    include_attached_pic: bool = False,
    progress: bool = True,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Remux the whole file without re-encoding."""
    output = _ensure_output_extension(output, DEFAULT_SEGMENT_EXTENSION, REMUX_CONTAINERS)
    check_output_available(output, overwrite=overwrite)
    if probe is not None:
        map_plan = plan_stream_maps(
            probe, output, selection,
            include_attached_pic=include_attached_pic,
        )
        map_args = list(map_plan.args)
        mapped = map_plan.streams
    else:
        map_args = generic_map_args(output, selection)
        mapped = ()
    command = build_ffmpeg_base(ffmpeg, overwrite=overwrite, progress=progress)
    command += ["-i", str(source)]
    command += map_args
    command += ["-c", "copy", "-map_metadata", "0"]
    command += ["-map_chapters", "0" if preserve_chapters and container_supports_chapters(output) else "-1"]
    command += build_metadata_args(
        probe, output, metadata, mapped_streams=mapped or None,
    )
    command += ["-max_muxing_queue_size", "4096"]
    _append_mux_flags(command, output)
    _append_extra(command, extra_args)
    command.append(output)
    return command


@dataclass(frozen=True, slots=True)
class ExternalTrack:
    path: str
    kind: str = "audio"
    language: str | None = None
    title: str | None = None
    default: bool | None = None
    forced: bool | None = None
    codec: str = "copy"

    def __post_init__(self) -> None:
        if self.kind not in {"audio", "subtitle"}:
            raise ValueError("外部轨道只支持 audio 或 subtitle。")
        if not str(self.path).strip():
            raise ValueError("外部轨道路径不能为空。")


def _disposition_value(track: ExternalTrack) -> str | None:
    if track.default is None and track.forced is None:
        return None
    flags: list[str] = []
    if track.default:
        flags.append("default")
    if track.forced:
        flags.append("forced")
    return "+".join(flags) if flags else "0"


def build_external_mux_command(
    ffmpeg: str,
    source: str,
    external_tracks: Sequence[ExternalTrack],
    output: str,
    *,
    probe: MediaProbe | None = None,
    selection: TrackSelection | None = None,
    metadata: MetadataOptions | None = None,
    overwrite: bool = False,
    preserve_chapters: bool = True,
    include_attached_pic: bool = False,
    progress: bool = True,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Add external audio/subtitle files to a source without touching video."""
    output = _ensure_output_extension(output, DEFAULT_SEGMENT_EXTENSION, REMUX_CONTAINERS)
    check_output_available(output, overwrite=overwrite)
    tracks = tuple(external_tracks)
    if not tracks:
        raise ValueError("至少需要一个外部音频或字幕文件。")
    if probe is not None:
        map_plan = plan_stream_maps(
            probe, output, selection,
            include_attached_pic=include_attached_pic,
        )
        map_args = list(map_plan.args)
        mapped = list(map_plan.streams)
    else:
        map_args = generic_map_args(output, selection)
        mapped = []
    counters = {
        "audio": sum(1 for stream in mapped if stream.is_audio) if probe else 0,
        "subtitle": sum(1 for stream in mapped if stream.is_subtitle) if probe else 0,
    }
    command = build_ffmpeg_base(ffmpeg, overwrite=overwrite, progress=progress)
    command += ["-i", str(source)]
    for track in tracks:
        command += ["-i", str(track.path)]
    command += map_args
    # Global stream copy must precede per-track codec overrides, otherwise it
    # would reset the external subtitle conversion back to "copy".
    command += ["-c", "copy"]
    external_args: list[str] = []
    for input_index, track in enumerate(tracks, start=1):
        letter = "a" if track.kind == "audio" else "s"
        out_index = counters[track.kind]
        counters[track.kind] += 1
        external_args += ["-map", f"{input_index}:{letter}:0"]
        if track.codec and track.codec != "copy":
            external_args += [f"-c:{letter}:{out_index}", track.codec]
        if track.language:
            external_args += [
                f"-metadata:s:{letter}:{out_index}",
                f"language={track.language}",
            ]
        if track.title:
            external_args += [
                f"-metadata:s:{letter}:{out_index}",
                f"title={track.title}",
            ]
        disposition = _disposition_value(track)
        if disposition is not None:
            external_args += [
                f"-disposition:{letter}:{out_index}", disposition,
            ]
    command += external_args
    command += ["-map_metadata", "0"]
    command += ["-map_chapters", "0" if preserve_chapters and container_supports_chapters(output) else "-1"]
    command += build_metadata_args(
        probe, output, metadata, mapped_streams=mapped or None,
    )
    command += ["-max_muxing_queue_size", "4096"]
    _append_mux_flags(command, output)
    _append_extra(command, extra_args)
    command.append(output)
    return command


def _attachment_output_name(stream: StreamInfo, index: int) -> str:
    name = stream.filename or stream.title or f"attachment_{index:02d}"
    name = sanitize_filename(name)
    if Path(name).suffix:
        return name
    mimetype = stream.mimetype.lower()
    codec = stream.codec_name.lower()
    if "font/ttf" in mimetype or "truetype" in mimetype or codec in {"ttf"}:
        return f"{name}.ttf"
    if "font/otf" in mimetype or "opentype" in mimetype or codec in {"otf"}:
        return f"{name}.otf"
    if "image/" in mimetype:
        extension = mimetype.split("/", 1)[1].split("+", 1)[0]
        if extension in {"jpeg", "jpg", "png"}:
            return f"{name}.{'jpg' if extension == 'jpeg' else extension}"
    return f"{name}.bin"


def build_attachment_extract_commands(
    ffmpeg: str,
    source: str,
    output_dir: str | Path,
    *,
    probe: MediaProbe,
    overwrite: bool = False,
    progress: bool = False,
    extra_args: Sequence[str] = (),
) -> list[list[str]]:
    """Build one ``-dump_attachment`` command per Matroska attachment."""
    attachments = probe.attachment_streams
    if not attachments:
        return []
    folder = Path(output_dir)
    paths: list[Path] = []
    names: set[str] = set()
    for relative_index, stream in enumerate(attachments):
        base_name = _attachment_output_name(stream, relative_index)
        candidate = base_name
        counter = 2
        while os.path.normcase(candidate) in names:
            path = Path(base_name)
            candidate = f"{path.stem}_{counter}{path.suffix}"
            counter += 1
        names.add(os.path.normcase(candidate))
        target = folder / candidate
        check_output_available(target, overwrite=overwrite)
        paths.append(target)
    commands: list[list[str]] = []
    for relative_index, target in enumerate(paths):
        command = build_ffmpeg_base(ffmpeg, overwrite=overwrite, progress=progress)
        command += [
            f"-dump_attachment:t:{relative_index}", str(target),
            "-i", str(source),
            "-map", "0", "-c", "copy", "-f", "null", "-",
        ]
        _append_extra(command, extra_args)
        commands.append(command)
    return commands


def build_extract_command(
    ffmpeg: str,
    source: str,
    output: str,
    kind: str = "audio",
    *,
    stream: int = 0,
    all_streams: bool = False,
    probe: MediaProbe | None = None,
    codec: str = "copy",
    metadata: MetadataOptions | None = None,
    overwrite: bool = False,
    progress: bool = True,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Extract one type (or every track of that type) with stream copy."""
    if kind not in STREAM_TYPE_LETTERS:
        raise ValueError("提取类型必须是 video、audio、subtitle、attachment 或 data。")
    if isinstance(stream, bool) or not isinstance(stream, int) or stream < 0:
        raise ValueError("轨道序号必须是从 0 开始的整数。")
    output_path = Path(str(output))
    if not output_path.name:
        raise ValueError("输出路径不能为空。")
    check_output_available(output_path, overwrite=overwrite)
    if probe is not None:
        type_streams = [
            item for item in probe.streams if item.codec_type == kind
        ]
        if not all_streams and stream >= len(type_streams):
            raise ValueError(
                f"源文件只有 {len(type_streams)} 条 {kind} 轨道，"
                f"无法提取第 {stream} 条。"
            )
    letter = STREAM_TYPE_LETTERS[kind]
    if kind == "attachment":
        command = build_ffmpeg_base(ffmpeg, overwrite=overwrite, progress=progress)
        command += [
            f"-dump_attachment:t:{stream}", str(output_path),
            "-i", str(source),
            "-map", "0", "-c", "copy", "-f", "null", "-",
        ]
        _append_extra(command, extra_args)
        return command
    map_spec = f"0:{letter}" if all_streams else f"0:{letter}:{stream}"
    command = build_ffmpeg_base(ffmpeg, overwrite=overwrite, progress=progress)
    command += ["-i", str(source), "-map", map_spec]
    command += ["-map_metadata", "0", "-map_chapters", "-1"]
    if codec == "copy":
        command += ["-c", "copy"]
    else:
        command += [f"-c:{letter}", codec]
    command += build_metadata_args(probe, output, metadata)
    command += ["-max_muxing_queue_size", "4096"]
    _append_mux_flags(command, output)
    _append_extra(command, extra_args)
    command.append(str(output_path))
    return command


# ---------------------------------------------------------------------------
# chapters
# ---------------------------------------------------------------------------


def _ffmetadata_escape(value: str) -> str:
    text = str(value).replace("\\", "\\\\")
    text = text.replace("\n", "\\\n")
    for character in ("=", ";", "#"):
        text = text.replace(character, f"\\{character}")
    return text


def render_chapter_metadata(
    chapters: Sequence[ChapterInfo],
    *,
    title: str | None = None,
) -> str:
    lines = [";FFMETADATA1"]
    if title is not None:
        lines.append(f"title={_ffmetadata_escape(title)}")
    for chapter in chapters:
        start = max(0, int(round(float(chapter.start) * 1000)))
        end = max(start + 1, int(round(float(chapter.end) * 1000)))
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={start}")
        lines.append(f"END={end}")
        lines.append(f"title={_ffmetadata_escape(chapter.title)}")
    return "\n".join(lines) + "\n"


def chapters_from_segments(
    segments: Sequence[Any],
    *,
    timeline: str = "source",
    respect_selection: bool = True,
    title_template: str | None = None,
    keyframes: KeyframeIndex | Iterable[Keyframe] | Iterable[float] | None = None,
    snap_start: bool = False,
) -> tuple[ChapterInfo, ...]:
    """Turn CutSegments into chapters.

    ``timeline="source"`` keeps the absolute source times; ``"concat"``
    accumulates each segment's duration for the merged output.  Pass the same
    keyframe index and ``snap_start=True`` used for the export to keep chapter
    boundaries equal to the real stream-copy cut points.
    """
    if timeline not in {"source", "concat"}:
        raise ValueError("章节时间线必须是 source 或 concat。")
    keyframes = materialize_keyframes(keyframes)
    items = list(selected_segments(segments, respect_selection=respect_selection))
    if not items:
        raise ValueError("没有选中的片段，无法生成章节。")

    def ranges_for(item: Any) -> tuple[float, float]:
        start, end = cut_segment_range(item)
        if snap_start:
            cut = resolve_cut_range(start, end, keyframes)
            return cut.start, cut.end
        return start, end

    entries: list[tuple[float, float, str]] = []
    if timeline == "source":
        ranges = [(seg, ranges_for(seg)) for seg in items]
        ranges.sort(key=lambda item: item[1][0])
        previous_end = -1.0
        for index, (segment, (start, end)) in enumerate(ranges, start=1):
            if start < previous_end - 0.001:
                raise ValueError(
                    f"第 {index} 个片段的章节区间与上一个片段重叠："
                    f"{start:.3f}s < {previous_end:.3f}s。"
                )
            entries.append((start, end, _chapter_title(segment, index, title_template)))
            previous_end = end
    else:
        cursor = 0.0
        for index, segment in enumerate(items, start=1):
            start, end = ranges_for(segment)
            entries.append((cursor, cursor + (end - start), _chapter_title(segment, index, title_template)))
            cursor += end - start
    return tuple(
        ChapterInfo(index=index, start=start, end=end, tags={"title": name})
        for index, (start, end, name) in enumerate(entries, start=1)
    )


def _chapter_title(segment: Any, index: int, template: str | None) -> str:
    name = segment_name(segment, index)
    if not template:
        return name
    try:
        return str(template).format(name=name, index=index)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"章节标题模板无效：{exc}") from exc


def write_chapters_file(
    path: str | Path,
    chapters: Sequence[ChapterInfo] | str,
    *,
    title: str | None = None,
) -> Path:
    text = chapters if isinstance(chapters, str) else render_chapter_metadata(chapters, title=title)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return target


def build_segments_chapters_command(
    ffmpeg: str,
    source: str,
    output: str,
    chapters_path: str,
    *,
    probe: MediaProbe | None = None,
    selection: TrackSelection | None = None,
    metadata: MetadataOptions | None = None,
    overwrite: bool = False,
    include_attached_pic: bool = False,
    progress: bool = True,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Remux the source and import chapters generated from segments."""
    output = _ensure_output_extension(output, DEFAULT_SEGMENT_EXTENSION, REMUX_CONTAINERS)
    if not container_supports_chapters(output):
        raise ValueError(f"{Path(output).suffix} 封装不支持章节，请改用 MKV 或 MP4。")
    check_output_available(output, overwrite=overwrite)
    if probe is not None:
        map_plan = plan_stream_maps(
            probe, output, selection,
            include_attached_pic=include_attached_pic,
        )
        map_args = list(map_plan.args)
        mapped = map_plan.streams
    else:
        map_args = generic_map_args(output, selection)
        mapped = ()
    command = build_ffmpeg_base(ffmpeg, overwrite=overwrite, progress=progress)
    command += ["-i", str(source), "-i", str(chapters_path)]
    command += map_args
    command += ["-c", "copy", "-map_metadata", "0", "-map_chapters", "1"]
    command += build_metadata_args(
        probe, output, metadata, mapped_streams=mapped or None,
    )
    command += ["-max_muxing_queue_size", "4096"]
    _append_mux_flags(command, output)
    _append_extra(command, extra_args)
    command.append(output)
    return command


# ---------------------------------------------------------------------------
# special transform commands
# ---------------------------------------------------------------------------


def build_loop_command(
    ffmpeg: str,
    source: str,
    output: str,
    *,
    loops: int = 1,
    duration: float | None = None,
    probe: MediaProbe | None = None,
    selection: TrackSelection | None = None,
    metadata: MetadataOptions | None = None,
    overwrite: bool = False,
    include_attached_pic: bool = False,
    progress: bool = True,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Loop a source ``loops`` extra times, optionally capped by duration."""
    if isinstance(loops, bool) or not isinstance(loops, int):
        raise ValueError("循环次数必须是整数（-1 表示无限循环）。")
    if loops < -1:
        raise ValueError("循环次数不能小于 -1。")
    if duration is not None:
        duration = float(duration)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("循环时长必须大于零。")
    output = _ensure_output_extension(output, DEFAULT_SEGMENT_EXTENSION, LOSSLESS_CONTAINERS)
    check_output_available(output, overwrite=overwrite)
    if probe is not None:
        map_plan = plan_stream_maps(
            probe, output, selection,
            include_attached_pic=include_attached_pic,
        )
        map_args = list(map_plan.args)
        mapped = map_plan.streams
    else:
        map_args = generic_map_args(output, selection)
        mapped = ()
    command = build_ffmpeg_base(ffmpeg, overwrite=overwrite, progress=progress)
    if loops:
        command += ["-stream_loop", str(loops)]
    command += ["-i", str(source)]
    if duration is not None:
        command += ["-t", _seconds(duration)]
    command += map_args
    command += ["-c", "copy", "-map_metadata", "0", "-map_chapters", "-1"]
    command += build_metadata_args(
        probe, output, metadata, mapped_streams=mapped or None,
    )
    command += ["-fflags", "+genpts", "-avoid_negative_ts", "make_zero"]
    command += ["-max_muxing_queue_size", "4096"]
    _append_mux_flags(command, output)
    _append_extra(command, extra_args)
    command.append(output)
    return command


def build_keyframe_only_command(
    ffmpeg: str,
    source: str,
    output: str,
    *,
    mode: str = "copy",
    video_codec: str = "libx264",
    crf: int = 18,
    preset: str = "medium",
    probe: MediaProbe | None = None,
    selection: TrackSelection | None = None,
    metadata: MetadataOptions | None = None,
    overwrite: bool = False,
    include_attached_pic: bool = False,
    progress: bool = True,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Keep only keyframes (I frames).

    ``mode="copy"`` uses ``-discard nonkey`` and is lossless.  Re-encoding with
    ``select=eq(pict_type,I)`` is also available when the caller needs a clean
    constant-frame-rate file.
    """
    mode = str(mode).lower()
    if mode not in {"copy", "reencode"}:
        raise ValueError("关键帧抽帧模式必须是 copy 或 reencode。")
    output = _ensure_output_extension(output, DEFAULT_SEGMENT_EXTENSION, LOSSLESS_CONTAINERS)
    check_output_available(output, overwrite=overwrite)
    if probe is not None:
        map_plan = plan_stream_maps(
            probe, output, selection,
            include_attached_pic=include_attached_pic,
        )
        map_args = list(map_plan.args)
        mapped = map_plan.streams
    elif mode == "copy":
        map_args = generic_map_args(output, selection)
        mapped = ()
    else:
        map_args = ["-map", "0:v:0", "-map", "0:a?"]
        mapped = ()
    command = build_ffmpeg_base(ffmpeg, overwrite=overwrite, progress=progress)
    if mode == "copy":
        command += ["-discard", "nokey"]
    command += ["-i", str(source)]
    command += map_args
    if mode == "copy":
        command += ["-c", "copy", "-fps_mode", "passthrough"]
    else:
        command += [
            "-vf", "select=eq(pict_type\\,I)",
            "-fps_mode", "vfr",
            "-c:v", video_codec, "-crf", str(crf), "-preset", preset,
            "-c:a", "copy",
        ]
    command += ["-map_metadata", "0", "-map_chapters", "-1"]
    command += build_metadata_args(
        probe, output, metadata, mapped_streams=mapped or None,
    )
    command += ["-avoid_negative_ts", "make_zero"]
    command += ["-max_muxing_queue_size", "4096"]
    _append_mux_flags(command, output)
    _append_extra(command, extra_args)
    command.append(output)
    return command


def build_fps_change_command(
    ffmpeg: str,
    source: str,
    output: str,
    fps: float | int | str,
    *,
    video_codec: str = "libx264",
    crf: int = 18,
    preset: str = "medium",
    audio: str = "copy",
    metadata: MetadataOptions | None = None,
    overwrite: bool = False,
    progress: bool = True,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Change video frame rate by re-encoding the first video stream."""
    rate = normalize_frame_rate(fps)
    if audio not in {"copy", "none", "aac"}:
        raise ValueError("音频模式必须是 copy、none 或 aac。")
    output = _ensure_output_extension(output, DEFAULT_SEGMENT_EXTENSION, LOSSLESS_CONTAINERS)
    check_output_available(output, overwrite=overwrite)
    map_args = ["-map", "0:v:0"]
    if audio != "none":
        map_args += ["-map", "0:a?"]
    command = build_ffmpeg_base(ffmpeg, overwrite=overwrite, progress=progress)
    command += ["-i", str(source)]
    command += map_args
    command += ["-vf", f"fps={rate}", "-fps_mode", "cfr"]
    command += ["-c:v", video_codec, "-crf", str(crf), "-preset", preset]
    if audio == "copy":
        command += ["-c:a", "copy"]
    elif audio == "aac":
        command += ["-c:a", "aac", "-b:a", "320k"]
    else:
        command += ["-an"]
    command += ["-map_metadata", "0"]
    command += ["-map_chapters", "0" if container_supports_chapters(output) else "-1"]
    command += build_metadata_args(None, output, metadata)
    command += ["-max_muxing_queue_size", "4096"]
    _append_mux_flags(command, output)
    _append_extra(command, extra_args)
    command.append(output)
    return command


def build_hls_to_mkv_command(
    ffmpeg: str,
    source: str,
    output: str,
    *,
    probe: MediaProbe | None = None,
    selection: TrackSelection | None = None,
    metadata: MetadataOptions | None = None,
    overwrite: bool = False,
    preserve_chapters: bool = True,
    allowed_extensions: bool = True,
    protocol_whitelist: str | None = None,
    include_attached_pic: bool = False,
    progress: bool = True,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Remux an HLS playlist into Matroska without re-encoding."""
    path = Path(str(output))
    if path.suffix and path.suffix.lower() != ".mkv":
        raise ValueError("HLS 无损转换的输出必须是 .mkv。")
    output = str(path if path.suffix else path.with_name(f"{path.name}.mkv"))
    check_output_available(output, overwrite=overwrite)
    if probe is not None:
        map_plan = plan_stream_maps(
            probe, output, selection,
            include_attached_pic=include_attached_pic,
        )
        map_args = list(map_plan.args)
        mapped = map_plan.streams
    else:
        map_args = generic_map_args(output, selection)
        mapped = ()
    command = build_ffmpeg_base(ffmpeg, overwrite=overwrite, progress=progress)
    if protocol_whitelist:
        command += ["-protocol_whitelist", str(protocol_whitelist)]
    if allowed_extensions:
        command += ["-allowed_extensions", "ALL"]
    command += ["-i", str(source)]
    command += map_args
    command += ["-c", "copy", "-map_metadata", "0"]
    command += ["-map_chapters", "0" if preserve_chapters and container_supports_chapters(output) else "-1"]
    command += build_metadata_args(
        probe, output, metadata, mapped_streams=mapped or None,
    )
    command += ["-fflags", "+genpts", "-avoid_negative_ts", "make_zero"]
    command += ["-max_muxing_queue_size", "4096"]
    _append_extra(command, extra_args)
    command.append(output)
    return command


def build_interval_stills_command(
    ffmpeg: str,
    source: str,
    output_dir: str | Path,
    *,
    every_n_frames: int | None = None,
    every_n_seconds: float | None = None,
    image_format: str = "jpg",
    start: float | None = None,
    end: float | None = None,
    quality: int | None = None,
    start_number: int = 1,
    overwrite: bool = False,
    progress: bool = False,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Capture JPEG/PNG frames every N frames or every N seconds."""
    if (every_n_frames is None) == (every_n_seconds is None):
        raise ValueError("每 N 帧和每 N 秒必须只填写一个。")
    image_format = str(image_format).lower().lstrip(".")
    if image_format == "jpeg":
        image_format = "jpg"
    if image_format not in {"jpg", "png"}:
        raise ValueError("截图格式只支持 JPEG（jpg）或 PNG。")
    if every_n_frames is not None:
        if isinstance(every_n_frames, bool) or not isinstance(every_n_frames, int) or every_n_frames <= 0:
            raise ValueError("每 N 帧必须是大于零的整数。")
        selector = f"select=not(mod(n\\,{every_n_frames}))"
    else:
        assert every_n_seconds is not None
        interval = float(every_n_seconds)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("每 N 秒必须是大于零的数值。")
        selector = f"fps=1/{interval:.6f}".rstrip("0").rstrip(".")
    if start is not None:
        start = float(start)
        if not math.isfinite(start) or start < 0:
            raise ValueError("截图起点不能小于 0。")
    if end is not None:
        end = float(end)
        if not math.isfinite(end) or end <= 0:
            raise ValueError("截图终点必须大于零。")
        if start is not None and end <= start:
            raise ValueError("截图终点必须晚于起点。")
    if start_number < 0:
        raise ValueError("截图起始编号不能为负数。")
    pattern = Path(output_dir) / f"frame_%06d.{image_format}"
    command = build_ffmpeg_base(ffmpeg, overwrite=overwrite, progress=progress)
    if start is not None:
        command += ["-ss", _seconds(start)]
    command += ["-i", str(source)]
    if end is not None:
        duration = end - (start or 0.0)
        command += ["-t", _seconds(duration)]
    command += ["-map", "0:v:0", "-vf", selector, "-fps_mode", "vfr", "-an"]
    if image_format == "jpg":
        command += ["-q:v", str(2 if quality is None else int(quality))]
    else:
        command += ["-compression_level", str(6 if quality is None else int(quality))]
    command += ["-start_number", str(start_number)]
    _append_extra(command, extra_args)
    command.append(str(pattern))
    return command


# ---------------------------------------------------------------------------
# export plans
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LosslessExportPlan:
    mode: str
    source: str
    commands: tuple[tuple[str, ...], ...]
    segment_outputs: tuple[str, ...] = ()
    merged_output: str | None = None
    part_outputs: tuple[str, ...] = ()
    temp_dir: str | None = None
    manifest_path: str | None = None
    manifest_text: str | None = None
    chapters_path: str | None = None
    chapters_text: str | None = None
    cleanup_paths: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def command_lists(self) -> list[list[str]]:
        return [list(command) for command in self.commands]

    def write_sidecars(self) -> tuple[Path, ...]:
        """Write the ffconcat and chapter sidecars this plan references."""
        written: list[Path] = []
        if self.manifest_path and self.manifest_text is not None:
            written.append(write_concat_manifest(self.manifest_path, self.manifest_text))
        if self.chapters_path and self.chapters_text is not None:
            written.append(write_chapters_file(self.chapters_path, self.chapters_text))
        return tuple(written)


def plan_lossless_export(
    ffmpeg: str,
    source: str,
    segments: Sequence[Any],
    *,
    mode: str = "separate",
    output_dir: str | Path | None = None,
    output: str | None = None,
    template: str = DEFAULT_SEGMENT_TEMPLATE,
    extension: str = DEFAULT_SEGMENT_EXTENSION,
    probe: MediaProbe | None = None,
    keyframes: KeyframeIndex | Iterable[Keyframe] | Iterable[float] | None = None,
    selection: TrackSelection | None = None,
    metadata: MetadataOptions | None = None,
    overwrite: bool = False,
    snap_start: bool = True,
    copy_chapters: bool = False,
    include_attached_pic: bool = False,
    chapters: bool = False,
    chapter_timeline: str | None = None,
    temp_dir: str | Path | None = None,
    progress: bool = True,
    extra_args: Sequence[str] = (),
) -> LosslessExportPlan:
    """Build every command needed for one lossless export mode.

    Modes:
      * ``single``    – exactly one segment, one stream-copy command;
      * ``separate``  – every selected segment exported on its own;
      * ``merge``     – temporary parts + ffconcat merge (``-c copy``);
      * ``both``      – separate outputs *and* an ffconcat merge of them.

    ``merge`` puts parts and the manifest in ``temp_dir`` and lists it in
    ``cleanup_paths``.  ``both`` uses the final per-segment files as merge
    parts; only the manifest/chapter folder is temporary.

    Call ``plan.write_sidecars()`` before running the commands: it writes the
    ffconcat/chapter files and creates their folder.  Output folders for the
    final files must already exist (the builder never creates user folders).
    """
    mode = str(mode).lower()
    if mode not in {"single", "separate", "merge", "both"}:
        raise ValueError("导出模式必须是 single、separate、merge 或 both。")
    keyframes = materialize_keyframes(keyframes)
    items = selected_segments(segments)
    if not items:
        raise ValueError("没有选中的片段。")
    if mode == "single" and len(items) != 1:
        raise ValueError("single 模式只能包含一个选中的片段。")
    if mode in {"merge", "both"}:
        # Concatenating overlapping source ranges silently duplicates frames,
        # so merged exports reject them up front.  Checking the requested
        # ranges (not the keyframe-snapped ones) keeps adjacent cuts usable.
        ranges = sorted(cut_segment_range(segment) for segment in items)
        for index in range(1, len(ranges)):
            if ranges[index][0] < ranges[index - 1][1] - 1e-6:
                raise ValueError(
                    "merge/both 模式不允许重叠片段："
                    f"{ranges[index][0]:.3f}s 落在上一段 {ranges[index - 1][1]:.3f}s 之前。"
                )
    if chapters and mode not in {"merge", "both"}:
        raise ValueError("自动章节只在 merge 或 both 模式下可用。")
    if chapters and chapter_timeline is None:
        chapter_timeline = "concat"
    if chapter_timeline is not None and chapter_timeline not in {"source", "concat"}:
        raise ValueError("章节时间线必须是 source 或 concat。")

    warnings: list[str] = []
    if snap_start:
        if keyframes is None:
            warnings.append(
                "未提供关键帧索引：无损裁切会按请求时间下刀，"
                "起点可能落在非关键帧上而出现花屏/黑场，建议先调用 probe_keyframes。"
            )
        elif not _keyframe_times(keyframes):
            warnings.append(
                "关键帧索引为空：无法吸附起点，将按请求时间尝试无损裁切。"
            )
        seen_warnings: set[str] = set()
        for segment in items:
            start, end = cut_segment_range(segment)
            cut = resolve_cut_range(start, end, keyframes)
            if cut.warning and cut.warning not in seen_warnings:
                warnings.append(cut.warning)
                seen_warnings.add(cut.warning)
            if probe is not None and probe.duration > 0 and end > probe.duration + 0.05:
                warnings.append(
                    f"片段 {segment_name(segment)} 的终点 {end:.3f}s 超过源时长 "
                    f"{probe.duration:.3f}s，输出会被截短。"
                )

    source_path = Path(source)
    source_folder = source_path.parent if str(source_path.parent) else Path(".")
    segment_outputs: tuple[str, ...] = ()
    merged_output: str | None = None
    part_outputs: tuple[str, ...] = ()
    plan_temp_dir: Path | None = None
    manifest_path: Path | None = None
    manifest_text: str | None = None
    chapters_path: Path | None = None
    chapters_text: str | None = None
    cleanup_paths: list[str] = []
    commands: list[list[str]] = []

    ext = normalize_extension(extension)
    if ext not in LOSSLESS_CONTAINERS:
        raise ValueError(f"不支持片段输出扩展名：{ext}")

    if mode in {"single", "separate"}:
        if mode == "single" and output:
            single_output = _ensure_output_extension(output, ext, LOSSLESS_CONTAINERS)
            segment_outputs = (single_output,)
        else:
            folder = Path(output_dir) if output_dir is not None else source_folder
            segment_outputs = plan_segment_outputs(
                source, items, folder, template=template, extension=ext,
                overwrite=overwrite, respect_selection=False,
            )
        commands = build_segments_copy_commands(
            ffmpeg, source, items, segment_outputs,
            probe=probe, keyframes=keyframes, selection=selection,
            metadata=metadata, overwrite=overwrite, snap_start=snap_start,
            copy_chapters=copy_chapters,
            include_attached_pic=include_attached_pic,
            respect_selection=False, progress=progress, extra_args=extra_args,
        )
        if probe is not None:
            try:
                map_plan = plan_stream_maps(
                    probe, segment_outputs[0], selection,
                    include_attached_pic=include_attached_pic,
                )
                warnings.extend(map_plan.warnings)
            except ValueError as exc:
                warnings.append(str(exc))
    else:
        if not output:
            raise ValueError("merge 和 both 模式必须指定合并输出文件。")
        merged_output = _ensure_output_extension(output, ext, LOSSLESS_CONTAINERS)
        if chapters and not container_supports_chapters(merged_output):
            raise ValueError(f"{Path(merged_output).suffix} 封装不支持章节，请改用 MKV 或 MP4。")
        merged_path = Path(merged_output)
        merged_folder = merged_path.parent if str(merged_path.parent) else Path(".")
        part_ext = merged_path.suffix.lower()
        if part_ext not in LOSSLESS_CONTAINERS:
            part_ext = DEFAULT_SEGMENT_EXTENSION

        if mode == "both":
            if part_ext != ext:
                raise ValueError(
                    "both 模式的分段扩展名必须与合并输出一致，"
                    f"请把 extension 设为 {part_ext}。"
                )
            folder = Path(output_dir) if output_dir is not None else merged_folder
            segment_outputs = plan_segment_outputs(
                source, items, folder, template=template, extension=ext,
                overwrite=overwrite, respect_selection=False,
            )
            part_outputs = segment_outputs
            plan_temp_dir = (
                Path(temp_dir) if temp_dir is not None
                else merged_folder / f".{merged_path.stem}_concat"
            )
        else:
            plan_temp_dir = (
                Path(temp_dir) if temp_dir is not None
                else merged_folder / f"{merged_path.stem}_parts"
            )
            part_outputs = tuple(
                str(plan_temp_dir / f"{merged_path.stem}_part_{index:03d}{part_ext}")
                for index in range(1, len(items) + 1)
            )
            check_outputs_available(part_outputs, overwrite=overwrite)

        manifest_path = plan_temp_dir / "segments.ffconcat"
        if manifest_path.exists() and not overwrite:
            raise FileExistsError(
                f"合并清单已存在：{manifest_path}；如需覆盖请启用覆盖选项。"
            )
        manifest_text = build_concat_manifest(part_outputs)

        if chapters:
            assert chapter_timeline is not None
            chapter_items = chapters_from_segments(
                items, timeline=chapter_timeline, respect_selection=False,
                keyframes=keyframes, snap_start=snap_start,
            )
            chapters_text = render_chapter_metadata(
                chapter_items, title=merged_path.stem,
            )
            chapters_path = plan_temp_dir / "chapters.ffmetadata"
            if chapters_path.exists() and not overwrite:
                raise FileExistsError(
                    f"章节文件已存在：{chapters_path}；如需覆盖请启用覆盖选项。"
                )

        commands = build_segments_copy_commands(
            ffmpeg, source, items, part_outputs,
            probe=probe, keyframes=keyframes, selection=selection,
            metadata=metadata, overwrite=overwrite, snap_start=snap_start,
            copy_chapters=False,
            include_attached_pic=include_attached_pic,
            respect_selection=False, progress=progress, extra_args=extra_args,
        )
        commands.append(
            build_concat_merge_command(
                ffmpeg, str(manifest_path), merged_output,
                probe=probe, selection=selection, metadata=metadata,
                overwrite=overwrite,
                attachment_source=source,
                chapters_path=str(chapters_path) if chapters_path else None,
                include_attached_pic=include_attached_pic,
                progress=progress, extra_args=extra_args,
            )
        )
        if probe is not None:
            try:
                map_plan = plan_stream_maps(
                    probe, merged_output, selection,
                    include_attached_pic=include_attached_pic,
                )
                warnings.extend(map_plan.warnings)
            except ValueError as exc:
                warnings.append(str(exc))
        cleanup_paths.append(str(plan_temp_dir))

    _reject_duplicate_final_outputs(segment_outputs, merged_output, part_outputs)

    return LosslessExportPlan(
        mode=mode,
        source=str(source),
        commands=tuple(tuple(command) for command in commands),
        segment_outputs=tuple(segment_outputs),
        merged_output=merged_output,
        part_outputs=tuple(part_outputs),
        temp_dir=str(plan_temp_dir) if plan_temp_dir else None,
        manifest_path=str(manifest_path) if manifest_path else None,
        manifest_text=manifest_text,
        chapters_path=str(chapters_path) if chapters_path else None,
        chapters_text=chapters_text,
        cleanup_paths=tuple(cleanup_paths),
        warnings=tuple(dict.fromkeys(warnings)),
    )


# ---------------------------------------------------------------------------
# presentation helpers
# ---------------------------------------------------------------------------


# Friendly aliases for callers that prefer the shorter/verb-first names.
segments_to_chapters = chapters_from_segments
build_extract_attachment_commands = build_attachment_extract_commands
build_lossless_cut_command = build_segment_copy_command
build_ffconcat_manifest = build_concat_manifest
build_concat_copy_command = build_concat_merge_command


def build_cut_command(
    ffmpeg: str,
    source: str,
    start: float,
    end: float,
    output: str,
    **kwargs: Any,
) -> list[str]:
    """Convenience wrapper for callers that have raw start/end seconds."""
    return build_segment_copy_command(
        ffmpeg, source, (start, end), output, **kwargs,
    )


def quote_command(command: Sequence[str]) -> str:
    return subprocess.list2cmdline([str(argument) for argument in command])


def describe_command(command: Sequence[str]) -> str:
    """Return a readable one-line summary for logs/UI."""
    return quote_command(command)
