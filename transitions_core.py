from __future__ import annotations

import math
import re

from effects_core import EffectSettings, build_effect_filter_graph, effects_active, overlay_active, validate_effects
from dataclasses import dataclass, replace
from pathlib import Path

"""Timeline transition rendering.

Transitions are attached to the cuts between adjacent timeline clips and
rendered by one FFmpeg pass: every clip is an input, normalised
(resolution/fps/pixel format), then the video streams are chained through
per-cut transitions.  Two kinds are supported:

- ``xfade`` — FFmpeg's built-in cross-fade modes (58 transitions).
- ``mask`` — gradient wipes driven by a grayscale mask image from the
  transition library (converted from the Y044 DaVinci transition pack).
  The mask is thresholded against a linearly animated ramp: pixels switch
  from the outgoing to the incoming clip in the order the mask defines.
  Masks run through the four-input ``threshold`` filter on planar RGB.

Audio is cross-faded with ``acrossfade`` when every clip has audio,
otherwise the render is video-only.
"""

# (xfade mode, Chinese label) for every built-in transition of this build.
XFADE_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("fade", "淡入淡出"),
    ("fadeblack", "黑场过渡"),
    ("fadewhite", "白场过渡"),
    ("fadefast", "快速淡入"),
    ("fadeslow", "缓慢淡入"),
    ("fadegrays", "灰化淡入"),
    ("dissolve", "溶解"),
    ("pixelize", "像素化"),
    ("wipeleft", "向左擦除"),
    ("wiperight", "向右擦除"),
    ("wipeup", "向上擦除"),
    ("wipedown", "向下擦除"),
    ("wipetl", "向左上擦除"),
    ("wipetr", "向右上擦除"),
    ("wipebl", "向左下擦除"),
    ("wipebr", "向右下擦除"),
    ("slideleft", "向左滑动"),
    ("slideright", "向右滑动"),
    ("slideup", "向上滑动"),
    ("slidedown", "向下滑动"),
    ("smoothleft", "平滑向左"),
    ("smoothright", "平滑向右"),
    ("smoothup", "平滑向上"),
    ("smoothdown", "平滑向下"),
    ("circleopen", "圆形展开"),
    ("circleclose", "圆形收拢"),
    ("circlecrop", "圆形裁切"),
    ("rectcrop", "矩形裁切"),
    ("vertopen", "垂直展开"),
    ("vertclose", "垂直收拢"),
    ("horzopen", "水平展开"),
    ("horzclose", "水平收拢"),
    ("diagtl", "对角左上"),
    ("diagtr", "对角右上"),
    ("diagbl", "对角左下"),
    ("diagbr", "对角右下"),
    ("hlslice", "上半切片"),
    ("hrslice", "下半切片"),
    ("vuslice", "左侧切片"),
    ("vdslice", "右侧切片"),
    ("hblur", "水平模糊"),
    ("squeezeh", "水平挤压"),
    ("squeezev", "垂直挤压"),
    ("zoomin", "推近"),
    ("distance", "距离变形"),
    ("radial", "径向擦除"),
    ("hlwind", "上侧风刷"),
    ("hrwind", "下侧风刷"),
    ("vuwind", "左侧风刷"),
    ("vdwind", "右侧风刷"),
    ("coverleft", "左侧覆盖"),
    ("coverright", "右侧覆盖"),
    ("coverup", "上方覆盖"),
    ("coverdown", "下方覆盖"),
    ("revealleft", "左侧揭幕"),
    ("revealright", "右侧揭幕"),
    ("revealup", "上方揭幕"),
    ("revealdown", "下方揭幕"),
)
XFADE_MODES = frozenset(mode for mode, _label in XFADE_TRANSITIONS)

MIN_TRANSITION_DURATION = 0.1
MAX_TRANSITION_DURATION = 4.0
RESOLUTION_HEIGHTS: dict[str, int] = {
    "1080p": 1080, "1440p": 1440, "4k": 2160, "8k": 4320,
}


@dataclass(slots=True)
class TransitionSpec:
    """One transition attached to a cut between two adjacent clips."""

    kind: str = "xfade"          # "xfade" | "mask"
    label: str = ""
    duration: float = 0.8
    xfade_mode: str = "fade"
    mask_file: str = ""          # file name inside the transitions directory
    reversed: bool = False

    def describe(self) -> str:
        suffix = f" {self.duration:g}s" + ("（反向）" if self.reversed else "")
        return f"{self.label}{suffix}"


