from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

# User-facing ranges.  The colour controls intentionally use the same
# -100..100 scale as MPV's GPU properties so the number shown in the UI is
# also the value sent to the player.
EFFECT_RANGES: dict[str, tuple[float, float, str]] = {
    "brightness": (-100.0, 100.0, "亮度"),
    "contrast": (-100.0, 100.0, "对比度"),
    "saturation": (-100.0, 100.0, "饱和度"),
    "gamma": (-100.0, 100.0, "伽马"),
    "sharpen": (0.0, 100.0, "锐化"),
    "blur": (0.0, 100.0, "模糊"),
    "vignette": (0.0, 100.0, "暗角"),
    "fade_in": (0.0, 10.0, "淡入"),
    "fade_out": (0.0, 10.0, "淡出"),
    "overlay_opacity": (0.0, 100.0, "叠加不透明度"),
}

ROTATION_CHOICES: dict[str, int] = {
    "不旋转": 0,
    "顺时针 90°": 90,
    "旋转 180°": 180,
    "顺时针 270°": 270,
}
ROTATION_LABELS: tuple[str, ...] = tuple(ROTATION_CHOICES)
ALLOWED_ROTATIONS = frozenset(ROTATION_CHOICES.values())
MPV_COLOR_PROPERTIES = ("brightness", "contrast", "saturation", "gamma")
OVERLAY_KINDS = frozenset({"alpha", "screen"})


@dataclass(slots=True)
class EffectSettings:
    """One immutable-ish description shared by export and preview.

    ``brightness``/``contrast``/``saturation``/``gamma`` use -100..100 where
    0 is neutral.  ``rotation`` is 0/90/180/270 degrees clockwise.
    ``sharpen``/``blur``/``vignette`` use 0..100.  Fade values are seconds.

    Overlay fields describe one asset from the bundled effects library:
    ``overlay`` is its display name (empty = none); ``overlay_path`` is the
    absolute file; ``overlay_kind`` is ``alpha`` (composited through the
    alpha channel) or ``screen`` (light assets filmed on black, blended with
    screen mode); ``overlay_opacity`` is 0..100.
    """

    brightness: float = 0.0
    contrast: float = 0.0
    saturation: float = 0.0
    gamma: float = 0.0
    rotation: int = 0
    hflip: bool = False
    vflip: bool = False
    sharpen: float = 0.0
    blur: float = 0.0
    vignette: float = 0.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    overlay: str = ""
    overlay_path: str = ""
    overlay_kind: str = ""
    overlay_fps: float = 25.0
    overlay_opacity: float = 100.0

    def copy(self) -> "EffectSettings":
        return replace(self)


DEFAULT_EFFECT_SETTINGS = EffectSettings()


def default_effects() -> EffectSettings:
    """Return a fresh neutral effect stack."""
    return EffectSettings()


def overlay_active(settings: EffectSettings | None) -> bool:
    """True when an overlay asset is selected with a usable file."""
    if settings is None:
        return False
    return bool(settings.overlay and settings.overlay_path and settings.overlay_kind in OVERLAY_KINDS)


def _number(value: object, label: str) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数字。") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label}必须是有限数字。")
    return result


def _check_range(value: float, minimum: float, maximum: float, label: str) -> float:
    if value < minimum - 1e-9 or value > maximum + 1e-9:
        raise ValueError(f"{label}必须在 {minimum:g}–{maximum:g} 之间。")
    return min(maximum, max(minimum, value))


