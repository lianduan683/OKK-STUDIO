"""Gentle display-referred skin-colour brightening shared by CPU and GPU.

This is a smooth colour-range selection, not face recognition or skin smoothing.
Warm colours with moderate chroma are selected; neutral, blue/green, saturated
red, deep shadows and near-white highlights are protected.
"""
from __future__ import annotations

import math
from pathlib import Path
import hashlib
import tempfile
import threading


def _clip(value):
    return min(1.0, max(0.0, float(value)))


def _smooth(low, high, value):
    t = _clip((value-low)/(high-low))
    return t*t*(3.0-2.0*t)


def skin_mask(r, g, b):
    r, g, b = map(_clip, (r, g, b))
    y = .2126*r + .7152*g + .0722*b
    cb = .5-.168736*r-.331264*g+.5*b
    cr = .5+.5*r-.418688*g-.081312*b
    distance = ((cb-.42)/.13)**2 + ((cr-.60)/.12)**2
    chroma = max(r,g,b)-min(r,g,b)
    return ((1-_smooth(.55,1.25,distance))
            * _smooth(.015,.06,r-b) * _smooth(.005,.05,r-g)
            * _smooth(.025,.09,chroma) * (1-_smooth(.45,.65,chroma))
            * _smooth(.035,.16,y) * (1-_smooth(.82,.97,y)))


def portrait_rgb(rgb, lighten=0.0, saturation=0.0, deyellow=0.0):
    """Apply restrained brightening and de-saturation to a normalized RGB pixel."""
    if (any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v)
            or not 0 <= v <= 1 for v in (lighten,saturation,deyellow))):
        raise ValueError('肤色提亮参数必须在 0 到 1 之间')
    r,g,b = map(_clip,rgb)
    mask = skin_mask(r,g,b)
    # Counter yellow/orange only inside the smooth skin-colour range.  The
    # blue correction cannot overshoot green by more than a small fraction;
    # this preserves a natural pink/peach signal instead of a blue wash.
    correction=min(max(0.,(r+g)*.5-b-.02),max(0.,g-b)*1.15)
    amount=deyellow*mask*_smooth(.002,.025,g-b)
    b+=correction*amount
    r-=max(0.,r-g)*.06*amount
    luminance = .2126*r+.7152*g+.0722*b
    white_mix = lighten*.22*mask*(1-luminance)
    desaturate = saturation*.18*mask
    values = [v+(luminance-v)*desaturate for v in (r,g,b)]
    return tuple(_clip(v+(1-v)*white_mix) for v in values)


_LUT_LOCK = threading.Lock()
_LUT_FOLDER = None


def portrait_lut_path(lighten, saturation, deyellow=0.0):
    """Cache a high-resolution 3D LUT for FFmpeg's optimized pixel transform.

    The temporary resource is derived from the saved numeric controls.
    It never replaces the user's LUT path or becomes a project dependency.
    GPU preview continues to evaluate the analytic function directly.
    """
    global _LUT_FOLDER
    portrait_rgb((0.,0.,0.),lighten,saturation,deyellow)  # Validate public parameters.
    key=hashlib.sha256(f'portrait-v2|65|{lighten:.10g}|{saturation:.10g}|{deyellow:.10g}'.encode()).hexdigest()[:24]
    with _LUT_LOCK:
        if _LUT_FOLDER is None:
            from cache_paths import cache_dir
            _LUT_FOLDER=tempfile.TemporaryDirectory(prefix='okk_portrait_luts_',dir=cache_dir('color/luts'),ignore_cleanup_errors=True)
        path=Path(_LUT_FOLDER.name)/(key+'.cube')
        if not path.is_file():
            temporary=path.with_suffix('.pending')
            try:
                with temporary.open('w',encoding='ascii',newline='\n') as stream:
                    stream.write('TITLE "OKK gentle skin-colour adjustment"\nLUT_3D_SIZE 65\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n')
                    for b in range(65):
                        for g in range(65):
                            for r in range(65):
                                out=portrait_rgb((r/64,g/64,b/64),lighten,saturation,deyellow)
                                stream.write(f'{out[0]:.9f} {out[1]:.9f} {out[2]:.9f}\n')
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return str(path)


GLSL_PORTRAIT = r"""
vec3 rg_portrait(vec3 c, float lighten, float saturation, float deyellow) {
    if (lighten <= 0.0 && saturation <= 0.0 && deyellow <= 0.0) return c;
    float y = dot(c,vec3(0.2126,0.7152,0.0722));
    float cb = 0.5 + dot(c,vec3(-0.168736,-0.331264,0.5));
    float cr = 0.5 + dot(c,vec3(0.5,-0.418688,-0.081312));
    float distance = pow((cb-0.42)/0.13,2.0) + pow((cr-0.60)/0.12,2.0);
    float chroma = max(c.r,max(c.g,c.b))-min(c.r,min(c.g,c.b));
    float mask = (1.0-smoothstep(0.55,1.25,distance))
        * smoothstep(0.015,0.06,c.r-c.b) * smoothstep(0.005,0.05,c.r-c.g)
        * smoothstep(0.025,0.09,chroma) * (1.0-smoothstep(0.45,0.65,chroma))
        * smoothstep(0.035,0.16,y) * (1.0-smoothstep(0.82,0.97,y));
    float correction=min(max(0.0,(c.r+c.g)*0.5-c.b-0.02),max(0.0,c.g-c.b)*1.15);
    float amount=deyellow*mask*smoothstep(0.002,0.025,c.g-c.b);
    c.b+=correction*amount;
    c.r-=max(0.0,c.r-c.g)*0.06*amount;
    y=dot(c,vec3(0.2126,0.7152,0.0722));
    c = mix(c,vec3(y),saturation*0.18*mask);
    return clamp(mix(c,vec3(1.0),lighten*0.22*mask*(1.0-y)),0.0,1.0);
}
"""
