"""LosslessCut project and cut-list interchange helpers.

The module intentionally depends only on the Python standard library.  It
stores the common subset of a LosslessCut ``.llc`` version 2 project
(``mediaFileName`` and ``cutSegments``) and converts cut segments to and
from CSV, TSV, MPlayer EDL, SRT and YouTube chapter lists.

All user-facing validation errors are Chinese ``ValueError`` subclasses so a
UI can show them directly.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from lossless_core import (
    Chapter,
    CutSegment,
    chapters_to_segments as _core_chapters_to_segments,
    make_segment_id,
)


LLC_VERSION = 2
TIMECODE_DECIMALS = 3
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_SEGMENTS = 100_000
MAX_JSON_DEPTH = 64

SUPPORTED_FORMATS = ("llc", "csv", "tsv", "mplayer", "srt", "chapters")

_FORMAT_ALIASES = {
    "llc": "llc",
    "lossless": "llc",
    "losslesscut": "llc",
    "losslesscut-project": "llc",
    "csv": "csv",
    "tsv": "tsv",
    "mplayer": "mplayer",
    "mplayer-edl": "mplayer",
    "mplayer_edl": "mplayer",
    "edl": "mplayer",
    "srt": "srt",
    "subrip": "srt",
    "chapters": "chapters",
    "chapter": "chapters",
    "youtube": "chapters",
    "youtube-chapters": "chapters",
    "youtube_chapters": "chapters",
}

_EXTENSION_FORMATS = {
    ".llc": "llc",
    ".csv": "csv",
    ".tsv": "tsv",
    ".edl": "mplayer",
    ".srt": "srt",
    ".chapters": "chapters",
}

_TAG_SEPARATORS = (";", "|")
_SELECTED_TRUE = {"1", "true", "yes", "y", "on", "是", "选中"}
_SELECTED_FALSE = {"0", "false", "no", "n", "off", "否", "未选"}

_CSV_HEADERS = {
    "start": "start",
    "start_time": "start",
    "starttime": "start",
    "from": "start",
    "in": "start",
    "入点": "start",
    "开始": "start",
    "end": "end",
    "end_time": "end",
    "endtime": "end",
    "to": "end",
    "out": "end",
    "出点": "end",
    "结束": "end",
    "name": "name",
    "title": "name",
    "label": "name",
    "名称": "name",
    "标题": "name",
    "tags": "tags",
    "tag": "tags",
    "标签": "tags",
    "selected": "selected",
    "select": "selected",
    "enabled": "selected",
    "选中": "selected",
}

_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_SRT_TIMING = re.compile(
    r"^\s*(\d{1,3}):([0-5]\d):([0-5]\d)[,.](\d{1,3})"
    r"\s*-->\s*"
    r"(\d{1,3}):([0-5]\d):([0-5]\d)[,.](\d{1,3})"
    r"\s*(.*)$"
)
_SRT_POSITION_TOKEN = re.compile(r"^(?:X1|X2|Y1|Y2):-?\d+$")
_CHAPTER_LINE = re.compile(
    r"^(\d{1,3}(?::\d{1,2}){1,2}(?:[.,]\d{1,3})?)(?:\s+(.*))?$"
)
_PLAIN_SECONDS = re.compile(r"^\+?(?:\d+(?:\.\d*)?|\.\d+)$")
_COLON_TIMECODE = re.compile(
    r"^(\d+):(\d{1,2})(?::(\d{1,2}))?(?:[.,](\d{1,9}))?$"
)
_LINE_SPLIT = re.compile(r"\r\n|\r|\n")


class LosslessEdlError(ValueError):
    """Base class for readable, user-facing cut-list errors."""


class LlcFormatError(LosslessEdlError):
    """Raised when a LosslessCut project cannot be parsed safely."""


class SegmentValidationError(LosslessEdlError):
    """Raised when a cut segment is negative, overlapping or out of bounds."""


@dataclass(slots=True)
class LlcProject:
    """The common subset of a LosslessCut project."""

    media_file_name: str
    segments: list[CutSegment] = field(default_factory=list)
    media_duration: float | None = None


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SegmentValidationError(f"{label}必须是数字。")
    result = float(value)
    if not math.isfinite(result):
        raise SegmentValidationError(f"{label}必须是有限数字。")
    return result


def _milliseconds(value: Any, label: str = "时间") -> int:
    raw = _number(value, label)
    if raw < 0:
        raise SegmentValidationError(f"{label}不能为负数。")
    try:
        scaled = Decimal(str(raw)) * 1000
        return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise SegmentValidationError(f"{label}无法转换为毫秒。") from exc


def _milliseconds_to_text(milliseconds: int) -> str:
    seconds, fraction = divmod(milliseconds, 1000)
    return f"{seconds}.{fraction:03d}"


def format_seconds(value: Any) -> str:
    """Format seconds with exactly three decimals (millisecond precision)."""

    return _milliseconds_to_text(_milliseconds(value))


def format_timecode(value: Any, separator: str = ".") -> str:
    """Format a non-negative value as ``HH:MM:SS.mmm``."""

    if separator not in (".", ","):
        raise LosslessEdlError("时间码分隔符只能是 '.' 或 ','。")
    milliseconds = _milliseconds(value)
    seconds, fraction = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{fraction:03d}"


def format_srt_timecode(value: Any) -> str:
    return format_timecode(value, ",")


def _fraction_seconds(text: str) -> float:
    return int(text.ljust(9, "0")) / 1_000_000_000


def parse_timecode(value: Any) -> float:
    """Parse decimal seconds or ``[HH:]MM:SS[.mmm]`` into seconds.

    SRT-style commas are accepted in addition to dots.
    """

    if isinstance(value, bool):
        raise LosslessEdlError("时间码不能是布尔值。")
    if isinstance(value, (int, float)):
        seconds = _number(value, "时间码")
        if seconds < 0:
            raise LosslessEdlError("时间码不能为负数。")
        return seconds
    if not isinstance(value, str):
        raise LosslessEdlError("时间码必须是文字或数字。")
    text = value.strip()
    if not text:
        raise LosslessEdlError("时间码不能为空。")
    if _PLAIN_SECONDS.match(text):
        seconds = float(text)
        if not math.isfinite(seconds):
            raise LosslessEdlError(f"时间码“{text}”超出范围。")
        return seconds
    match = _COLON_TIMECODE.match(text)
    if not match:
        raise LosslessEdlError(f"无法识别时间码“{text}”，请使用秒数或 HH:MM:SS.mmm。")
    first, second, third, fraction = match.groups()
    try:
        second_value = int(second)
        third_value = int(third) if third is not None else None
        first_value = int(first)
    except ValueError as exc:
        raise LosslessEdlError(f"时间码“{text}”超出范围。") from exc
    if second_value > 59 or (third_value is not None and third_value > 59):
        raise LosslessEdlError(f"时间码“{text}”的分钟或秒不能超过 59。")
    if third_value is None:
        seconds = first_value * 60 + second_value
    else:
        seconds = first_value * 3600 + second_value * 60 + third_value
    if fraction:
        seconds += _fraction_seconds(fraction)
    result = float(seconds)
    if not math.isfinite(result):
        raise LosslessEdlError(f"时间码“{text}”超出范围。")
    return result


def _srt_seconds(hours: str, minutes: str, seconds: str, fraction: str) -> float:
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + _fraction_seconds(fraction)
    )


def validate_media_file_name(value: Any) -> str:
    """Validate ``mediaFileName`` as a relative, traversal-free path."""

    if not isinstance(value, str):
        raise LlcFormatError("mediaFileName 必须是文字。")
    if not value:
        raise LlcFormatError("mediaFileName 不能为空。")
    if len(value) > 1024:
        raise LlcFormatError("mediaFileName 过长。")
    if value != value.strip():
        raise LlcFormatError("mediaFileName 不能以空格开头或结尾。")
    if _CONTROL_CHARS.search(value):
        raise LlcFormatError("mediaFileName 不能包含控制字符。")
    if "://" in value or value.lower().startswith(("file:", "http:", "https:")):
        raise LlcFormatError("mediaFileName 不能是网址。")

    normalized = value.replace("\\", "/")
    if normalized.startswith("//"):
        raise LlcFormatError("mediaFileName 不能是网络共享路径。")
    if normalized.startswith("/"):
        raise LlcFormatError("mediaFileName 必须是相对路径，不能是绝对路径。")
    if normalized.endswith("/"):
        raise LlcFormatError("mediaFileName 不能以斜杠结尾。")
    if _DRIVE_PATH.match(value):
        raise LlcFormatError("mediaFileName 不能包含盘符。")

    parts = normalized.split("/")
    if any(part == ".." for part in parts):
        raise LlcFormatError("mediaFileName 不能包含“..”，以防跳出媒体目录。")
    parts = [part for part in parts if part not in ("", ".")]
    if not parts:
        raise LlcFormatError("mediaFileName 没有有效的文件名。")
    for part in parts:
        if ":" in part:
            raise LlcFormatError("mediaFileName 不能包含冒号。")
        if any(char in '<>"|?*' for char in part):
            raise LlcFormatError("mediaFileName 不能包含 Windows 不允许的字符。")
        if part.endswith((" ", ".")):
            raise LlcFormatError("mediaFileName 的路径段不能以空格或点结尾。")
        if part.split(".", 1)[0].lower() in _WINDOWS_RESERVED_NAMES:
            raise LlcFormatError(f"mediaFileName 使用了系统保留名“{part}”。")
    return value


def resolve_media_path(
    media_file_name: Any,
    base_dir: str | os.PathLike[str],
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve a project media name below *base_dir* without escaping it."""

    name = validate_media_file_name(media_file_name)
    try:
        base = Path(base_dir).resolve(strict=False)
    except (TypeError, OSError, ValueError) as exc:
        raise LlcFormatError(f"无效的媒体目录：{base_dir!r}") from exc
    normalized = name.replace("\\", "/")
    relative = Path(*[part for part in normalized.split("/") if part not in ("", ".")])
    try:
        candidate = (base / relative).resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LlcFormatError("mediaFileName 无法解析为有效路径。") from exc
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise LlcFormatError("mediaFileName 指向媒体目录之外，已拒绝加载。") from exc
    if candidate == base:
        raise LlcFormatError("mediaFileName 没有指向文件。")
    if must_exist and not candidate.is_file():
        raise LlcFormatError(f"找不到媒体文件：{candidate}")
    return candidate


