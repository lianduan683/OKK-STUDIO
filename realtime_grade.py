"""Live display-referred primary grading on MPV's GPU renderer.

The shader is written once. Slider/wheel drags only update DYNAMIC uniforms
through glsl-shader-opts; decoded frames, playback time and pause stay intact.
The maths follows creative_core's export chain. GPU floating point and the
export filters' integer LUT quantisation can differ by a small rounding error.

Public references checked against MPV 0.41 / libplacebo 7.371:
https://mpv.io/manual/stable/#options-glsl-shader-opts
https://libplacebo.org/custom-shaders/#tunable-parameters
"""
from __future__ import annotations

from pathlib import Path
import tempfile

from creative_core import ColorGrade, GRADE_RANGES, WHEEL_RANGES, SKIN_RANGES, WB_RANGES, grade_is_identity, validate_grade
from color_curves import DEFAULT_CURVE, canonical_curve, curve_coefficients
from portrait_color import GLSL_PORTRAIT


CONTROLS = tuple(GRADE_RANGES) + tuple(WHEEL_RANGES)
ALL_CONTROLS = (*CONTROLS,*SKIN_RANGES,*WB_RANGES)
CURVES = ("curve_r", "curve_g", "curve_b", "curve_master")
COEFFICIENTS = ("x0", "x1", "a", "b", "c", "d")


def _curve_uniforms(grade: ColorGrade, prefix: str) -> dict[str, float]:
    values = {prefix + "custom_curves": float(any(
        canonical_curve(getattr(grade, name, DEFAULT_CURVE)) != DEFAULT_CURVE for name in CURVES))}
    for name in CURVES:
        intervals = curve_coefficients(getattr(grade, name, DEFAULT_CURVE))
        values[prefix + name + "_count"] = float(len(intervals))
        for index in range(7):
            interval = intervals[index] if index < len(intervals) else (0., 1., 0., 1., 0., 0.)
            for label, value in zip(COEFFICIENTS, interval):
                values[f"{prefix}{name}_{index}_{label}"] = float(value)
    return values


def validate_preview_target(target) -> tuple:
    if not isinstance(target, (list, tuple)) or not target:
        raise ValueError("实时调色目标无效")
    target = tuple(target)
    if target == ("global",):
        return target
    if len(target) == 2 and target[0] == "camera" and type(target[1]) is int and target[1] > 0:
        return target
    if len(target) == 3 and target[0] == "selection":
        import math
        first, last = target[1:]
        if (not isinstance(first, bool) and not isinstance(last, bool)
                and isinstance(first, (int, float)) and isinstance(last, (int, float))
                and math.isfinite(first) and math.isfinite(last) and 0 <= first < last):
            return target
    raise ValueError("实时调色目标无效")


def grade_uniforms(grades) -> tuple[dict[str, float], tuple[str, ...]]:
    """Three independent, ordered grades: camera, local range, global."""
    values = {}
    notices = []
    grades = list(grades)
    if len(grades) > 3:
        raise ValueError("实时调色最多支持机位、片段和全局三层")
    for index in range(3):
        grade = grades[index] if index < len(grades) and grades[index] is not None else ColorGrade()
        validate_grade(grade, check_lut=False)
        enabled = grade.enabled and not grade_is_identity(grade)
        values[f"g{index}_enabled"] = 1.0 if enabled else 0.0
        for name in ALL_CONTROLS:
            values[f"g{index}_{name}"] = float(getattr(grade, name))
        values.update(_curve_uniforms(grade, f"g{index}_"))
        if grade.enabled and grade.lut_path:
            notices.append("LUT 不在实时色轮预览中显示；请使用‘渲染效果预览’核对 LUT 成片效果。")
    return values, tuple(dict.fromkeys(notices))