@dataclass(slots=True)
class TransitionClip:
    """Probed info of one timeline clip used by the render."""

    path: str
    duration: float
    width: int = 0
    height: int = 0
    fps: float = 25.0
    has_audio: bool = False
    pixel_format: str = "yuv420p"


def _cuda_download_format(clip: TransitionClip) -> str | None:
    """CUDA download layout for this source; unsupported layouts use CPU decode."""
    return {
        "yuv420p": "nv12", "yuvj420p": "nv12", "nv12": "nv12",
        "yuv420p10le": "p010le", "p010le": "p010le", "p016le": "p016le",
    }.get((clip.pixel_format or "").strip().lower())


def default_transition_duration(value: object) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("转场时长必须是数字。") from exc
    if not math.isfinite(duration):
        raise ValueError("转场时长必须是有限数字。")
    if duration < MIN_TRANSITION_DURATION or duration > MAX_TRANSITION_DURATION:
        raise ValueError(
            f"转场时长必须在 {MIN_TRANSITION_DURATION:g}–{MAX_TRANSITION_DURATION:g} 秒之间。"
        )
    return duration


def validate_transitions(
    clips: list[TransitionClip],
    transitions: list[TransitionSpec | None],
    *,
    mask_dir: str | Path | None = None,
) -> list[TransitionSpec | None]:
    """Normalise and check one transition per cut (``len == len(clips) - 1``)."""

    if len(clips) < 2:
        raise ValueError("转场渲染至少需要两个片段。")
    if len(transitions) != len(clips) - 1:
        raise ValueError("转场数量与片段接点数量不一致。")
    checked: list[TransitionSpec | None] = []
    for index, spec in enumerate(transitions):
        if spec is None:
            checked.append(None)
            continue
        duration = default_transition_duration(spec.duration)
        neighbours = [clips[index].duration, clips[index + 1].duration]
        if min(neighbours) <= duration:
            raise ValueError(
                f"第 {index + 1} 个转场时长 {duration:g} 秒必须短于两侧片段"
                f"（{neighbours[0]:.3f}s / {neighbours[1]:.3f}s）。"
            )
        if spec.kind == "xfade":
            if spec.xfade_mode not in XFADE_MODES:
                raise ValueError(f"第 {index + 1} 个转场使用了不支持的 xfade 模式。")
        elif spec.kind == "mask":
            if not spec.mask_file or mask_dir is None:
                raise ValueError(f"第 {index + 1} 个转场缺少遮罩文件。")
            base = Path(mask_dir).resolve()
            mask_path = (base / spec.mask_file).resolve()
            if not mask_path.is_relative_to(base) or not mask_path.is_file():
                raise ValueError(f"第 {index + 1} 个转场的遮罩文件不存在：{spec.mask_file}")
        else:
            raise ValueError(f"第 {index + 1} 个转场类型无效：{spec.kind}")
        checked.append(replace(spec, duration=duration))
    return checked


def _quote_filter_path(path: str) -> str:
    escaped = path.replace("\\", "/")
    escaped = escaped.replace("'", r"\'").replace(":", r"\:")
    escaped = escaped.replace(",", r"\,").replace(";", r"\;").replace("[", r"\[").replace("]", r"\]")
    return f"'{escaped}'"


def _num(value: float) -> str:
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def _pixel_depth(pixel_format: str) -> int:
    """Bit depth of one FFmpeg pixel format name (8 when unspecified)."""
    name = (pixel_format or "").strip().lower()
    if name.startswith("p0"):
        digits = name[2:4]
        if digits.isdigit() and int(digits) > 8:
            return int(digits)
    for depth in (16, 12, 10):
        if name.endswith((f"p{depth}", f"p{depth}le", f"p{depth}be")):
            return depth
    return 8


def _intermediate_format(pixel_format: str) -> str:
    """Planar 4:4:4 working format that keeps the requested bit depth.

    Normalising every clip to ``yuv420p`` would truncate a 10-bit timeline
    to 8 bits before a single transition runs, so the intermediate format
    follows the requested output depth instead.
    """
    depth = _pixel_depth(pixel_format)
    return f"yuv444p{depth}le" if depth > 8 else "yuv420p"


