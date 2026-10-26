"""Validated subtitle tracks, safe SRT import, and portable ASS/SRT export.

Cue times always refer to the original programme clock.  ``offset`` is applied
only when displaying/exporting a track; exporting a range never edits the cues.
Style sizes and margins use a 1920 x 1080 design canvas and scale with output.
No HTML or ASS override code from an imported subtitle is executed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import html
import math
from pathlib import Path
import re
from typing import Mapping


MAX_SRT_BYTES = 16 * 1024 * 1024
MAX_SUBTITLE_CUES = 100_000
MAX_CUE_TEXT = 16_384
MAX_SUBTITLE_TIME = 10_000_000.0
_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_STAMP = r"(?:\d{1,4}:)?\d{1,2}:\d{2}[,.]\d{1,3}"
_TIMING = re.compile(rf"^\s*({_STAMP})\s*-->\s*({_STAMP})(?:\s+(?:[XY][12]:\d+\s*)+)?\s*$")
_BASIC_TAG = re.compile(r"</?(?:b|i|u|s)\s*>|</?font(?:\s+[^<>]*)?>", re.IGNORECASE)
_BREAK_TAG = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BAD_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(slots=True)
class SubtitleCue:
    id: str
    start: float
    end: float
    text: str


@dataclass(slots=True)
class SubtitleStyle:
    font_family: str = "Microsoft YaHei"
    font_size: float = 48
    color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_width: float = 2
    shadow: float = 1
    bold: bool = False
    italic: bool = False
    alignment: int = 2
    margin_x: int = 60
    margin_y: int = 60
    box: bool = False
    background_color: str = "#000000"
    background_opacity: float = 0


@dataclass(slots=True)
class SubtitleTrack:
    cues: list[SubtitleCue] = field(default_factory=list)
    style: SubtitleStyle = field(default_factory=SubtitleStyle)
    offset: float = 0
    enabled: bool = True
    burn_in: bool = True
    source_path: str = ""


def _number(value, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须是数字")
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{label}须在 {minimum:g} 至 {maximum:g} 之间")
    return value


def _integer(value, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label}须为 {minimum} 至 {maximum} 的整数")
    return value


def validate_subtitle_style(style: SubtitleStyle) -> None:
    if not isinstance(style, SubtitleStyle):
        raise ValueError("字幕样式格式无效")
    if (not isinstance(style.font_family, str) or not style.font_family.strip()
            or len(style.font_family) > 128
            or any(character in style.font_family for character in ",\r\n{}\\")
            or _BAD_CONTROL.search(style.font_family)):
        raise ValueError("字幕字体名称无效，不能包含逗号、换行或 ASS 控制字符")
    _number(style.font_size, "字幕字号", 8, 300)
    _number(style.outline_width, "字幕描边", 0, 20)
    _number(style.shadow, "字幕阴影", 0, 20)
    _number(style.background_opacity, "字幕背景不透明度", 0, 1)
    for label, value in (("文字颜色", style.color), ("描边颜色", style.outline_color),
                         ("背景颜色", style.background_color)):
        if not isinstance(value, str) or not _COLOR.fullmatch(value):
            raise ValueError(f"{label}须为 #RRGGBB 格式")
    _integer(style.alignment, "字幕位置", 1, 9)
    _integer(style.margin_x, "字幕水平边距", 0, 960)
    _integer(style.margin_y, "字幕垂直边距", 0, 1080)
    for name in ("bold", "italic", "box"):
        if not isinstance(getattr(style, name), bool):
            raise ValueError(f"字幕 {name} 须为布尔值")


def validate_subtitle_track(track: SubtitleTrack) -> None:
    if not isinstance(track, SubtitleTrack):
        raise ValueError("字幕轨道格式无效")
    validate_subtitle_style(track.style)
    _number(track.offset, "字幕偏移", -MAX_SUBTITLE_TIME, MAX_SUBTITLE_TIME)
    if not isinstance(track.enabled, bool) or not isinstance(track.burn_in, bool):
        raise ValueError("字幕启用与烧录选项须为布尔值")
    if (not isinstance(track.source_path, str) or len(track.source_path) > 32767
            or "\x00" in track.source_path):
        raise ValueError("字幕来源路径无效")
    if not isinstance(track.cues, list) or len(track.cues) > MAX_SUBTITLE_CUES:
        raise ValueError(f"字幕不能超过 {MAX_SUBTITLE_CUES} 条")
    ids = set()
    for index, cue in enumerate(track.cues, 1):
        if not isinstance(cue, SubtitleCue):
            raise ValueError(f"第 {index} 条字幕格式无效")
        if not isinstance(cue.id, str) or not cue.id or len(cue.id) > 128 or cue.id in ids:
            raise ValueError(f"第 {index} 条字幕编号为空、重复或过长")
        ids.add(cue.id)
        start = _number(cue.start, f"第 {index} 条字幕起点", 0, MAX_SUBTITLE_TIME)
        end = _number(cue.end, f"第 {index} 条字幕终点", 0, MAX_SUBTITLE_TIME)
        if end <= start:
            raise ValueError(f"第 {index} 条字幕结束时间必须晚于开始时间")
        if (not isinstance(cue.text, str) or not cue.text.strip()
                or len(cue.text) > MAX_CUE_TEXT or _BAD_CONTROL.search(cue.text)):
            raise ValueError(f"第 {index} 条字幕文字为空、过长或包含无效控制字符")


def _decode_srt(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise ValueError("SRT 的 UTF-16 编码损坏") from exc
    # Some Windows applications write UTF-16 without its byte-order mark.
    if b"\x00" in data[:128]:
        odd = data[1:128:2].count(0)
        even = data[:128:2].count(0)
        encoding = "utf-16-le" if odd > even else "utf-16-be"
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as exc:
            raise ValueError("SRT 的 UTF-16 编码损坏") from exc
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("SRT 编码无法识别；请另存为 UTF-8、UTF-16 或 GB18030")


def _seconds(stamp: str) -> float:
    main, fraction = re.split(r"[,.]", stamp)
    parts = [int(part) for part in main.split(":")]
    if len(parts) == 2:
        hours, minutes, seconds = 0, *parts
    else:
        hours, minutes, seconds = parts
    if minutes >= 60 or seconds >= 60:
        raise ValueError("分钟和秒必须小于 60")
    return hours * 3600 + minutes * 60 + seconds + int(fraction.ljust(3, "0")) / 1000


def _plain_text(value: str) -> str:
    # Only a tiny presentation-tag allowlist is stripped. All other markup is
    # ordinary visible text; neither an HTML parser nor an ASS evaluator runs it.
    return html.unescape(_BASIC_TAG.sub("", _BREAK_TAG.sub("\n", value)))


def parse_srt(path: str | Path) -> list[SubtitleCue]:
    path = Path(path)
    if path.stat().st_size > MAX_SRT_BYTES:
        raise ValueError("SRT 文件过大，最多支持 16 MiB")
    with path.open("rb") as stream:
        data = stream.read(MAX_SRT_BYTES + 1)
    if len(data) > MAX_SRT_BYTES:
        raise ValueError("SRT 文件过大，最多支持 16 MiB")
    text = _decode_srt(data).replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks = re.split(r"\n[\t ]*\n", text.strip())
    cues = []
    for block_number, block in enumerate(blocks, 1):
        if not block.strip():
            continue
        if len(cues) >= MAX_SUBTITLE_CUES:
            raise ValueError(f"SRT 字幕不能超过 {MAX_SUBTITLE_CUES} 条")
        lines = block.split("\n")
        if re.fullmatch(r"\s*\d+\s*", lines[0]):
            lines = lines[1:]
        if not lines or not (match := _TIMING.fullmatch(lines[0])):
            raise ValueError(f"SRT 第 {block_number} 段时间格式无效，应为 00:00:01,000 --> 00:00:02,000")
        try:
            start, end = (_seconds(stamp) for stamp in match.groups())
        except ValueError as exc:
            raise ValueError(f"SRT 第 {block_number} 段时间无效：{exc}") from exc
        cue_text = _plain_text("\n".join(lines[1:]).strip())
        cues.append(SubtitleCue(f"srt-{len(cues) + 1:06d}", start, end, cue_text))
    if not cues:
        raise ValueError("SRT 文件中没有字幕")
    validate_subtitle_track(SubtitleTrack(cues=cues))
    return sorted(cues, key=lambda cue: (cue.start, cue.end))


def subtitle_track_to_dict(track: SubtitleTrack) -> dict:
    validate_subtitle_track(track)
    return asdict(track)


def subtitle_track_from_dict(value: Mapping) -> SubtitleTrack:
    if not isinstance(value, Mapping):
        raise ValueError("字幕轨道数据须为对象")
    if set(value) - {"cues", "style", "offset", "enabled", "burn_in", "source_path"}:
        raise ValueError("字幕轨道包含未知字段")
    try:
        raw_cues = value.get("cues", [])
        if not isinstance(raw_cues, list) or len(raw_cues) > MAX_SUBTITLE_CUES:
            raise ValueError("字幕列表格式无效或条数过多")
        raw_style = value.get("style", {})
        if not isinstance(raw_style, Mapping):
            raise ValueError("字幕样式数据须为对象")
        track = SubtitleTrack(
            cues=[SubtitleCue(**dict(cue)) for cue in raw_cues],
            style=SubtitleStyle(**dict(raw_style)),
            offset=value.get("offset", 0), enabled=value.get("enabled", True),
            burn_in=value.get("burn_in", True), source_path=value.get("source_path", ""),
        )
    except (TypeError, KeyError) as exc:
        raise ValueError("字幕轨道字段无效") from exc
    validate_subtitle_track(track)
    return track


def subtitle_at(track: SubtitleTrack, time: float) -> list[SubtitleCue]:
    """Return all active cues, using half-open intervals and the track offset."""
    if not track.enabled:
        return []
    value = _number(time, "字幕播放位置", -MAX_SUBTITLE_TIME, MAX_SUBTITLE_TIME) - track.offset
    return [cue for cue in track.cues if cue.start <= value < cue.end]


def _export_cues(track: SubtitleTrack, range_start: float, range_end: float | None):
    validate_subtitle_track(track)
    start = _number(range_start, "导出起点", 0, MAX_SUBTITLE_TIME)
    end = MAX_SUBTITLE_TIME if range_end is None else _number(range_end, "导出终点", 0, MAX_SUBTITLE_TIME)
    if end <= start:
        raise ValueError("字幕导出终点必须晚于起点")
    if not track.enabled:
        return []
    result = []
    for cue in track.cues:
        cue_start, cue_end = cue.start + track.offset, cue.end + track.offset
        clipped_start, clipped_end = max(start, cue_start), min(end, cue_end)
        if clipped_end > clipped_start:
            result.append((clipped_start - start, clipped_end - start, cue.text))
    return sorted(result, key=lambda item: (item[0], item[1]))


def _timestamp(ticks: int, precision: int, separator: str) -> str:
    scale = 10 ** precision
    seconds, fraction = divmod(ticks, scale)
    minutes, second = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{second:02d}{separator}{fraction:0{precision}d}"


def _ass_color(color: str, opacity: float = 1) -> str:
    red, green, blue = color[1:3], color[3:5], color[5:7]
    alpha = round((1 - opacity) * 255)
    return f"&H{alpha:02X}{blue}{green}{red}".upper()


def escape_ass_text(value: str) -> str:
    r"""Keep imported text literal; fullwidth punctuation cannot open ASS code.

    ASS has no portable literal-backslash/brace quoting across libass and mpv.
    Replacing just these three punctuation characters avoids override and \N
    injection while retaining readable punctuation in untrusted SRT text.
    """
    value = _plain_text(value).replace("\r\n", "\n").replace("\r", "\n")
    return value.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", r"\N")


def subtitle_filter(ass_path: str | Path) -> str:
    """FFmpeg filter expression with both graph and option escaping applied.

    Pass the returned expression directly in a subprocess argument list, never
    through a shell.  In particular, a Windows drive colon and literal quote in
    a user directory must survive FFmpeg's two successive parsing layers.
    """
    value = str(Path(ass_path).resolve()).replace("\\", "/")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("字幕文件路径包含无效控制字符")
    option = re.sub(r"([\\':])", r"\\\1", value)
    graph = re.sub(r"([\\'\[\],;])", r"\\\1", option)
    return "subtitles=filename=" + graph


def write_ass(track: SubtitleTrack, path: str | Path, width: int, height: int,
              range_start: float = 0, range_end: float | None = None) -> Path:
    cues = _export_cues(track, range_start, range_end)
    _integer(width, "输出宽度", 2, 32768)
    _integer(height, "输出高度", 2, 32768)
    style = track.style
    factor = height / 1080
    margin_x, margin_y = round(style.margin_x * width / 1920), round(style.margin_y * factor)
    background = _ass_color(style.background_color, style.background_opacity)
    outline = background if style.box else _ass_color(style.outline_color)
    values = ["Default", style.font_family, f"{style.font_size * factor:g}",
              _ass_color(style.color), _ass_color(style.color), outline,
              background if style.box else _ass_color(style.background_color, 0.65),
              "-1" if style.bold else "0", "-1" if style.italic else "0", "0", "0",
              "100", "100", "0", "0", "3" if style.box else "1",
              f"{style.outline_width * factor:g}", f"{style.shadow * factor:g}",
              str(style.alignment), str(margin_x), str(margin_x), str(margin_y), "1"]
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
             "ScaledBorderAndShadow: yes", "WrapStyle: 0", "", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
             "Style: " + ",".join(values), "", "[Events]",
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for start, end, text in cues:
        begin = max(0, math.floor(start * 100 + 1e-7))
        finish = max(begin + 1, math.ceil(end * 100 - 1e-7))
        lines.append(f"Dialogue: 0,{_timestamp(begin, 2, '.')},{_timestamp(finish, 2, '.')},Default,,0,0,0,,{escape_ass_text(text)}")
    path = Path(path)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig", newline="\n")
    return path


def write_srt(track: SubtitleTrack, path: str | Path, range_start: float = 0,
              range_end: float | None = None) -> Path:
    blocks = []
    for index, (start, end, text) in enumerate(_export_cues(track, range_start, range_end), 1):
        begin = max(0, round(start * 1000))
        finish = max(begin + 1, round(end * 1000))
        # Plain SRT output deliberately excludes imported presentation tags.
        blocks.append(f"{index}\n{_timestamp(begin, 3, ',')} --> {_timestamp(finish, 3, ',')}\n{_plain_text(text)}")
    path = Path(path)
    path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8-sig", newline="\n")
    return path