_MATH = r"""
float rg_max(vec3 v) { return max(v.r, max(v.g, v.b)); }
float rg_min(vec3 v) { return min(v.r, min(v.g, v.b)); }
vec3 rg_balance(vec3 c, float temperature, float tint) {
    // Match the export's midtone weight and preserve HSL lightness.
    float original_sum = rg_max(c) + rg_min(c);
    float w = clamp((original_sum - 0.333) * 4.0 + 0.5, 0.0, 1.0)
            * clamp((1.0 - original_sum - 0.333) * 4.0 + 0.5, 0.0, 1.0) * 0.7;
    c = clamp(c + w * vec3(temperature * 0.25 - tint * 0.1,
                          tint * 0.2, -temperature * 0.25 - tint * 0.1), 0.0, 1.0);
    float hi = rg_max(c), lo = rg_min(c), delta = hi - lo;
    float h = 0.0;
    if (delta > 0.0000001) {
        if (hi == c.r) h = (c.g - c.b) / delta;
        else if (hi == c.g) h = 2.0 + (c.b - c.r) / delta;
        else h = 4.0 + (c.r - c.g) / delta;
        h = mod(h, 6.0);
    }
    float l = original_sum * 0.5;
    float s = (hi >= 1.0 || lo <= 0.0) ? 0.0 : delta / max(0.0000001, 1.0 - abs(2.0 * l - 1.0));
    vec3 k = mod(vec3(0.0, 8.0, 4.0) + h * 2.0, 12.0);
    return clamp(l - s * min(l, 1.0 - l) * max(min(min(k - 3.0, 9.0 - k), 1.0), -1.0), 0.0, 1.0);
}
vec3 rg_curve(vec3 x, float shadows, float highlights) {
    // Natural cubic spline through the same four control points as export.
    float y1 = 0.25 + shadows * 0.2, y2 = 0.75 + highlights * 0.2;
    float r1 = 6.0 * (2.0 * y2 - 6.0 * y1);
    float r2 = 6.0 * (4.0 - 6.0 * y2 + 2.0 * y1);
    float m1 = (1.5 * r1 - 0.5 * r2) * 0.5;
    float m2 = (1.5 * r2 - 0.5 * r1) * 0.5;
    vec3 outv;
    for (int i = 0; i < 3; i++) {
        float a, b, h, yl, yr, ml, mr;
        if (x[i] < 0.25) { h=0.25; b=x[i]/h; yl=0.0; yr=y1; ml=0.0; mr=m1; }
        else if (x[i] < 0.75) { h=0.5; b=(x[i]-0.25)/h; yl=y1; yr=y2; ml=m1; mr=m2; }
        else { h=0.25; b=(x[i]-0.75)/h; yl=y2; yr=1.0; ml=m2; mr=0.0; }
        a = 1.0 - b;
        outv[i] = clamp(a*yl + b*yr + ((a*a*a-a)*ml + (b*b*b-b)*mr)*h*h/6.0, 0.0, 1.0);
    }
    return outv;
}
"""