def validate_effects(
    settings: EffectSettings,
    *,
    duration: float | None = None,
) -> EffectSettings:
    """Validate and normalise an effect stack.

    Raises ``ValueError`` with a user-readable Chinese message.  A duration is
    required when a fade is requested, because FFmpeg's ``fade`` filter needs
    the absolute fade times.
    """

    values: dict[str, float] = {}
    for name, (minimum, maximum, label) in EFFECT_RANGES.items():
        raw = getattr(settings, name)
        values[name] = _check_range(_number(raw, label), minimum, maximum, label)

    duration_value: float | None = None
    if duration is not None:
        # Reject NaN/Inf/illegal strings consistently even when no fade is
        # enabled. Numeric strings are normalised; 0 is only a problem for a
        # requested fade and is checked below.
        duration_value = _number(duration, "视频时长")

    try:
        rotation_number = float(settings.rotation)
    except (TypeError, ValueError) as exc:
        raise ValueError("旋转角度必须是 0、90、180 或 270 度。") from exc
    if not math.isfinite(rotation_number) or rotation_number != int(rotation_number):
        raise ValueError("旋转角度必须是 0、90、180 或 270 度。")
    rotation = int(rotation_number)
    if rotation not in ALLOWED_ROTATIONS:
        raise ValueError("旋转角度必须是 0、90、180 或 270 度。")

    fade_in = values["fade_in"]
    fade_out = values["fade_out"]
    if fade_in > 0 or fade_out > 0:
        if duration_value is None:
            raise ValueError("淡入/淡出需要先读取视频时长。")
        if duration_value <= 0:
            raise ValueError("需要先读取视频时长；视频时长必须大于 0 才能计算淡入/淡出。")
        if fade_in + fade_out > duration_value + 1e-6:
            raise ValueError(
                f"淡入 {fade_in:g} 秒 + 淡出 {fade_out:g} 秒不能超过视频时长 "
                f"{duration_value:.3f} 秒。"
            )

    overlay = str(settings.overlay or "").strip()[:80]
    overlay_path = str(settings.overlay_path or "").strip()
    overlay_kind = str(settings.overlay_kind or "").strip()
    if not overlay:
        overlay_path = ""
        overlay_kind = ""
    elif overlay_kind not in OVERLAY_KINDS:
        raise ValueError(f"叠加特效“{overlay}”的类型无效，请重新选择。")
    if not overlay_path:
        overlay_path = ""
    try:
        overlay_fps = _number(settings.overlay_fps, "叠加素材帧率")
    except ValueError as exc:
        raise ValueError(f"叠加特效“{overlay}”信息无效，请重新选择。") from exc
    if overlay and (not overlay_path or not math.isfinite(overlay_fps) or overlay_fps <= 0):
        raise ValueError(f"叠加特效“{overlay}”缺少素材信息，请重新选择。")
    if not overlay:
        overlay_fps = 25.0

    return replace(
        settings,
        brightness=values["brightness"],
        contrast=values["contrast"],
        saturation=values["saturation"],
        gamma=values["gamma"],
        sharpen=values["sharpen"],
        blur=values["blur"],
        vignette=values["vignette"],
        fade_in=fade_in,
        fade_out=fade_out,
        rotation=rotation,
        hflip=bool(settings.hflip),
        vflip=bool(settings.vflip),
        overlay=overlay,
        overlay_path=overlay_path,
        overlay_kind=overlay_kind,
        overlay_fps=max(1.0, min(240.0, overlay_fps)),
        overlay_opacity=values["overlay_opacity"],
    )


def effects_active(settings: EffectSettings | None) -> bool:
    """True when the stack would visibly change a frame."""
    if settings is None:
        return False
    try:
        if settings.rotation or settings.hflip or settings.vflip:
            return True
        if overlay_active(settings):
            return True
        return any(
            abs(float(getattr(settings, name))) > 1e-9
            for name in (
                "brightness", "contrast", "saturation", "gamma",
                "sharpen", "blur", "vignette", "fade_in", "fade_out",
            )
        )
    except (TypeError, ValueError):
        # Invalid values must still travel to validate_effects(), which raises
        # a readable error instead of silently dropping the stack.
        return True


def _colour_filters(settings: EffectSettings) -> list[str]:
    if not any(
        abs(getattr(settings, name)) > 1e-9
        for name in ("brightness", "contrast", "saturation", "gamma")
    ):
        return []
    brightness = settings.brightness / 100.0
    contrast = 1.0 + settings.contrast / 100.0
    saturation = 1.0 + settings.saturation / 100.0
    gamma = max(0.1, 1.0 + settings.gamma / 100.0)
    return [
        f"eq=brightness={brightness:.5f}:contrast={contrast:.5f}:"
        f"saturation={saturation:.5f}:gamma={gamma:.5f}"
    ]


def _geometry_filters(settings: EffectSettings, *, even_dimensions: bool) -> list[str]:
    filters: list[str] = []
    if settings.rotation == 90:
        filters.append("transpose=1")
    elif settings.rotation == 180:
        filters.append("transpose=2,transpose=2")
    elif settings.rotation == 270:
        filters.append("transpose=2")
    if even_dimensions and settings.rotation in {90, 270}:
        # NVENC requires even dimensions.  Transposing an odd-sized source
        # would otherwise produce an odd width and fail at encoder setup.
        filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    if settings.hflip:
        filters.append("hflip")
    if settings.vflip:
        filters.append("vflip")
    return filters


def _look_filters(settings: EffectSettings) -> list[str]:
    filters: list[str] = []
    if settings.sharpen > 0:
        amount = settings.sharpen / 100.0 * 1.5
        filters.append(f"unsharp=5:5:{amount:.4f}:5:5:0")
    if settings.blur > 0:
        sigma = settings.blur / 100.0 * 5.0
        filters.append(f"gblur=sigma={sigma:.4f}")
    if settings.vignette > 0:
        # Larger FFmpeg vignette angles darken the corners more.  Keep the
        # 100% setting just below PI/2 so the picture remains recognisable.
        angle = settings.vignette / 100.0 * (math.pi / 2.0) * 0.95
        filters.append(f"vignette=angle={angle:.5f}")
    return filters


