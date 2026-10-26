"""Original grading and transition controls backed by public FFmpeg filters.

These are deliberately simple, portable controls.  A grade describes display
referred adjustments, not a camera colour-management transform or a Resolve
node graph.  Transition times use the programme's unchanged master clock.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
import math
from pathlib import Path
from typing import Mapping

from color_curves import CURVE_CHANNELS, DEFAULT_CURVE, canonical_curve
from portrait_color import portrait_lut_path


@dataclass(slots=True)
class ColorGrade:
    exposure: float = 0.0
    contrast: float = 1.0
    saturation: float = 1.0
    temperature: float = 0.0
    tint: float = 0.0
    gamma: float = 1.0
    shadows: float = 0.0
    highlights: float = 0.0
    lut_path: str = ""
    enabled: bool = True
    # Independent display-referred primary wheels.  Gamma/gain are offsets
    # from unity, so every wheel is neutral at zero.  Appending these fields
    # also preserves the positional arguments used by older projects/tools.
    lift_r: float = 0.0
    lift_g: float = 0.0
    lift_b: float = 0.0
    lift_y: float = 0.0
    gamma_r: float = 0.0
    gamma_g: float = 0.0
    gamma_b: float = 0.0
    gamma_y: float = 0.0
    gain_r: float = 0.0
    gain_g: float = 0.0
    gain_b: float = 0.0
    gain_y: float = 0.0
    offset_r: float = 0.0
    offset_g: float = 0.0
    offset_b: float = 0.0
    offset_y: float = 0.0
    curve_master: tuple[tuple[float, float], ...] = DEFAULT_CURVE
    curve_r: tuple[tuple[float, float], ...] = DEFAULT_CURVE
    curve_g: tuple[tuple[float, float], ...] = DEFAULT_CURVE
    curve_b: tuple[tuple[float, float], ...] = DEFAULT_CURVE
    skin_lighten: float = 0.0
    skin_saturation: float = 0.0
    wb_red: float = 1.0
    wb_green: float = 1.0
    wb_blue: float = 1.0
    skin_deyellow: float = 0.0


@dataclass(slots=True)
class Transition:
    time: float
    kind: str = "fade"
    duration: float = 0.5


# Public names are intentionally identical to FFmpeg's documented xfade names.
TRANSITION_KINDS: dict[str, str] = {
    name: name for name in (
        "fade", "dissolve", "wipeleft", "wiperight", "slideleft", "slideright",
        "circleopen", "fadeblack", "fadewhite",
    )
}

GRADE_RANGES: dict[str, tuple[float, float]] = {
    "exposure": (-4.0, 4.0),
    "contrast": (0.0, 3.0),
    "saturation": (0.0, 3.0),
    "temperature": (-1.0, 1.0),
    "tint": (-1.0, 1.0),
    "gamma": (0.1, 4.0),
    "shadows": (-1.0, 1.0),
    "highlights": (-1.0, 1.0),
}

WHEEL_NAMES = ("lift", "gamma", "gain", "offset")
SKIN_RANGES = {'skin_lighten': (0.0,1.0), 'skin_saturation': (0.0,1.0), 'skin_deyellow': (0.0,1.0)}
WB_RANGES = {'wb_red':(.5,2.0),'wb_green':(.5,2.0),'wb_blue':(.5,2.0)}
WHEEL_RANGES: dict[str, tuple[float, float]] = {
    f"{wheel}_{channel}": (-0.8, 0.8) if channel == "y" and wheel in {"gamma", "gain"} else (-0.5, 0.5)
    for wheel in WHEEL_NAMES for channel in "rgby"
}
_WHEEL_LABELS = {"lift": "暗部", "gamma": "中间调", "gain": "亮部", "offset": "偏移"}

_GRADE_LABELS = {
    "exposure": "曝光", "contrast": "对比度", "saturation": "饱和度",
    "temperature": "色温", "tint": "色调", "gamma": "伽马",
    "shadows": "阴影", "highlights": "高光",
}


def _finite(value, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须是数字")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{label}必须在 {minimum:g} 到 {maximum:g} 之间")
    return number


def validate_grade(grade: ColorGrade, *, check_lut: bool = True) -> None:
    """Reject invalid controls; verify an enabled LUT before rendering."""
    if not isinstance(grade, ColorGrade):
        raise ValueError("调色设置结构无效")
    for name, bounds in GRADE_RANGES.items():
        _finite(getattr(grade, name), _GRADE_LABELS[name], *bounds)
    for name, bounds in WHEEL_RANGES.items():
        wheel, channel = name.split("_")
        _finite(getattr(grade, name), f"{_WHEEL_LABELS[wheel]} {channel.upper()}", *bounds)
    for name,bounds in SKIN_RANGES.items():
        _finite(getattr(grade,name),{'skin_lighten':'肤色提亮','skin_saturation':'肤色去饱和','skin_deyellow':'肤色去黄'}[name],*bounds)
    for name,bounds in WB_RANGES.items():
        _finite(getattr(grade,name),'白平衡通道增益',*bounds)
    for channel in "rgb":
        if 1.0 + grade.gamma_y + getattr(grade, f"gamma_{channel}") < 0.05 - 1e-12:
            raise ValueError(f"中间调 {channel.upper()} 与 Y 合计后的伽马必须至少为 0.05")
        if 1.0 + grade.gain_y + getattr(grade, f"gain_{channel}") < -1e-12:
            raise ValueError(f"亮部 {channel.upper()} 与 Y 合计后的增益不能小于 0")
    for channel in CURVE_CHANNELS:
        canonical_curve(getattr(grade, f'curve_{channel}'))
    if not isinstance(grade.enabled, bool):
        raise ValueError("调色启用状态必须是布尔值")
    if not isinstance(grade.lut_path, str) or any(char in grade.lut_path for char in "\x00\r\n"):
        raise ValueError("LUT 路径无效")
    if grade.lut_path:
        path = Path(grade.lut_path)
        if path.suffix.lower() not in {".cube", ".3dl"}:
            raise ValueError("LUT 仅支持 .cube 和 .3dl 文件")
        if check_lut and grade.enabled and not path.is_file():
            raise ValueError(f"找不到 LUT 文件：{grade.lut_path}")


def grade_is_identity(grade: ColorGrade) -> bool:
    """True means bypass the grade, keeping the original fast render path."""
    validate_grade(grade, check_lut=False)
    if not grade.enabled:
        return True
    defaults = ColorGrade()
    return not grade.lut_path and all(
        math.isclose(float(getattr(grade, name)), float(getattr(defaults, name)),
                     rel_tol=0.0, abs_tol=1e-12)
        for name in (*GRADE_RANGES, *WHEEL_RANGES, *SKIN_RANGES, *WB_RANGES)
    ) and all(canonical_curve(getattr(grade, f'curve_{channel}')) == DEFAULT_CURVE
              for channel in CURVE_CHANNELS)


def wheel_values(grade: ColorGrade, name: str) -> dict[str, float]:
    """Read one wheel using normalized RGB and luminance/master controls."""
    if name not in WHEEL_NAMES:
        raise ValueError("调色盘类型无效")
    validate_grade(grade, check_lut=False)
    return {channel: float(getattr(grade, f"{name}_{channel}")) for channel in "rgby"}


def replace_wheel(grade: ColorGrade, name: str, r: float, g: float,
                  b: float, y: float) -> ColorGrade:
    """Return a validated copy; leave the original untouched on invalid input."""
    if name not in WHEEL_NAMES:
        raise ValueError("调色盘类型无效")
    if not isinstance(grade, ColorGrade):
        raise ValueError("调色设置结构无效")
    adjusted = replace(grade, **{f"{name}_{channel}": value
                                for channel, value in zip("rgby", (r, g, b, y))})
    validate_grade(adjusted, check_lut=False)
    return adjusted


def curve_points(grade: ColorGrade, channel: str) -> tuple[tuple[float, float], ...]:
    """Read one validated curve without exposing a mutable project value."""
    if channel not in CURVE_CHANNELS:
        raise ValueError('曲线通道无效')
    if not isinstance(grade, ColorGrade):
        raise ValueError('调色设置结构无效')
    return canonical_curve(getattr(grade, f'curve_{channel}'))


def replace_curve(grade: ColorGrade, channel: str, points) -> ColorGrade:
    """Replace one curve on a new grade, preserving all remaining controls."""
    if channel not in CURVE_CHANNELS:
        raise ValueError('曲线通道无效')
    if not isinstance(grade, ColorGrade):
        raise ValueError('调色设置结构无效')
    result = replace(grade, **{f'curve_{channel}': canonical_curve(points)})
    validate_grade(result, check_lut=False)
    return result


def grade_to_dict(grade: ColorGrade) -> dict:
    validate_grade(grade, check_lut=False)
    return asdict(grade)


def grade_from_dict(value: Mapping | None) -> ColorGrade:
    if value is None:
        return ColorGrade()
    if not isinstance(value, Mapping):
        raise ValueError("调色设置必须是对象")
    known = {item.name for item in fields(ColorGrade)}
    if set(value) - known:
        raise ValueError("调色设置包含未知选项")
    payload = dict(value)
    for channel in CURVE_CHANNELS:
        name = f'curve_{channel}'
        if name in payload:
            payload[name] = canonical_curve(payload[name])
    grade = ColorGrade(**payload)
    # A moved project can still open and relink an unavailable LUT.  Rendering
    # calls validate_grade with check_lut=True and reports its full path.
    validate_grade(grade, check_lut=False)
    return grade


def escape_filter_path(value: str) -> str:
    """Escape an option value through both libavfilter parsing layers.

    The caller must pass the complete filter graph as a subprocess argument,
    never through a shell.  No outer quotes are needed.  Forward slashes also
    work with Windows drive paths and avoid incidental backslash escapes.
    """
    if not isinstance(value, str) or any(char in value for char in "\x00\r\n"):
        raise ValueError("滤镜文件路径无效")
    value = value.replace("\\", "/")
    option = "".join("\\" + char if char in "\\':" else char for char in value)
    return "".join("\\" + char if char in "\\'[],;" else char for char in option)


def _fmt(value: float) -> str:
    return format(value, ".10g")


def _saturation_filter(amount: float) -> str:
    # Rec.709 luma weights.  Splitting extreme settings keeps each matrix
    # coefficient inside colorchannelmixer's legal [-2, 2] range.
    count = 2 if amount > 2.0 else 1
    factor = amount ** (1.0 / count)
    weights = (0.2126, 0.7152, 0.0722)
    names = (("rr", "rg", "rb"), ("gr", "gg", "gb"), ("br", "bg", "bb"))
    options = []
    for row in range(3):
        for column in range(3):
            coefficient = weights[column] * (1.0 - factor) + (factor if row == column else 0.0)
            options.append(f"{names[row][column]}={_fmt(coefficient)}")
    return ",".join(["colorchannelmixer=" + ":".join(options)] * count)


def grade_filter(grade: ColorGrade) -> str:
    """Return an FFmpeg chain, or '' for an identity/disabled grade.

    RGB LUT, curves and channel-matrix filters retain high-bit-depth formats;
    the caller chooses the final YUV output format after this chain.
    """
    validate_grade(grade)
    if grade_is_identity(grade):
        return ""
    chain: list[str] = []
    if any(getattr(grade,name)!=1.0 for name in WB_RANGES):
        chain.append('format=gbrp16le,lutrgb='+':'.join(
            f"{channel}='clip(val*{_fmt(gain)},0,maxval)'"
            for channel,gain in zip('rgb',(grade.wb_red,grade.wb_green,grade.wb_blue))))
    if grade.exposure != 0 or grade.contrast != 1 or grade.gamma != 1:
        expression = (
            "pow(clip(((val/maxval)*" + _fmt(2.0 ** grade.exposure) + "-0.5)*"
            + _fmt(grade.contrast) + "+0.5,0,1)," + _fmt(1.0 / grade.gamma) + ")*maxval"
        )
        chain.append("lutrgb=" + ":".join(f"{channel}='{expression}'" for channel in "rgb"))
    if any(getattr(grade, name) != 0 for name in WHEEL_RANGES):
        # A portable display-referred primary transform, implemented here
        # independently of any proprietary grading system.  Lift anchors at
        # white, gain scales from black, gamma bends the midtones, and offset
        # translates the whole signal.  RGB LUTs operate at 16-bit precision.
        channels = []
        for channel in "rgb":
            lift = grade.lift_y + getattr(grade, f"lift_{channel}")
            gamma = max(0.05, 1.0 + grade.gamma_y + getattr(grade, f"gamma_{channel}"))
            gain = max(0.0, 1.0 + grade.gain_y + getattr(grade, f"gain_{channel}"))
            offset = grade.offset_y + getattr(grade, f"offset_{channel}")
            expression = (
                "clip(pow(clip(((val/maxval)*(1-" + _fmt(lift) + ")+"
                + _fmt(lift) + ")*" + _fmt(gain) + ",0,1),"
                + _fmt(1.0 / gamma) + ")+" + _fmt(offset) + ",0,1)*maxval"
            )
            channels.append(f"{channel}='{expression}'")
        chain.append("lutrgb=" + ":".join(channels))
    if grade.saturation != 1:
        chain.append(_saturation_filter(grade.saturation))
    if grade.temperature != 0 or grade.tint != 0:
        red = grade.temperature * 0.25 - grade.tint * 0.1
        green = grade.tint * 0.2
        blue = -grade.temperature * 0.25 - grade.tint * 0.1
        chain.append(f"colorbalance=rm={_fmt(red)}:gm={_fmt(green)}:bm={_fmt(blue)}:pl=1")
    if grade.shadows != 0 or grade.highlights != 0:
        shadow = 0.25 + grade.shadows * 0.2
        highlight = 0.75 + grade.highlights * 0.2
        chain.append(f"curves=master='0/0 0.25/{_fmt(shadow)} 0.75/{_fmt(highlight)} 1/1'")
    custom_curves = []
    for channel, option in (('r', 'red'), ('g', 'green'), ('b', 'blue'), ('master', 'master')):
        points = curve_points(grade, channel)
        if points != DEFAULT_CURVE:
            coordinates = ' '.join(f'{_fmt(x)}/{_fmt(y)}' for x, y in points)
            custom_curves.append(f"{option}='{coordinates}'")
    if custom_curves:
        # Explicit 16-bit RGB preserves input precision and gives every legal
        # control point a distinct LUT index, including a 10-bit YUV input.
        chain.append('format=gbrp16le')
        chain.append('curves=' + ':'.join(custom_curves) + ':interp=natural')
    if grade.skin_lighten or grade.skin_saturation or grade.skin_deyellow:
        chain.append(f"format=gbrp16le,lut3d=file={escape_filter_path(portrait_lut_path(grade.skin_lighten,grade.skin_saturation,grade.skin_deyellow))}:interp=tetrahedral")
    if grade.lut_path:
        chain.append(f"lut3d=file={escape_filter_path(grade.lut_path)}:interp=tetrahedral")
    return ",".join(chain)


def transition_filter(kind: str | Transition) -> str:
    if isinstance(kind, Transition):
        kind = kind.kind
    if not isinstance(kind, str) or kind not in TRANSITION_KINDS:
        raise ValueError("转场类型无效")
    return TRANSITION_KINDS[kind]


def transition_window(transition: Transition) -> tuple[float, float]:
    validate_transitions([transition])
    half = transition.duration / 2.0
    return transition.time - half, transition.time + half


def validate_transitions(transitions: list[Transition], project=None) -> None:
    """Validate centred transition windows without changing programme time.

    Optional project is duck typed (fps, duration and cuts).  This keeps the
    module independent of the multicamera model and avoids circular imports.
    """
    if not isinstance(transitions, list) or len(transitions) > 100_000:
        raise ValueError("转场必须是列表且数量不能超过 100000")
    ordered: list[Transition] = []
    for transition in transitions:
        if not isinstance(transition, Transition):
            raise ValueError("转场结构无效")
        _finite(transition.time, "转场时间", 0.0, 1e12)
        _finite(transition.duration, "转场时长", 0.05, 5.0)
        transition_filter(transition.kind)
        ordered.append(transition)
    ordered.sort(key=lambda item: item.time)
    previous_end = -math.inf
    for transition in ordered:
        start = transition.time - transition.duration / 2.0
        end = transition.time + transition.duration / 2.0
        if start < -1e-8:
            raise ValueError("转场不能超出工程开头")
        if start < previous_end - 1e-8:
            raise ValueError("转场区间不能重叠，请缩短时长或调整切点")
        previous_end = end
        if project is not None:
            fps = _finite(project.fps, "工程帧率", 0.001, 1000.0)
            duration = _finite(project.duration, "工程时长", 0.0, 1e12)
            if end > duration + 1e-8:
                raise ValueError("转场不能超出工程结尾")
            # Match the same rounded frame used by the multicamera timeline.
            frame = math.floor(transition.time * fps + 0.5)
            boundaries = [cut.time for cut in project.cuts]
            boundaries.extend(getattr(project, 'edit_points', []))
            if not any(math.floor(time * fps + 0.5) == frame and frame > 0
                       for time in boundaries):
                raise ValueError("转场必须放在现有的片段切点上")


def transition_to_dict(transition: Transition) -> dict:
    validate_transitions([transition])
    return asdict(transition)


def transition_from_dict(value: Mapping) -> Transition:
    if not isinstance(value, Mapping) or set(value) - {"time", "kind", "duration"} or "time" not in value:
        raise ValueError("转场设置结构无效")
    transition = Transition(**dict(value))
    validate_transitions([transition])
    return transition


def _camera_key(value) -> int:
    if isinstance(value, bool):
        raise ValueError("调色机位编号必须是正整数")
    if isinstance(value, str):
        if not value.isascii() or not value.isdecimal():
            raise ValueError("调色机位编号必须是正整数")
        value = int(value)
    if not isinstance(value, int) or value < 1:
        raise ValueError("调色机位编号必须是正整数")
    return value


def effects_to_dict(global_grade: ColorGrade, camera_grades: Mapping[int, ColorGrade],
                    transitions: list[Transition]) -> dict:
    if not isinstance(camera_grades, Mapping):
        raise ValueError("机位调色必须是对象")
    validate_transitions(transitions)
    cameras = {}
    for camera, grade in camera_grades.items():
        key = str(_camera_key(camera))
        if key in cameras:
            raise ValueError("调色机位编号重复")
        cameras[key] = grade_to_dict(grade)
    return {"global_grade": grade_to_dict(global_grade), "camera_grades": cameras,
            "transitions": [transition_to_dict(item) for item in transitions]}


def effects_from_dict(value: Mapping | None) -> tuple[ColorGrade, dict[int, ColorGrade], list[Transition]]:
    if value is None:
        return ColorGrade(), {}, []
    if not isinstance(value, Mapping) or set(value) - {"global_grade", "camera_grades", "transitions"}:
        raise ValueError("调色与转场设置结构无效")
    raw_cameras = value.get("camera_grades", {})
    raw_transitions = value.get("transitions", [])
    if not isinstance(raw_cameras, Mapping) or not isinstance(raw_transitions, list):
        raise ValueError("机位调色或转场设置结构无效")
    cameras: dict[int, ColorGrade] = {}
    for camera, grade in raw_cameras.items():
        key = _camera_key(camera)
        if key in cameras:
            raise ValueError("调色机位编号重复")
        cameras[key] = grade_from_dict(grade)
    transitions = [transition_from_dict(item) for item in raw_transitions]
    validate_transitions(transitions)
    return grade_from_dict(value.get("global_grade")), cameras, transitions