def shader_source() -> str:
    """One fixed shader; no generated code or shader reload per drag."""
    defaults = ColorGrade()
    declarations = []
    transforms = []
    custom_functions = []
    for index in range(3):
        prefix = f"g{index}_"
        for name in ("enabled", *ALL_CONTROLS):
            default = 0.0 if name == "enabled" else getattr(defaults, name)
            declarations.append(f"//!PARAM {prefix}{name}\n//!TYPE DYNAMIC float\n{float(default):.9g}\n")
        for name, value in _curve_uniforms(defaults, prefix).items():
            declarations.append(f"//!PARAM {name}\n//!TYPE DYNAMIC float\n{value:.9g}\n")
        for curve in CURVES:
            clauses = []
            for interval in range(7):
                label = f"{prefix}{curve}_{interval}_"
                clauses.append(f"""if ({interval}.0 < {prefix}{curve}_count &&
                    (x <= {label}x1 || {interval + 1}.0 >= {prefix}{curve}_count)) {{
                    float t=x-{label}x0;
                    return clamp({label}a + t*({label}b + t*({label}c + t*{label}d)), 0.0, 1.0);
                }}""")
            custom_functions.append(f"float rg_custom{index}_{curve}(float x) {{ x=clamp(x,0.0,1.0); "
                                    + "\n".join(clauses) + " return x; }\n")
        vector = lambda wheel: "vec3(" + ",".join(f"{prefix}{wheel}_{c}+{prefix}{wheel}_y" for c in "rgb") + ")"
        transforms.append(f"""
vec3 rg_grade{index}(vec3 c) {{
    if ({prefix}enabled < 0.5) return c;
    c=clamp(c*vec3({prefix}wb_red,{prefix}wb_green,{prefix}wb_blue),0.0,1.0);
    c = pow(clamp((c*exp2({prefix}exposure)-0.5)*{prefix}contrast+0.5,0.0,1.0), vec3(1.0/{prefix}gamma));
    vec3 lift = {vector('lift')}, gain = max(vec3(0.0), 1.0 + {vector('gain')});
    vec3 gm = max(vec3(0.05), 1.0 + {vector('gamma')}), off = {vector('offset')};
    c = clamp(pow(clamp((c*(1.0-lift)+lift)*gain,0.0,1.0),1.0/gm)+off,0.0,1.0);
    float sat = {prefix}saturation;
    // Above 2 the export uses two clipped channel-matrix stages.
    for (int i=0; i<2; i++) {{
        if (i == 1 && sat <= 2.0) break;
        float f = sat > 2.0 ? sqrt(sat) : sat;
        c = clamp(mix(vec3(dot(c,vec3(0.2126,0.7152,0.0722))),c,f),0.0,1.0);
    }}
    if ({prefix}temperature != 0.0 || {prefix}tint != 0.0)
        c = rg_balance(c,{prefix}temperature,{prefix}tint);
    if ({prefix}shadows != 0.0 || {prefix}highlights != 0.0)
        c = rg_curve(c,{prefix}shadows,{prefix}highlights);
    if ({prefix}custom_curves > 0.5) {{
        c = vec3(rg_custom{index}_curve_r(c.r), rg_custom{index}_curve_g(c.g), rg_custom{index}_curve_b(c.b));
        c = vec3(rg_custom{index}_curve_master(c.r), rg_custom{index}_curve_master(c.g), rg_custom{index}_curve_master(c.b));
    }}
    c = rg_portrait(c,{prefix}skin_lighten,{prefix}skin_saturation,{prefix}skin_deyellow);
    return c;
}}
""")
    return ("\n".join(declarations) + "\n//!HOOK MAIN\n//!BIND HOOKED\n//!DESC Real-time primary grading\n"
            + _MATH + GLSL_PORTRAIT + "\n".join(custom_functions) + "\n".join(transforms)
            + "\nvec4 hook() { vec4 c=HOOKED_tex(HOOKED_pos); c.rgb=rg_grade2(rg_grade1(rg_grade0(c.rgb))); return c; }\n")


class RealtimeGrade:
    """Worker-owned shader resource and per-decoder uniform change cache."""
    def __init__(self):
        self._folder = None
        self._path = None
        self._states = {}

    def _shader(self):
        if self._path is None:
            from cache_paths import cache_dir
            self._folder = tempfile.TemporaryDirectory(prefix="okk_realtime_grade_",dir=cache_dir('color/shaders'), ignore_cleanup_errors=True)
            self._path = Path(self._folder.name) / "primary_grade.glsl"
            self._path.write_text(shader_source(), encoding="utf-8")
        return str(self._path)

    def apply(self, controller, grades=(), *, bypass=False) -> tuple[str, ...]:
        values, notices = grade_uniforms(() if bypass else grades)
        key = tuple(values.items())
        previous = self._states.get(id(controller))
        if previous is not None and previous[1] == key:
            return notices
        if previous is None and not any(values[f"g{i}_enabled"] for i in range(3)):
            return notices
        # The fixed program is installed once per MPV instance. Afterwards
        # this is a uniform update, including paused-frame redraws by MPV.
        controller.command(["set_property", "glsl-shader-opts", {name: format(value, ".9g") for name, value in values.items()}])
        if previous is None:
            controller.command(["change-list", "glsl-shaders", "append", self._shader()])
        self._states[id(controller)] = (controller, key)
        return notices

    def close(self):
        self._states.clear()
        if self._folder is not None:
            self._folder.cleanup()
            self._folder = None
        self._path = None

    def forget(self,controller):
        """Release a disposed decoder's state without touching active shaders."""
        self._states.pop(id(controller),None)