def _read_name(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SegmentValidationError(f"{label}的片段名称必须是文字。")
    return value


def _read_tags(value: Any, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SegmentValidationError(f"{label}的标签必须是键值对。")
    tags: dict[str, str] = {}
    for key, tag_value in value.items():
        if not isinstance(key, str) or not isinstance(tag_value, str):
            raise SegmentValidationError(f"{label}的标签键和值都必须是文字。")
        if not key.strip():
            raise SegmentValidationError(f"{label}的标签键不能为空。")
        tags[key] = tag_value
    return tags


def _read_selected(value: Any, label: str) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise SegmentValidationError(f"{label}的 selected 必须是 true 或 false。")


def _read_segment_id(value: Any, label: str) -> str:
    if value is None:
        return make_segment_id()
    if not isinstance(value, str) or not value.strip():
        raise SegmentValidationError(f"{label}的片段 ID 必须是非空文字。")
    return value


def _segment_field(segment: Any, name: str, label: str) -> Any:
    try:
        return getattr(segment, name)
    except AttributeError as exc:
        raise SegmentValidationError(f"{label}不是有效的片段对象。") from exc


def _make_segment(
    start: float,
    end: float,
    *,
    name: str = "",
    tags: Mapping[str, str] | None = None,
    selected: bool = True,
    segment_id: str | None = None,
) -> CutSegment:
    return CutSegment(
        id=segment_id or make_segment_id(),
        start=float(start),
        end=float(end),
        name=name,
        tags=dict(tags or {}),
        selected=bool(selected),
    )


def validate_segments(
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    *,
    media_duration: float | None = None,
) -> list[CutSegment]:
    """Validate bounds and overlap at millisecond precision."""

    if isinstance(segments, (str, bytes, bytearray, dict)):
        raise SegmentValidationError("片段列表格式错误。")
    try:
        items = list(segments)
    except TypeError as exc:
        raise SegmentValidationError("片段列表格式错误。") from exc
    if len(items) > MAX_SEGMENTS:
        raise SegmentValidationError(f"片段数量不能超过 {MAX_SEGMENTS}。")

    duration_ms: int | None = None
    if media_duration is not None:
        duration = _number(media_duration, "视频总时长")
        if duration < 0:
            raise SegmentValidationError("视频总时长不能为负数。")
        duration_ms = _milliseconds(duration, "视频总时长")

    records: list[tuple[int, int, int, int, str]] = []
    seen_ids: set[str] = set()
    for index, segment in enumerate(items, start=1):
        label = f"第 {index} 段"
        end_value = _segment_field(segment, "end", label)
        if end_value is None:
            raise SegmentValidationError(
                f"{label}缺少结束时间，标记点不能作为剪切片段。"
            )
        start_value = _number(_segment_field(segment, "start", label), f"{label}的开始时间")
        end_value = _number(end_value, f"{label}的结束时间")
        if start_value < 0:
            raise SegmentValidationError(f"{label}的开始时间不能为负数。")
        if end_value <= start_value:
            raise SegmentValidationError(f"{label}的结束时间必须晚于开始时间。")
        start_ms = _milliseconds(start_value, f"{label}的开始时间")
        end_ms = _milliseconds(end_value, f"{label}的结束时间")
        if end_ms <= start_ms:
            raise SegmentValidationError(f"{label}的时长不足 1 毫秒，无法精确保存。")
        if duration_ms is not None and end_ms > duration_ms:
            raise SegmentValidationError(
                f"{label}结束于 {format_seconds(end_value)}，超出视频总时长 "
                f"{format_seconds(media_duration)}。"
            )
        _read_name(_segment_field(segment, "name", label), label)
        _read_tags(_segment_field(segment, "tags", label), label)
        _read_selected(_segment_field(segment, "selected", label), label)
        segment_id = _segment_field(segment, "id", label)
        if not isinstance(segment_id, str) or not segment_id:
            raise SegmentValidationError(f"{label}的片段 ID 必须是非空文字。")
        if segment_id in seen_ids:
            raise SegmentValidationError(f"{label}的片段 ID 与前面的片段重复。")
        seen_ids.add(segment_id)
        records.append((start_ms, end_ms, index, index, segment_id))

    ordered = sorted(records, key=lambda item: (item[0], item[1], item[2]))
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            previous_index = previous[2]
            current_index = current[2]
            raise SegmentValidationError(
                f"第 {previous_index} 段与第 {current_index} 段重叠，"
                "请先调整片段边界。"
            )
    return items


def _validate_duration(value: Any) -> float | None:
    if value is None:
        return None
    duration = _number(value, "视频总时长")
    if duration < 0:
        raise SegmentValidationError("视频总时长不能为负数。")
    return duration


class _Json5Parser:
    """A small JSON5 parser covering the subset used by LosslessCut files."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.length = len(text)
        self.pos = 0
        self.depth = 0

    def parse(self) -> Any:
        self._skip_space()
        value = self._parse_value()
        self._skip_space()
        if self.pos != self.length:
            self._error("文件末尾还有无法识别的内容")
        return value

    def _error(self, message: str) -> None:
        line = self.text.count("\n", 0, self.pos) + 1
        line_start = self.text.rfind("\n", 0, self.pos) + 1
        column = self.pos - line_start + 1
        raise LlcFormatError(f"LLC 文件格式错误（第 {line} 行第 {column} 列）：{message}。")

    def _skip_space(self) -> None:
        while self.pos < self.length:
            char = self.text[self.pos]
            if char.isspace():
                self.pos += 1
                continue
            if self.text.startswith("//", self.pos):
                newline = self.text.find("\n", self.pos + 2)
                self.pos = self.length if newline < 0 else newline + 1
                continue
            if self.text.startswith("/*", self.pos):
                end = self.text.find("*/", self.pos + 2)
                if end < 0:
                    self._error("块注释没有结束标记")
                self.pos = end + 2
                continue
            break

    def _parse_value(self) -> Any:
        self._skip_space()
        if self.pos >= self.length:
            self._error("缺少值")
        char = self.text[self.pos]
        if char == "{":
            return self._parse_object()
        if char == "[":
            return self._parse_array()
        if char in ('"', "'"):
            return self._parse_string()
        if char.isdigit() or char in "+-.":
            return self._parse_number()
        identifier = self._parse_identifier()
        if identifier == "true":
            return True
        if identifier == "false":
            return False
        if identifier == "null":
            return None
        self._error(f"无法识别的值“{identifier}”")

    def _enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_JSON_DEPTH:
            self._error("嵌套层级过深")

    def _parse_object(self) -> dict[str, Any]:
        self._enter()
        self.pos += 1
        result: dict[str, Any] = {}
        self._skip_space()
        if self.pos < self.length and self.text[self.pos] == "}":
            self.pos += 1
            self.depth -= 1
            return result
        while True:
            self._skip_space()
            if self.pos >= self.length:
                self._error("对象没有结束的“}”")
            char = self.text[self.pos]
            if char in ('"', "'"):
                key = self._parse_string()
            elif char.isalpha() or char in "_$":
                key = self._parse_identifier()
            else:
                self._error("对象的键必须是文字或标识符")
            self._skip_space()
            if self.pos >= self.length or self.text[self.pos] != ":":
                self._error("对象的键后面缺少冒号")
            self.pos += 1
            if key in result:
                self._error(f"对象的键“{key}”重复")
            result[key] = self._parse_value()
            self._skip_space()
            if self.pos >= self.length:
                self._error("对象没有结束的“}”")
            char = self.text[self.pos]
            if char == ",":
                self.pos += 1
                self._skip_space()
                if self.pos < self.length and self.text[self.pos] == "}":
                    self.pos += 1
                    self.depth -= 1
                    return result
                continue
            if char == "}":
                self.pos += 1
                self.depth -= 1
                return result
            self._error("对象成员之间缺少逗号")

    def _parse_array(self) -> list[Any]:
        self._enter()
        self.pos += 1
        result: list[Any] = []
        self._skip_space()
        if self.pos < self.length and self.text[self.pos] == "]":
            self.pos += 1
            self.depth -= 1
            return result
        while True:
            result.append(self._parse_value())
            self._skip_space()
            if self.pos >= self.length:
                self._error("数组没有结束的“]”")
            char = self.text[self.pos]
            if char == ",":
                self.pos += 1
                self._skip_space()
                if self.pos < self.length and self.text[self.pos] == "]":
                    self.pos += 1
                    self.depth -= 1
                    return result
                continue
            if char == "]":
                self.pos += 1
                self.depth -= 1
                return result
            self._error("数组成员之间缺少逗号")

    def _parse_string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        chars: list[str] = []
        while self.pos < self.length:
            char = self.text[self.pos]
            self.pos += 1
            if char == quote:
                return "".join(chars)
            if char in "\r\n":
                self._error("字符串中不能直接换行")
            if char != "\\":
                chars.append(char)
                continue
            if self.pos >= self.length:
                self._error("字符串转义没有结束")
            escape = self.text[self.pos]
            self.pos += 1
            if escape in "\r\n":
                if escape == "\r" and self.pos < self.length and self.text[self.pos] == "\n":
                    self.pos += 1
                continue
            simple = {
                "b": "\b",
                "f": "\f",
                "n": "\n",
                "r": "\r",
                "t": "\t",
                "v": "\v",
                "0": "\0",
                "'": "'",
                '"': '"',
                "\\": "\\",
                "/": "/",
            }
            if escape in simple:
                chars.append(simple[escape])
                continue
            if escape == "x":
                chars.append(chr(self._parse_hex(2)))
                continue
            if escape == "u":
                code = self._parse_hex(4)
                if 0xD800 <= code <= 0xDBFF:
                    if not self.text.startswith("\\u", self.pos):
                        self._error("Unicode 代理对不完整")
                    self.pos += 2
                    low = self._parse_hex(4)
                    if not 0xDC00 <= low <= 0xDFFF:
                        self._error("Unicode 代理对不完整")
                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                elif 0xDC00 <= code <= 0xDFFF:
                    self._error("Unicode 代理对不完整")
                chars.append(chr(code))
                continue
            chars.append(escape)
        self._error("字符串没有结束的引号")

    def _parse_hex(self, length: int) -> int:
        digits = self.text[self.pos:self.pos + length]
        if len(digits) != length or any(char not in "0123456789abcdefABCDEF" for char in digits):
            self._error("十六进制转义格式错误")
        self.pos += length
        return int(digits, 16)

    def _parse_identifier(self) -> str:
        start = self.pos
        if self.pos < self.length:
            char = self.text[self.pos]
            if not (char.isalpha() or char in "_$"):
                self._error("无法识别的字符")
            self.pos += 1
        while self.pos < self.length:
            char = self.text[self.pos]
            if char.isalnum() or char in "_$":
                self.pos += 1
            else:
                break
        return self.text[start:self.pos]

    def _parse_number(self) -> int | float:
        match = re.match(
            r"[+-]?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)",
            self.text[self.pos:],
        )
        if not match:
            self._error("数字格式错误")
        token = match.group(0)
        self.pos += len(token)
        try:
            if re.fullmatch(r"[+-]?0[xX][0-9a-fA-F]+", token):
                return int(token, 16)
            if any(char in token for char in ".eE"):
                value = float(token)
                if not math.isfinite(value):
                    self._error("数值过大")
                return value
            return int(token, 10)
        except ValueError as exc:
            self._error(f"数字格式错误：{token}")
            raise AssertionError from exc


def _llc_version(value: Any) -> int:
    if isinstance(value, bool):
        raise LlcFormatError("LLC 的 version 必须是数字 2。")
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise LlcFormatError("LLC 的 version 必须是数字 2。")
        value = int(value)
    if not isinstance(value, int):
        raise LlcFormatError("LLC 的 version 必须是数字 2。")
    if value != LLC_VERSION:
        raise LlcFormatError(f"只支持 LosslessCut .llc version {LLC_VERSION}，当前为 {value}。")
    return value


def parse_llc(text: str) -> LlcProject:
    """Parse a LosslessCut version 2 project written as JSON or JSON5.

    Tags may be a LosslessCut string array or a ``{key: value}`` mapping; both
    are represented by ``CutSegment.tags``.
    """

    if not isinstance(text, str):
        raise LlcFormatError("LLC 内容必须是文字。")
    if len(text) > MAX_FILE_BYTES:
        raise LlcFormatError("LLC 内容过大。")
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise LlcFormatError("LLC 文件为空。")
    data = _Json5Parser(text).parse()
    if not isinstance(data, dict):
        raise LlcFormatError("LLC 文件的顶层必须是对象。")
    _llc_version(data.get("version", LLC_VERSION))

    if "mediaFileName" not in data:
        raise LlcFormatError("LLC 文件缺少 mediaFileName。")
    media_file_name = validate_media_file_name(data["mediaFileName"])

    raw_segments = data.get("cutSegments")
    if not isinstance(raw_segments, list):
        raise LlcFormatError("LLC 文件缺少 cutSegments 列表。")
    if len(raw_segments) > MAX_SEGMENTS:
        raise LlcFormatError(f"cutSegments 不能超过 {MAX_SEGMENTS} 个。")

    duration = data.get("mediaDuration", data.get("mediaFileDuration"))
    duration = _validate_duration(duration)

    segments: list[CutSegment] = []
    for index, raw in enumerate(raw_segments, start=1):
        label = f"第 {index} 段"
        if not isinstance(raw, dict):
            raise LlcFormatError(f"{label}必须是对象。")
        if "start" not in raw or "end" not in raw:
            raise LlcFormatError(f"{label}缺少 start 或 end。")
        if raw["end"] is None:
            raise LlcFormatError(f"{label}缺少结束时间，标记点不能作为剪切片段。")
        start = _number(raw["start"], f"{label}的开始时间")
        end = _number(raw["end"], f"{label}的结束时间")
        name = _read_name(raw.get("name", ""), label)
        tags = _tags_from_llc(raw.get("tags", []), label)
        selected = _read_selected(raw.get("selected", True), label)
        segment_id = _read_segment_id(raw.get("id"), label)
        segments.append(
            _make_segment(
                start,
                end,
                name=name,
                tags=tags,
                selected=selected,
                segment_id=segment_id,
            )
        )
    validate_segments(segments, media_duration=duration)
    return LlcProject(media_file_name, segments, duration)


def _tags_from_llc(value: Any, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return _read_tags(value, label)
    if isinstance(value, str):
        return {value: ""}
    if isinstance(value, (list, tuple)):
        tags: dict[str, str] = {}
        for item in value:
            if not isinstance(item, str):
                raise LlcFormatError(f"{label}的标签必须是文字或键值对。")
            if not item.strip():
                continue
            key, tag_value = _parse_llc_tag(item)
            if not key.strip():
                raise LlcFormatError(f"{label}的标签键不能为空。")
            if key in tags and tags[key] != tag_value:
                raise LlcFormatError(f"{label}的标签“{key}”重复且内容不同。")
            tags[key] = tag_value
        return tags
    raise LlcFormatError(f"{label}的标签必须是数组或键值对。")


def _escape_llc_tag(value: str) -> str:
    return value.replace("\\", "\\\\").replace("=", "\\=")


def _unescape_llc_tag(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value) and value[index + 1] in "\\=":
            result.append(value[index + 1])
            index += 2
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _parse_llc_tag(value: str) -> tuple[str, str]:
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "=":
            return (
                _unescape_llc_tag(value[:index]),
                _unescape_llc_tag(value[index + 1:]),
            )
    return _unescape_llc_tag(value), ""


def _tags_for_llc(tags: Mapping[str, str]) -> list[str]:
    result: list[str] = []
    for key, value in tags.items():
        escaped_key = _escape_llc_tag(key)
        if value == "":
            result.append(escaped_key)
        else:
            result.append(f"{escaped_key}={_escape_llc_tag(value)}")
    return result


def _segment_payload(segment: CutSegment, index: int) -> dict[str, Any]:
    label = f"第 {index} 段"
    start_ms = _milliseconds(_segment_field(segment, "start", label), f"{label}的开始时间")
    end_ms = _milliseconds(_segment_field(segment, "end", label), f"{label}的结束时间")
    name = _read_name(_segment_field(segment, "name", label), label)
    tags = _read_tags(_segment_field(segment, "tags", label), label)
    selected = _read_selected(_segment_field(segment, "selected", label), label)
    segment_id = _segment_field(segment, "id", label)
    if not isinstance(segment_id, str) or not segment_id:
        raise SegmentValidationError(f"{label}的片段 ID 必须是非空文字。")
    return {
        "id": segment_id,
        "start": start_ms / 1000,
        "end": end_ms / 1000,
        "name": name,
        "tags": _tags_for_llc(tags),
        "selected": selected,
    }


def dump_llc(
    media_file_name: Any,
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    *,
    media_duration: float | None = None,
) -> str:
    """Serialize the common LosslessCut version 2 subset.

    Tags are written as a LosslessCut string array.  A mapping tag with a
    non-empty value is encoded as ``key=value`` and decoded again on load.
    """

    name = validate_media_file_name(media_file_name)
    duration = _validate_duration(media_duration)
    items = validate_segments(segments, media_duration=duration)
    payload: dict[str, Any] = {
        "version": LLC_VERSION,
        "mediaFileName": name,
        "cutSegments": [
            _segment_payload(segment, index)
            for index, segment in enumerate(items, start=1)
        ],
    }
    if duration is not None:
        payload["mediaDuration"] = _milliseconds(duration) / 1000
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _decode_bytes(data: bytes, source: str) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise LosslessEdlError(f"无法读取 {source}：UTF-16 编码损坏。") from exc
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise LosslessEdlError(f"无法读取 {source}：请使用 UTF-8、UTF-16 或 GB18030 编码。")


def read_text_file(path: str | os.PathLike[str]) -> str:
    """Read a text interchange file with common Chinese encodings."""

    try:
        source = Path(path)
    except TypeError as exc:
        raise LosslessEdlError(f"无效的文件路径：{path!r}") from exc
    try:
        size = source.stat().st_size
    except (OSError, ValueError) as exc:
        raise LosslessEdlError(f"无法读取文件：{source}") from exc
    if not source.is_file():
        raise LosslessEdlError(f"不是文件：{source}")
    if size > MAX_FILE_BYTES:
        raise LosslessEdlError(f"文件过大，最多支持 {MAX_FILE_BYTES // (1024 * 1024)} MB。")
    try:
        data = source.read_bytes()
    except (OSError, ValueError) as exc:
        raise LosslessEdlError(f"无法读取文件：{source}") from exc
    return _decode_bytes(data, str(source))


def write_text_file(path: str | os.PathLike[str], text: str) -> Path:
    """Write UTF-8 text atomically, creating the parent directory if needed."""

    if not isinstance(text, str):
        raise LosslessEdlError("写入内容必须是文字。")
    try:
        target = Path(path)
    except TypeError as exc:
        raise LosslessEdlError(f"无效的文件路径：{path!r}") from exc
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        raise LosslessEdlError(f"无法创建目录：{parent}") from exc
    temporary = ""
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except (OSError, ValueError) as exc:
        raise LosslessEdlError(f"无法写入文件：{target}") from exc
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return target


def load_llc(path: str | os.PathLike[str]) -> LlcProject:
    return parse_llc(read_text_file(path))


def save_llc(
    path: str | os.PathLike[str],
    media_file_name: Any,
    segments: Sequence[CutSegment] | Iterable[CutSegment] | None = None,
    *,
    media_duration: float | None = None,
) -> Path:
    """Save a project.  *media_file_name* may also be an :class:`LlcProject`."""

    if isinstance(media_file_name, LlcProject):
        if segments is not None:
            raise LosslessEdlError("传入 LlcProject 时不能再指定片段。")
        project = media_file_name
        duration = project.media_duration if media_duration is None else media_duration
        text = dump_llc(
            project.media_file_name,
            project.segments,
            media_duration=duration,
        )
    else:
        if segments is None:
            raise LosslessEdlError("保存 LLC 需要片段列表。")
        text = dump_llc(media_file_name, segments, media_duration=media_duration)
    return write_text_file(path, text)


def save_llc_project(path: str | os.PathLike[str], project: LlcProject) -> Path:
    if not isinstance(project, LlcProject):
        raise LosslessEdlError("project 必须是 LlcProject。")
    return save_llc(path, project)


def dump_llc_project(project: LlcProject) -> str:
    if not isinstance(project, LlcProject):
        raise LosslessEdlError("project 必须是 LlcProject。")
    return dump_llc(
        project.media_file_name,
        project.segments,
        media_duration=project.media_duration,
    )


load_llc_project = load_llc
parse_llc_project = parse_llc


def _delimiter(value: str) -> str:
    if value == ",":
        return ","
    if value == "\t":
        return "\t"
    raise LosslessEdlError("分隔符只能是逗号或制表符。")


def _escape_tag_component(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("=", "\\=")
        .replace(";", "\\;")
        .replace("|", "\\|")
    )


def _unescape_tag_component(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            result.append(value[index + 1])
            index += 2
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _format_tag_entry(key: str, value: str) -> str:
    escaped_key = _escape_tag_component(key)
    if value == "":
        return escaped_key
    return f"{escaped_key}={_escape_tag_component(value)}"


def _split_tags(value: str) -> dict[str, str]:
    entries: list[str] = []
    current: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            current.append("\\")
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char in _TAG_SEPARATORS:
            entries.append("".join(current))
            current = []
            continue
        current.append(char)
    if escaped:
        current.append("\\")
    entries.append("".join(current))

    tags: dict[str, str] = {}
    for entry in entries:
        if not entry.strip():
            continue
        equal = -1
        escaped = False
        for index, char in enumerate(entry):
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "=":
                equal = index
                break
        if equal >= 0:
            key_text = entry[:equal]
            value_text = entry[equal + 1:]
        else:
            key_text = entry
            value_text = ""
        key = _unescape_tag_component(key_text).strip()
        tag_value = _unescape_tag_component(value_text).strip()
        if not key:
            raise LosslessEdlError("标签的键不能为空。")
        if key in tags:
            raise LosslessEdlError(f"标签“{key}”重复。")
        tags[key] = tag_value
    return tags


def _selected_text(value: bool) -> str:
    return "1" if value else "0"


def _parse_selected(value: str, label: str) -> bool:
    text = value.strip().lower()
    if not text:
        return True
    if text in _SELECTED_TRUE:
        return True
    if text in _SELECTED_FALSE:
        return False
    raise LosslessEdlError(f"{label}的 selected 无法识别：{value}。")


def _header_key(value: str) -> str:
    return value.strip().lstrip("\ufeff").lower().replace("-", "_").replace(" ", "_")


def _csv_header_map(row: list[str]) -> dict[str, int] | None:
    mapped: dict[str, int] = {}
    duplicates: list[str] = []
    for index, cell in enumerate(row):
        field_name = _CSV_HEADERS.get(_header_key(cell))
        if field_name is None:
            continue
        if field_name in mapped:
            duplicates.append(cell)
            continue
        mapped[field_name] = index
    if not mapped:
        return None
    if "start" not in mapped or "end" not in mapped:
        first_field = _CSV_HEADERS.get(_header_key(row[0])) if row else None
        if first_field in ("start", "end"):
            raise LosslessEdlError("CSV 表头必须同时包含 start 和 end。")
        return None
    if duplicates:
        raise LosslessEdlError(f"CSV 表头重复出现“{duplicates[0]}”。")
    return mapped


def _cell(row: list[str], index: int | None, label: str) -> str:
    if index is None:
        return ""
    if index >= len(row):
        return ""
    return row[index]


def parse_delimited(
    text: str,
    *,
    delimiter: str = ",",
    media_duration: float | None = None,
) -> list[CutSegment]:
    """Parse header-optional CSV/TSV cut lists."""

    delimiter = _delimiter(delimiter)
    if not isinstance(text, str):
        raise LosslessEdlError("片段表格内容必须是文字。")
    text = text.lstrip("\ufeff")
    if not text.strip():
        return []
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True))
    except csv.Error as exc:
        raise LosslessEdlError(f"片段表格格式损坏：{exc}。") from exc

    meaningful = [row for row in rows if any(cell.strip() for cell in row)]
    if not meaningful:
        return []
    if len(meaningful) > MAX_SEGMENTS + 1:
        raise LosslessEdlError(f"片段数量不能超过 {MAX_SEGMENTS}。")

    header = _csv_header_map(meaningful[0])
    header_width = len(meaningful[0]) if header is not None else 0
    data_rows = meaningful[1:] if header is not None else meaningful

    segments: list[CutSegment] = []
    for offset, row in enumerate(data_rows, start=1):
        if header is None:
            while row and not row[-1].strip():
                row = row[:-1]
            if len(row) < 2:
                raise LosslessEdlError(f"片段表格第 {offset} 行至少需要开始和结束两列。")
            if len(row) > 5 and any(cell.strip() for cell in row[5:]):
                raise LosslessEdlError(f"片段表格第 {offset} 行包含太多列。")
            start_text = _cell(row, 0, "开始")
            end_text = _cell(row, 1, "结束")
            name_text = _cell(row, 2, "名称")
            tags_text = _cell(row, 3, "标签")
            selected_text = _cell(row, 4, "选中")
        else:
            if len(row) > header_width and any(
                cell.strip() for cell in row[header_width:]
            ):
                raise LosslessEdlError(f"片段表格第 {offset} 行包含表头之外的列。")
            start_text = _cell(row, header.get("start"), "开始")
            end_text = _cell(row, header.get("end"), "结束")
            name_text = _cell(row, header.get("name"), "名称")
            tags_text = _cell(row, header.get("tags"), "标签")
            selected_text = _cell(row, header.get("selected"), "选中")
        label = f"第 {offset} 段"
        if not start_text.strip() or not end_text.strip():
            raise LosslessEdlError(f"{label}缺少开始或结束时间。")
        try:
            start = parse_timecode(start_text)
            end = parse_timecode(end_text)
        except LosslessEdlError as exc:
            raise LosslessEdlError(f"{label}时间码错误：{exc}") from exc
        name = name_text.strip()
        tags = _split_tags(tags_text)
        selected = _parse_selected(selected_text, label)
        segments.append(
            _make_segment(start, end, name=name, tags=tags, selected=selected)
        )
    validate_segments(segments, media_duration=media_duration)
    return segments


def parse_csv(text: str, *, media_duration: float | None = None) -> list[CutSegment]:
    return parse_delimited(text, delimiter=",", media_duration=media_duration)


def parse_tsv(text: str, *, media_duration: float | None = None) -> list[CutSegment]:
    return parse_delimited(text, delimiter="\t", media_duration=media_duration)


def dump_delimited(
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    *,
    delimiter: str = ",",
    include_header: bool = True,
    media_duration: float | None = None,
) -> str:
    delimiter = _delimiter(delimiter)
    items = validate_segments(segments, media_duration=media_duration)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n")
    if include_header:
        writer.writerow(["start", "end", "name", "tags", "selected"])
    for index, segment in enumerate(items, start=1):
        label = f"第 {index} 段"
        name = _read_name(_segment_field(segment, "name", label), label)
        tags = _read_tags(_segment_field(segment, "tags", label), label)
        selected = _read_selected(_segment_field(segment, "selected", label), label)
        writer.writerow(
            [
                format_seconds(_segment_field(segment, "start", label)),
                format_seconds(_segment_field(segment, "end", label)),
                name,
                ";".join(_format_tag_entry(key, value) for key, value in tags.items()),
                _selected_text(selected),
            ]
        )
    return buffer.getvalue()


def dump_csv(
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    *,
    include_header: bool = True,
    media_duration: float | None = None,
) -> str:
    return dump_delimited(
        segments,
        delimiter=",",
        include_header=include_header,
        media_duration=media_duration,
    )


def dump_tsv(
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    *,
    include_header: bool = True,
    media_duration: float | None = None,
) -> str:
    return dump_delimited(
        segments,
        delimiter="\t",
        include_header=include_header,
        media_duration=media_duration,
    )


def parse_mplayer_edl(
    text: str,
    *,
    media_duration: float | None = None,
) -> list[CutSegment]:
    """Parse the common MPlayer EDL cut subset (``start end 0``)."""

    if not isinstance(text, str):
        raise LosslessEdlError("MPlayer EDL 内容必须是文字。")
    text = text.lstrip("\ufeff")
    segments: list[CutSegment] = []
    for line_number, raw_line in enumerate(_LINE_SPLIT.split(text), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) not in (2, 3):
            raise LosslessEdlError(
                f"MPlayer EDL 第 {line_number} 行应为“开始 结束 [0]”。"
            )
        try:
            start = parse_timecode(fields[0])
            end = parse_timecode(fields[1])
        except LosslessEdlError as exc:
            raise LosslessEdlError(
                f"MPlayer EDL 第 {line_number} 行时间码错误：{exc}"
            ) from exc
        if len(fields) == 3:
            try:
                action = float(fields[2])
            except ValueError as exc:
                raise LosslessEdlError(
                    f"MPlayer EDL 第 {line_number} 行的操作码必须是 0（剪切）。"
                ) from exc
            if not math.isfinite(action) or not action.is_integer() or action != 0:
                raise LosslessEdlError(
                    f"MPlayer EDL 第 {line_number} 行只支持剪切操作码 0，不支持静音。"
                )
        segments.append(_make_segment(start, end))
        if len(segments) > MAX_SEGMENTS:
            raise LosslessEdlError(f"片段数量不能超过 {MAX_SEGMENTS}。")
    validate_segments(segments, media_duration=media_duration)
    return segments


def dump_mplayer_edl(
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    *,
    media_duration: float | None = None,
) -> str:
    items = validate_segments(segments, media_duration=media_duration)
    lines = []
    for index, segment in enumerate(items, start=1):
        label = f"第 {index} 段"
        start = format_seconds(_segment_field(segment, "start", label))
        end = format_seconds(_segment_field(segment, "end", label))
        lines.append(f"{start} {end} 0")
    return "\n".join(lines) + ("\n" if lines else "")


def _srt_block_text(lines: list[str]) -> str:
    return "\n".join(lines).strip()


def parse_srt(
    text: str,
    *,
    media_duration: float | None = None,
) -> list[CutSegment]:
    """Parse SRT subtitles as segments, accepting comma or dot milliseconds."""

    if not isinstance(text, str):
        raise LosslessEdlError("SRT 内容必须是文字。")
    text = text.lstrip("\ufeff")
    normalized = _LINE_SPLIT.sub("\n", text)
    if not normalized.strip():
        return []
    blocks = re.split(r"\n[ \t]*\n", normalized.strip())
    if len(blocks) > MAX_SEGMENTS:
        raise LosslessEdlError(f"SRT 字幕块不能超过 {MAX_SEGMENTS} 个。")
    segments: list[CutSegment] = []
    seen_indices: set[int] = set()
    saw_index = False
    saw_without_index = False
    for block_number, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        position = 0
        index_value: int | None = None
        if lines and re.fullmatch(r"\s*\d+\s*", lines[0]):
            index_value = int(lines[0].strip())
            if index_value <= 0:
                raise LosslessEdlError(f"SRT 第 {block_number} 个字幕块序号必须大于 0。")
            if index_value in seen_indices:
                raise LosslessEdlError(f"SRT 第 {block_number} 个字幕块序号重复。")
            seen_indices.add(index_value)
            saw_index = True
            position = 1
        else:
            saw_without_index = True
        if position >= len(lines):
            raise LosslessEdlError(f"SRT 第 {block_number} 个字幕块缺少时间轴。")
        match = _SRT_TIMING.match(lines[position])
        if not match:
            raise LosslessEdlError(
                f"SRT 第 {block_number} 个字幕块时间轴损坏，"
                "应使用 HH:MM:SS,mmm --> HH:MM:SS,mmm。"
            )
        tail = match.group(9).strip()
        if tail and not all(_SRT_POSITION_TOKEN.match(token) for token in tail.split()):
            raise LosslessEdlError(f"SRT 第 {block_number} 个字幕块时间轴后有多余内容。")
        start = _srt_seconds(match.group(1), match.group(2), match.group(3), match.group(4))
        end = _srt_seconds(match.group(5), match.group(6), match.group(7), match.group(8))
        name = _srt_block_text(lines[position + 1:])
        segments.append(_make_segment(start, end, name=name))
    if saw_index and saw_without_index:
        raise LosslessEdlError("SRT 不能混用带序号和不带序号的字幕块。")
    validate_segments(segments, media_duration=media_duration)
    return segments


def _srt_text(value: str, index: int) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n[ \t]*\n+", "\n", text).strip()
    return text or f"片段 {index}"


def dump_srt(
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    *,
    media_duration: float | None = None,
) -> str:
    items = validate_segments(segments, media_duration=media_duration)
    blocks: list[str] = []
    for index, segment in enumerate(items, start=1):
        label = f"第 {index} 段"
        start = format_srt_timecode(_segment_field(segment, "start", label))
        end = format_srt_timecode(_segment_field(segment, "end", label))
        name = _read_name(_segment_field(segment, "name", label), label)
        blocks.append(f"{index}\n{start} --> {end}\n{_srt_text(name, index)}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _chapter_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\r", " ").replace("\n", " ")).strip()


def parse_chapters(text: str) -> list[Chapter]:
    """Parse YouTube-style chapter lines (``0:00 Title``)."""

    if not isinstance(text, str):
        raise LosslessEdlError("章节内容必须是文字。")
    text = text.lstrip("\ufeff")
    chapters: list[Chapter] = []
    previous_ms: int | None = None
    for line_number, raw_line in enumerate(_LINE_SPLIT.split(text), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        match = _CHAPTER_LINE.match(line)
        if not match:
            raise LosslessEdlError(
                f"章节第 {line_number} 行格式错误，应为“0:00 标题”。"
            )
        timestamp, name = match.groups()
        try:
            start = parse_timecode(timestamp)
        except LosslessEdlError as exc:
            raise LosslessEdlError(
                f"章节第 {line_number} 行时间码错误：{exc}"
            ) from exc
        start_ms = _milliseconds(start, f"章节第 {line_number} 行开始时间")
        if previous_ms is not None and start_ms <= previous_ms:
            raise LosslessEdlError(
                f"章节第 {line_number} 行的开始时间必须晚于上一章。"
            )
        previous_ms = start_ms
        chapters.append(Chapter(start=start, end=None, title=(name or "").strip()))
        if len(chapters) > MAX_SEGMENTS:
            raise LosslessEdlError(f"章节数量不能超过 {MAX_SEGMENTS}。")
    return chapters


def chapters_to_segments(
    chapters: Sequence[Chapter] | Iterable[Chapter],
    media_duration: float | None = None,
) -> list[CutSegment]:
    """Convert chapter starts into adjacent cut segments.

    A chapter without an explicit end inherits the next chapter start.  The
    last chapter still needs *media_duration* because a chapter list has no
    end marker for it.
    """

    try:
        items = list(chapters)
    except TypeError as exc:
        raise LosslessEdlError("章节列表格式错误。") from exc
    if not items:
        _validate_duration(media_duration)
        return []
    if len(items) > MAX_SEGMENTS:
        raise LosslessEdlError(f"章节数量不能超过 {MAX_SEGMENTS}。")
    for index, chapter in enumerate(items, start=1):
        if not isinstance(chapter, Chapter):
            raise LosslessEdlError(f"第 {index} 个章节不是 Chapter 对象。")
    duration = _validate_duration(media_duration)
    try:
        converted = _core_chapters_to_segments(
            items,
            media_duration=duration,
            selected=True,
        )
    except (TypeError, ValueError) as exc:
        raise LosslessEdlError(str(exc)) from exc
    segments = list(converted)
    if any(segment.end is None for segment in segments):
        raise LosslessEdlError(
            "从章节创建片段需要视频总时长，用于确定最后一个章节的结束时间。"
        )
    return validate_segments(segments, media_duration=duration)


def segments_to_chapters(
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    *,
    media_duration: float | None = None,
) -> list[Chapter]:
    items = validate_segments(segments, media_duration=media_duration)
    ordered = sorted(
        items,
        key=lambda segment: (
            _milliseconds(_segment_field(segment, "start", "片段"), "片段开始时间"),
            _milliseconds(_segment_field(segment, "end", "片段"), "片段结束时间"),
        ),
    )
    chapters: list[Chapter] = []
    for index, segment in enumerate(ordered, start=1):
        label = f"第 {index} 段"
        start_ms = _milliseconds(_segment_field(segment, "start", label), f"{label}的开始时间")
        end_ms = _milliseconds(_segment_field(segment, "end", label), f"{label}的结束时间")
        name = _read_name(_segment_field(segment, "name", label), label)
        chapters.append(Chapter(start=start_ms / 1000, end=end_ms / 1000, title=name))
    return chapters


def _youtube_timestamp(value: Any) -> str:
    milliseconds = _milliseconds(value)
    seconds, fraction = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        result = f"{hours}:{minutes:02d}:{seconds:02d}"
    else:
        result = f"{minutes}:{seconds:02d}"
    if fraction:
        result += f".{fraction:03d}"
    return result


def dump_chapters(
    chapters: Sequence[Chapter] | Iterable[Chapter],
) -> str:
    try:
        items = list(chapters)
    except TypeError as exc:
        raise LosslessEdlError("章节列表格式错误。") from exc
    if len(items) > MAX_SEGMENTS:
        raise LosslessEdlError(f"章节数量不能超过 {MAX_SEGMENTS}。")
    lines: list[str] = []
    previous_ms: int | None = None
    for index, chapter in enumerate(items, start=1):
        if not isinstance(chapter, Chapter):
            raise LosslessEdlError(f"第 {index} 个章节不是 Chapter 对象。")
        start_ms = _milliseconds(chapter.start, f"第 {index} 个章节的开始时间")
        if previous_ms is not None and start_ms <= previous_ms:
            raise LosslessEdlError(f"第 {index} 个章节的开始时间必须晚于上一章。")
        previous_ms = start_ms
        title = chapter.title
        if not isinstance(title, str):
            raise LosslessEdlError(f"第 {index} 个章节的标题必须是文字。")
        name = _chapter_name(title) or f"片段 {index}"
        lines.append(f"{_youtube_timestamp(start_ms / 1000)} {name}")
    return "\n".join(lines) + ("\n" if lines else "")


def dump_youtube_chapters(
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    *,
    media_duration: float | None = None,
) -> str:
    return dump_chapters(
        segments_to_chapters(segments, media_duration=media_duration)
    )


def parse_youtube_chapters(text: str) -> list[Chapter]:
    return parse_chapters(text)


def import_chapters(
    text: str,
    media_duration: float | None = None,
) -> list[CutSegment]:
    return chapters_to_segments(parse_chapters(text), media_duration)


def export_chapters(
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    *,
    media_duration: float | None = None,
) -> str:
    return dump_youtube_chapters(segments, media_duration=media_duration)


def normalize_format(value: str | None) -> str:
    if value is None:
        raise LosslessEdlError("没有指定片段格式。")
    text = str(value).strip().lower().lstrip(".")
    normalized = _FORMAT_ALIASES.get(text)
    if normalized is None:
        raise LosslessEdlError(f"不支持的片段格式：{value}。")
    return normalized


def format_from_path(path: str | os.PathLike[str]) -> str | None:
    try:
        suffix = Path(path).suffix.lower()
    except TypeError as exc:
        raise LosslessEdlError(f"无效的文件路径：{path!r}") from exc
    return _EXTENSION_FORMATS.get(suffix)


def _strip_leading_comments(text: str) -> str:
    index = 0
    length = len(text)
    while index < length:
        if text[index].isspace():
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            if newline < 0:
                return ""
            index = newline + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                return text[index:]
            index = end + 2
            continue
        break
    return text[index:]


def _looks_like_mplayer_line(line: str) -> bool:
    fields = line.split()
    if len(fields) not in (2, 3):
        return False
    try:
        parse_timecode(fields[0])
        parse_timecode(fields[1])
    except LosslessEdlError:
        return False
    if len(fields) == 3:
        try:
            action = float(fields[2])
        except ValueError:
            return False
        if not math.isfinite(action) or not action.is_integer() or action not in (0, 1):
            return False
    return True


def detect_format(text: str) -> str:
    """Guess the format of cut-list text from its first useful lines."""

    if not isinstance(text, str):
        raise LosslessEdlError("片段内容必须是文字。")
    text = text.lstrip("\ufeff")
    stripped = text.strip()
    if not stripped:
        raise LosslessEdlError("片段内容为空，无法判断格式。")
    if _strip_leading_comments(text).startswith("{"):
        return "llc"

    lines = [
        line.strip()
        for line in _LINE_SPLIT.split(text)
        if line.strip() and not line.lstrip().startswith(("#", "//"))
    ]
    if not lines:
        raise LosslessEdlError("片段内容为空，无法判断格式。")
    if any(_SRT_TIMING.match(line) for line in lines):
        return "srt"
    first = lines[0]
    if "\t" in first:
        return "tsv"
    first_without_comment = first.split("#", 1)[0].strip()
    if _looks_like_mplayer_line(first_without_comment):
        return "mplayer"
    if _CHAPTER_LINE.match(first) and _looks_like_chapter_timestamp(first):
        return "chapters"
    if "," in first:
        return "csv"
    raise LosslessEdlError("无法判断片段格式，请显式指定 llc/csv/tsv/mplayer/srt/chapters。")


def _looks_like_chapter_timestamp(line: str) -> bool:
    match = _CHAPTER_LINE.match(line)
    if not match:
        return False
    return ":" in (match.group(1) or "")


def parse_segments(
    text: str,
    fmt: str | None = None,
    *,
    media_duration: float | None = None,
) -> list[CutSegment]:
    """Parse any supported cut-list format into segments."""

    selected_format = normalize_format(fmt) if fmt is not None else detect_format(text)
    if selected_format == "llc":
        project = parse_llc(text)
        if media_duration is not None:
            validate_segments(project.segments, media_duration=media_duration)
        return project.segments
    if selected_format == "csv":
        return parse_csv(text, media_duration=media_duration)
    if selected_format == "tsv":
        return parse_tsv(text, media_duration=media_duration)
    if selected_format == "mplayer":
        return parse_mplayer_edl(text, media_duration=media_duration)
    if selected_format == "srt":
        return parse_srt(text, media_duration=media_duration)
    if selected_format == "chapters":
        return chapters_to_segments(parse_chapters(text), media_duration)
    raise LosslessEdlError(f"不支持的片段格式：{selected_format}。")


def parse_document(
    text: str,
    fmt: str | None = None,
    *,
    media_file_name: str = "",
    media_duration: float | None = None,
) -> LlcProject:
    """Parse any format and keep project metadata when it exists."""

    selected_format = normalize_format(fmt) if fmt is not None else detect_format(text)
    if selected_format == "llc":
        project = parse_llc(text)
        if media_duration is not None:
            validate_segments(project.segments, media_duration=media_duration)
            if project.media_duration is None:
                project.media_duration = _validate_duration(media_duration)
        return project
    segments = parse_segments(text, selected_format, media_duration=media_duration)
    media_name = validate_media_file_name(media_file_name) if media_file_name else ""
    return LlcProject(media_name, segments, _validate_duration(media_duration))


def export_segments(
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    fmt: str,
    *,
    media_file_name: str | None = None,
    media_duration: float | None = None,
    include_header: bool = True,
) -> str:
    selected_format = normalize_format(fmt)
    if selected_format == "llc":
        if not media_file_name:
            raise LosslessEdlError("导出 LLC 需要 mediaFileName。")
        return dump_llc(
            media_file_name,
            segments,
            media_duration=media_duration,
        )
    if selected_format == "csv":
        return dump_csv(
            segments,
            include_header=include_header,
            media_duration=media_duration,
        )
    if selected_format == "tsv":
        return dump_tsv(
            segments,
            include_header=include_header,
            media_duration=media_duration,
        )
    if selected_format == "mplayer":
        return dump_mplayer_edl(segments, media_duration=media_duration)
    if selected_format == "srt":
        return dump_srt(segments, media_duration=media_duration)
    if selected_format == "chapters":
        return dump_youtube_chapters(segments, media_duration=media_duration)
    raise LosslessEdlError(f"不支持的片段格式：{selected_format}。")


def load_segments(
    path: str | os.PathLike[str],
    fmt: str | None = None,
    *,
    media_duration: float | None = None,
) -> list[CutSegment]:
    text = read_text_file(path)
    selected_format = fmt or format_from_path(path)
    if selected_format is None:
        selected_format = detect_format(text)
    return parse_segments(text, selected_format, media_duration=media_duration)


def load_document(
    path: str | os.PathLike[str],
    fmt: str | None = None,
    *,
    media_file_name: str = "",
    media_duration: float | None = None,
) -> LlcProject:
    text = read_text_file(path)
    selected_format = fmt or format_from_path(path)
    if selected_format is None:
        selected_format = detect_format(text)
    return parse_document(
        text,
        selected_format,
        media_file_name=media_file_name,
        media_duration=media_duration,
    )


def save_segments(
    path: str | os.PathLike[str],
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    fmt: str | None = None,
    *,
    media_file_name: str | None = None,
    media_duration: float | None = None,
    include_header: bool = True,
) -> Path:
    selected_format = fmt or format_from_path(path)
    if selected_format is None:
        raise LosslessEdlError("无法从扩展名判断片段格式，请显式指定。")
    text = export_segments(
        segments,
        selected_format,
        media_file_name=media_file_name,
        media_duration=media_duration,
        include_header=include_header,
    )
    return write_text_file(path, text)


def load_csv(
    path: str | os.PathLike[str],
    *,
    media_duration: float | None = None,
) -> list[CutSegment]:
    return load_segments(path, "csv", media_duration=media_duration)


def save_csv(
    path: str | os.PathLike[str],
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    *,
    include_header: bool = True,
    media_duration: float | None = None,
) -> Path:
    return save_segments(
        path,
        segments,
        "csv",
        include_header=include_header,
        media_duration=media_duration,
    )


def load_tsv(
    path: str | os.PathLike[str],
    *,
    media_duration: float | None = None,
) -> list[CutSegment]:
    return load_segments(path, "tsv", media_duration=media_duration)


def save_tsv(
    path: str | os.PathLike[str],
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    *,
    include_header: bool = True,
    media_duration: float | None = None,
) -> Path:
    return save_segments(
        path,
        segments,
        "tsv",
        include_header=include_header,
        media_duration=media_duration,
    )


def load_mplayer_edl(
    path: str | os.PathLike[str],
    *,
    media_duration: float | None = None,
) -> list[CutSegment]:
    return load_segments(path, "mplayer", media_duration=media_duration)


def save_mplayer_edl(
    path: str | os.PathLike[str],
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    *,
    media_duration: float | None = None,
) -> Path:
    return save_segments(path, segments, "mplayer", media_duration=media_duration)


def load_srt(
    path: str | os.PathLike[str],
    *,
    media_duration: float | None = None,
) -> list[CutSegment]:
    return load_segments(path, "srt", media_duration=media_duration)


def save_srt(
    path: str | os.PathLike[str],
    segments: Sequence[CutSegment] | Iterable[CutSegment],
    *,
    media_duration: float | None = None,
) -> Path:
    return save_segments(path, segments, "srt", media_duration=media_duration)


def load_chapters(path: str | os.PathLike[str]) -> list[Chapter]:
    return parse_chapters(read_text_file(path))


def save_chapters(
    path: str | os.PathLike[str],
    chapters: Sequence[Chapter] | Iterable[Chapter],
) -> Path:
    return write_text_file(path, dump_chapters(chapters))



import_csv = parse_csv
export_csv = dump_csv
import_tsv = parse_tsv
export_tsv = dump_tsv
import_mplayer_edl = parse_mplayer_edl
export_mplayer_edl = dump_mplayer_edl
import_edl = parse_mplayer_edl
export_edl = dump_mplayer_edl
import_srt = parse_srt
export_srt = dump_srt
import_youtube_chapters = import_chapters
export_youtube_chapters = dump_youtube_chapters


__all__ = [
    "Chapter",
    "CutSegment",
    "LlcFormatError",
    "LlcProject",
    "LosslessEdlError",
    "SegmentValidationError",
    "LLC_VERSION",
    "SUPPORTED_FORMATS",
    "chapters_to_segments",
    "detect_format",
    "dump_chapters",
    "dump_csv",
    "dump_delimited",
    "dump_llc",
    "dump_llc_project",
    "dump_mplayer_edl",
    "dump_srt",
    "dump_tsv",
    "dump_youtube_chapters",
    "export_chapters",
    "export_csv",
    "export_edl",
    "export_mplayer_edl",
    "export_segments",
    "export_srt",
    "export_tsv",
    "export_youtube_chapters",
    "format_from_path",
    "format_seconds",
    "format_srt_timecode",
    "format_timecode",
    "import_chapters",
    "import_csv",
    "import_edl",
    "import_mplayer_edl",
    "import_srt",
    "import_tsv",
    "import_youtube_chapters",
    "load_document",
    "load_llc",
    "load_llc_project",
    "load_segments",
    "load_chapters",
    "load_csv",
    "load_mplayer_edl",
    "load_srt",
    "load_tsv",
    "normalize_format",
    "parse_chapters",
    "parse_csv",
    "parse_delimited",
    "parse_document",
    "parse_llc",
    "parse_llc_project",
    "parse_mplayer_edl",
    "parse_segments",
    "parse_srt",
    "parse_timecode",
    "parse_tsv",
    "parse_youtube_chapters",
    "read_text_file",
    "resolve_media_path",
    "save_llc",
    "save_llc_project",
    "save_segments",
    "save_chapters",
    "save_csv",
    "save_mplayer_edl",
    "save_srt",
    "save_tsv",
    "segments_to_chapters",
    "validate_media_file_name",
    "validate_segments",
    "write_text_file",
]