def _rgb_format(pixel_format: str) -> str:
    depth = _pixel_depth(pixel_format)
    return f"gbrp{depth}le" if depth > 8 else "gbrp"


def _even_size(value: float) -> int:
    """Round to the nearest even pixel count (4:2:0 requires even sides)."""
    return max(2, int(round(float(value) / 2.0)) * 2)


def _frame_size(
    clips: list[TransitionClip],
    width: int | None = None,
    height: int | None = None,
) -> tuple[int, int]:
    """Target frame size: explicit override, else the first clip's own size."""

    def _even(value: object, fallback: int) -> int:
        try:
            size = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            size = fallback
        if size <= 0:
            size = fallback
        return _even_size(size)

    first = clips[0] if clips else None
    base_width = first.width if first is not None and first.width else 1920
    base_height = first.height if first is not None and first.height else 1080
    return _even(width, base_width), _even(height, base_height)


def target_frame_size(settings: object, media: object) -> tuple[int, int]:
    """Frame size for one transition render, honouring the export resolution.

    ``settings.resolution`` uses the same names as the regular export path
    (``"original"`` keeps the first clip's geometry).  Both sides are even
    because the pipeline ends in 4:2:0 formats.
    """
    resolution = str(getattr(settings, "resolution", "") or "").strip().lower()
    source_width = int(getattr(media, "width", 0) or 0)
    source_height = int(getattr(media, "height", 0) or 0)
    height = RESOLUTION_HEIGHTS.get(resolution) or source_height or 1080
    if not source_width or not source_height:
        width = 1920 * height / 1080
    elif height == source_height:
        width = source_width
    else:
        width = source_width * height / source_height
    return _even_size(width), _even_size(height)


def _movie_source(path: str, fps: float, width: int, height: int, extra: str, label: str) -> str:
    chain = [
        f"movie={_quote_filter_path(path)}:loop=0",
        f"scale={width}:{height}",
    ]
    if extra:
        chain.append(extra)
    chain.append(f"setpts=N/({_num(fps)}*TB)")
    return ",".join(chain) + label


def _ramp_source(
    width: int,
    height: int,
    fps: float,
    duration: float,
    offset: float,
    label: str,
    rgb_format: str = "gbrp",
    source_duration: float | None = None,
) -> str:
    """White clip fading in over ``duration``, delayed to start at ``offset``.

    ``source_duration`` extends the white level past the fade so the ramp
    never becomes the input that ends the mask stage early.
    """
    total = duration + 1.0 if source_duration is None else max(duration, source_duration)
    chain = [
        f"color=c=white:s={width}x{height}:r={_num(fps)}:d={_num(total)}",
        f"format={rgb_format}",
        f"fade=t=in:st=0:d={_num(duration)}",
        f"tpad=start_duration={_num(offset)}:start_mode=add",
    ]
    return ",".join(chain) + label


def _mask_stage(
    prev: str,
    clip_label: str,
    spec: TransitionSpec,
    mask_path: str,
    width: int,
    height: int,
    fps: float,
    offset: float,
    incoming_duration: float,
    stage: str,
    intermediate: str = "yuv420p",
    rgb_format: str = "gbrp",
) -> str:
    # threshold outputs ``max`` where mask >= ramp, else ``min``.  The ramp
    # runs 0 -> peak, so before the cut (ramp 0) every pixel takes ``max`` (the
    # outgoing clip) and after it every pixel takes ``min`` (the incoming
    # clip); dark mask areas switch first, negate reverses that order.
    peak = (1 << _pixel_depth(intermediate)) - 1
    lut = (
        f"lut=r='1+val*{peak - 2}/{peak}':g='1+val*{peak - 2}/{peak}'"
        f":b='1+val*{peak - 2}/{peak}'"
    )
    extra = ("negate," if spec.reversed else "") + f"format={rgb_format}," + lut
    # framesync (threshold) stops at the first input EOF.  The outgoing clip
    # ends when the wipe finishes, so clone its tail and keep the ramp white
    # until the incoming clip ends; otherwise the transition silently cuts
    # the remaining seconds of the timeline.
    tail = max(0.0, float(incoming_duration) - spec.duration) + 0.1
    statements = [
        f"[{prev}]format={intermediate},"
        f"tpad=stop_mode=clone:stop_duration={_num(tail)}[p0_{stage}]",
        f"[{clip_label}]tpad=start_duration={_num(offset)}:start_mode=add,"
        f"format={intermediate}[p1_{stage}]",
        _movie_source(mask_path, fps, width, height, extra, f"[mk_{stage}]"),
        _ramp_source(
            width, height, fps, spec.duration, offset, f"[thr_{stage}]",
            rgb_format, float(incoming_duration) + 0.1,
        ),
        f"[mk_{stage}][thr_{stage}][p1_{stage}][p0_{stage}]threshold,"
        f"format={intermediate}[{stage}]",
    ]
    return ";".join(statements)