def _fade_filters(settings: EffectSettings, duration: float | None) -> list[str]:
    filters: list[str] = []
    if settings.fade_in > 0:
        filters.append(f"fade=t=in:st=0:d={settings.fade_in:.3f}")
    if settings.fade_out > 0:
        start = max(0.0, float(duration or 0.0) - settings.fade_out)
        filters.append(f"fade=t=out:st={start:.3f}:d={settings.fade_out:.3f}")
    return filters


def _effect_chain_filters(
    validated: EffectSettings,
    duration: float | None,
    *,
    include_colours: bool,
    even_dimensions: bool,
) -> tuple[list[str], list[str]]:
    """Split the stack into (pre-overlay, post-overlay) simple chains."""
    pre = _geometry_filters(validated, even_dimensions=even_dimensions)
    if include_colours:
        pre += _colour_filters(validated)
    pre += _look_filters(validated)
    post = _fade_filters(validated, duration)
    return pre, post


def build_effect_filters(
    settings: EffectSettings | None,
    *,
    duration: float | None = None,
    include_colours: bool = True,
    even_dimensions: bool = True,
) -> list[str]:
    """Return the ordered simple filter chain, overlays excluded.

    Order is: geometry (rotation/flip) → colour → sharpen/blur → vignette →
    fade.  The returned list is joined with commas by the export layer so the
    whole stack is a single ``-vf`` filter graph.  Overlay assets cannot be
    expressed as a simple chain; use :func:`build_effect_filter_graph`.
    """

    if settings is None:
        return []
    validated = validate_effects(settings, duration=duration)
    if not effects_active(validated):
        return []
    pre, post = _effect_chain_filters(
        validated, duration,
        include_colours=include_colours, even_dimensions=even_dimensions,
    )
    return pre + post


def _movie_path_argument(path: str) -> str:
    """Escape an absolute path for the ``movie`` filter's filename option.

    The value passes through two levels of FFmpeg parsing (graph then filter
    options), so ``:`` must be escaped even inside the graph-level quotes.
    """
    escaped = path.replace("\\", "/")
    escaped = escaped.replace("'", r"\'").replace(":", r"\:")
    escaped = escaped.replace(",", r"\,").replace(";", r"\;").replace("[", r"\[").replace("]", r"\]")
    return f"'{escaped}'"


def _overlay_movie_chain(validated: EffectSettings) -> str:
    """movie-source chain for the selected overlay asset, output labelled [ov]."""
    opacity = validated.overlay_opacity
    chain = [
        f"movie={_movie_path_argument(validated.overlay_path)}:loop=0",
        "format=yuva420p" if validated.overlay_kind == "alpha" else "format=yuv420p",
    ]
    if validated.overlay_kind == "alpha" and opacity < 100.0 - 1e-9:
        chain.append(f"colorchannelmixer=aa={opacity / 100.0:.4f}")
    chain.append(f"setpts=N/({validated.overlay_fps:.6f}*TB)")
    return ",".join(chain) + "[ov]"


def build_effect_filter_graph(
    settings: EffectSettings | None,
    *,
    duration: float | None = None,
    prefix: Sequence[str] = (),
    suffix: Sequence[str] = (),
    include_colours: bool = True,
    even_dimensions: bool = True,
    in_label: str = "[in]",
    out_label: str = "[out]",
) -> str:
    """Return one complete FFmpeg filter-graph string for the effect stack.

    Without an overlay this is the plain comma-joined chain (identical to
    :func:`build_effect_filters` plus ``prefix``/``suffix``), so existing
    export paths keep byte-identical commands.  With an overlay the graph
    becomes a labelled multi-statement graph that decodes the asset through a
    ``movie`` source, loops it (``loop=0``), scales it to the main frame
    (``scale2ref``), composites it (``overlay`` for alpha assets, ``blend=
    screen`` for light-on-black assets) and ends the output with the main
    stream (``shortest=1``).
    """

    if settings is None:
        return ""
    validated = validate_effects(settings, duration=duration)
    if not effects_active(validated):
        return ""
    pre, post = _effect_chain_filters(
        validated, duration,
        include_colours=include_colours, even_dimensions=even_dimensions,
    )
    pre = [str(part) for part in prefix] + pre
    post = post + [str(part) for part in suffix]

    if not overlay_active(validated):
        return ",".join(pre + post)

    statements: list[str] = []
    statements.append(in_label + ("null" if not pre else ",".join(pre)) + "[bg]")
    statements.append(_overlay_movie_chain(validated))
    statements.append("[ov][bg]scale2ref=w=rw:h=rh[ovs][bgs]")
    tail = ",".join(post) if post else None
    if validated.overlay_kind == "alpha":
        composite = "[bgs][ovs]overlay=0:0:format=auto:shortest=1"
        if tail:
            composite += "," + tail
        statements.append(composite + out_label)
    else:
        opacity = validated.overlay_opacity / 100.0
        composite_head = (
            "[bgs]format=gbrp[bgp];[ovs]format=gbrp[ovp];"
            f"[bgp][ovp]blend=all_mode=screen:all_opacity={opacity:.4f}:shortest=1"
        )
        composite = composite_head + (("," + tail) if tail else "") + out_label
        statements.append(composite)
    return ";".join(statements)