def build_transition_video_graph(
    clips: list[TransitionClip],
    transitions: list[TransitionSpec | None],
    *,
    mask_dir: str | Path,
    decode_cuda: bool = False,
    pixel_format: str = "yuv420p",
    width: int | None = None,
    height: int | None = None,
    overlays: dict[str, EffectSettings] | None = None,
) -> str:
    """Build the ``-filter_complex`` video graph for one timeline render.

    ``width``/``height`` are optional keyword-only overrides for the target
    frame size used by every ``scale``; when omitted the first clip's own
    geometry is kept, which is the historical behaviour.
    """

    width, height = _frame_size(clips, width, height)
    fps = min(120.0, max(1.0, clips[0].fps or 25.0))
    intermediate = _intermediate_format(pixel_format)
    rgb_format = _rgb_format(pixel_format)
    statements: list[str] = []
    for index, clip in enumerate(clips):
        branch: list[str] = []
        download_format = _cuda_download_format(clip)
        if decode_cuda and download_format is not None:
            branch += ["hwdownload", f"format={download_format}"]
        branch += [
            # Clips can start at a non-zero PTS (MOV/TS camera files). Reset
            # each one so xfade offsets and concat lengths stay on a timeline
            # that begins at zero.
            "setpts=PTS-STARTPTS",
            f"fps={_num(fps)}",
            f"scale={width}:{height}:flags=lanczos",
            "setsar=1",
            f"format={intermediate}",
        ]
        if overlays:
            branch.append("settb=AVTB")
        effect = (overlays or {}).get(clip.path)
        if effect is None or not effects_active(effect):
            statements.append(f"[{index}:v]" + ",".join(branch) + f"[v{index}]")
            continue
        effect = validate_effects(effect, duration=clip.duration)
        if overlay_active(effect) and not Path(effect.overlay_path).is_file():
            raise ValueError(f"叠加特效素材不存在：{effect.overlay_path}")
        input_label = f"effect_input_{index}"
        effect_label = f"effect_output_{index}"
        statements.append(f"[{index}:v]" + ",".join(branch) + f"[{input_label}]")
        graph = build_effect_filter_graph(
            effect, duration=clip.duration,
            in_label=f"[{input_label}]", out_label=f"[{effect_label}]",
        )
        if overlay_active(effect):
            # The shared effect graph uses local intermediate labels. Namespace
            # them for each clip so multiple effects can coexist in one graph.
            graph = re.sub(
                r"(?<!\\)\[(bg|ov|ovs|bgs|bgp|ovp)\]",
                lambda match: f"[effect_{index}_{match.group(1)}]", graph,
            )
            statements.append(graph)
        else:
            statements.append(f"[{input_label}]{graph}[{effect_label}]")
        # Geometry-changing filters and RGB screen blending must be normalised
        # again before the shared xfade/concat stages.
        statements.append(
            f"[{effect_label}]scale={width}:{height}:flags=lanczos,"
            f"setsar=1,fps={_num(fps)},settb=AVTB,setpts=PTS-STARTPTS,format={intermediate}[v{index}]"
        )

    prev = "v0"
    elapsed_transitions = 0.0
    prefix = clips[0].duration
    for index, spec in enumerate(transitions):
        stage = f"x{index}"
        offset = prefix - elapsed_transitions - spec.duration if spec else 0.0
        if spec is None:
            statements.append(f"[{prev}][v{index + 1}]concat=n=2:v=1:a=0[{stage}]")
        elif spec.kind == "xfade":
            statements.append(
                f"[{prev}][v{index + 1}]xfade=transition={spec.xfade_mode}"
                f":duration={_num(spec.duration)}:offset={_num(offset)}[{stage}]"
            )
        else:
            mask_path = str(Path(mask_dir) / spec.mask_file)
            statements.append(
                _mask_stage(prev, f"v{index + 1}", spec, mask_path,
                            width, height, fps, offset,
                            float(clips[index + 1].duration), stage,
                            intermediate, rgb_format),
            )
        prefix += clips[index + 1].duration
        if spec is not None:
            elapsed_transitions += spec.duration
        prev = stage
    statements.append(f"[{prev}]format={pixel_format}[vout]")
    return ";".join(statements)