def build_effect_filter_string(
    settings: EffectSettings | None,
    *,
    duration: float | None = None,
) -> str:
    return ",".join(build_effect_filters(settings, duration=duration))


def mpv_effect_properties(settings: EffectSettings) -> dict[str, float]:
    """GPU-rendered colour properties, in MPV's native -100..100 scale.

    Fade values are intentionally not checked here: they do not affect the
    colour properties and may be edited before a video duration is known.
    """

    result: dict[str, float] = {}
    for name in MPV_COLOR_PROPERTIES:
        minimum, maximum, label = EFFECT_RANGES[name]
        result[name] = _check_range(_number(getattr(settings, name), label), minimum, maximum, label)
    return result


def mpv_effect_filter(settings: EffectSettings, *, duration: float | None = None) -> str:
    """MPV ``lavfi`` expression for effects MPV cannot apply as GPU properties.

    Colour is deliberately omitted: it is sent through MPV's GPU properties.
    Rotation/flip and the look filters run through libavfilter and may force a
    CPU copy of hardware-decoded frames.  The UI states this explicitly.

    Overlay assets cannot run through MPV's simple ``lavfi`` video filter;
    when one is selected the preview must use
    :func:`mpv_lavfi_complex_graph` instead.
    """

    if overlay_active(settings):
        return ""
    filters = build_effect_filters(
        settings,
        duration=duration,
        include_colours=False,
        even_dimensions=False,
    )
    if not filters:
        return ""
    return "lavfi=[" + ",".join(filters) + "]"


def mpv_lavfi_complex_graph(settings: EffectSettings, *, duration: float | None = None) -> str:
    """MPV ``--lavfi-complex`` graph (``[vid1]…[vo]``) for the whole stack.

    Used when an overlay asset is selected: the ``movie`` source requires a
    complex graph, which MPV only accepts through ``lavfi-complex``.  Colour
    is omitted as before — it is applied through GPU properties.
    """

    return build_effect_filter_graph(
        settings,
        duration=duration,
        include_colours=False,
        even_dimensions=False,
        in_label="[vid1]",
        out_label="[vo]",
    )


def effects_summary(settings: EffectSettings | None) -> str:
    if settings is None or not effects_active(settings):
        return "当前参数为默认值（无可见特效）"
    parts: list[str] = []
    labels = {
        "brightness": "亮度", "contrast": "对比度", "saturation": "饱和度",
        "gamma": "伽马", "sharpen": "锐化", "blur": "模糊", "vignette": "暗角",
    }
    for name, label in labels.items():
        value = float(getattr(settings, name))
        if abs(value) > 1e-9:
            if name in {"brightness", "contrast", "saturation", "gamma"}:
                parts.append(f"{label} {value:+g}")
            else:
                parts.append(f"{label} {value:g}")
    if settings.rotation:
        parts.append(f"旋转 {settings.rotation}°")
    if settings.hflip:
        parts.append("水平翻转")
    if settings.vflip:
        parts.append("垂直翻转")
    if settings.fade_in > 0:
        parts.append(f"淡入 {settings.fade_in:g}s")
    if settings.fade_out > 0:
        parts.append(f"淡出 {settings.fade_out:g}s")
    if overlay_active(settings):
        opacity = ""
        if settings.overlay_opacity < 100.0 - 1e-9:
            opacity = f" {settings.overlay_opacity:g}%"
        mode = "滤色" if settings.overlay_kind == "screen" else "叠加"
        parts.append(f"{mode} {settings.overlay}{opacity}")
    return " · ".join(parts)


def describe_filter_chain(filters: Iterable[str]) -> str:
    """Small diagnostic helper used by logs/tests."""
    return ",".join(filters)