def build_transition_audio_graph(
    clip_count: int,
    transitions: list[TransitionSpec | None],
    *,
    durations: list[float] | None = None,
) -> str | None:
    """Normalise audio to each picture's duration before joining at its cuts."""

    if durations is not None:
        if len(durations) != clip_count or any(
            not math.isfinite(duration) or duration <= 0 for duration in durations
        ):
            raise ValueError("音频片段时长必须与画面数量一致且为正的有限数字。")

    statements: list[str] = []
    for index in range(clip_count):
        branch = (
            f"[{index}:a]asetpts=PTS-STARTPTS,aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo[a{index}]".format(index=index)
        )
        if durations is not None:
            duration = _num(durations[index])
            branch = branch.removesuffix(f"[a{index}]")
            branch += f",apad=whole_dur={duration},atrim=duration={duration}[a{index}]"
        statements.append(branch)
    prev = "a0"
    for index, spec in enumerate(transitions):
        stage = f"ao{index}"
        if spec is None:
            statements.append(f"[{prev}][a{index + 1}]concat=n=2:v=0:a=1[{stage}]")
        else:
            statements.append(
                f"[{prev}][a{index + 1}]acrossfade=d={_num(spec.duration)}[{stage}]"
            )
        prev = stage
    statements.append(f"[{prev}]anull[aout]")
    return ";".join(statements)


def total_render_duration(clips: list[TransitionClip], transitions: list[TransitionSpec | None]) -> float:
    total = sum(clip.duration for clip in clips)
    total -= sum(spec.duration for spec in transitions if spec is not None)
    return total


def build_transition_render_command(
    ffmpeg: str,
    clips: list[TransitionClip],
    transitions: list[TransitionSpec | None],
    output: str,
    *,
    mask_dir: str | Path,
    video_args: list[str],
    audio_args: list[str],
    decode_cuda: bool = False,
    gpu_index: int = 0,
    pixel_format: str = "yuv420p",
    width: int | None = None,
    height: int | None = None,
    overlays: dict[str, EffectSettings] | None = None,
) -> list[str]:
    """Assemble the full FFmpeg argv for one timeline transition render.

    ``width``/``height`` are optional keyword-only target dimensions; they
    default to the first clip's own size.
    """

    transitions = validate_transitions(clips, transitions, mask_dir=mask_dir)
    command = [
        ffmpeg, "-hide_banner", "-y", "-loglevel", "warning",
        "-stats_period", "0.25", "-progress", "pipe:1", "-nostats",
    ]
    for clip in clips:
        if decode_cuda and _cuda_download_format(clip) is not None:
            command += ["-hwaccel", "cuda", "-hwaccel_device", str(gpu_index),
                        "-hwaccel_output_format", "cuda"]
        command += ["-i", clip.path]
    video_graph = build_transition_video_graph(
        clips, transitions, mask_dir=mask_dir,
        decode_cuda=decode_cuda, pixel_format=pixel_format,
        width=width, height=height, overlays=overlays,
    )
    filter_complex = video_graph
    has_audio = all(clip.has_audio for clip in clips)
    if has_audio:
        audio_graph = build_transition_audio_graph(
            len(clips), transitions, durations=[clip.duration for clip in clips],
        )
        assert audio_graph is not None
        filter_complex += ";" + audio_graph
    command += ["-filter_complex", filter_complex]
    command += ["-map", "[vout]"]
    if has_audio:
        command += ["-map", "[aout]"]
    else:
        command += ["-an"]
    command += list(video_args)
    if has_audio:
        command += list(audio_args)
    command += [output]
    return command

