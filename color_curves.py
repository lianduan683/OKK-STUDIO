"""Natural cubic colour curves shared by the editor and realtime renderer.

Coordinates use the normalized display signal.  The spline has natural end
conditions; RGB curves are evaluated before the shared master curve.  This
module contains no player, UI or media-processing dependencies.
"""
from __future__ import annotations

from bisect import bisect_right
from functools import lru_cache
import math


DEFAULT_CURVE = ((0.0, 0.0), (1.0, 1.0))
CURVE_CHANNELS = ('master', 'r', 'g', 'b')
MAX_CURVE_POINTS = 8
MIN_CURVE_SPACING = 1.0 / 65535.0


def canonical_curve(points) -> tuple[tuple[float, float], ...]:
    """Validate a render-safe curve and return immutable numeric coordinates."""
    if not isinstance(points, (list, tuple)) or not 2 <= len(points) <= MAX_CURVE_POINTS:
        raise ValueError('每条曲线必须包含 2 到 8 个控制点')
    result = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError('曲线控制点必须包含 X 和 Y 两个坐标')
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or not 0 <= value <= 1 for value in point):
            raise ValueError('曲线坐标必须是 0 到 1 之间的有限数字')
        x, y = map(float, point)
        if result and x <= result[-1][0]:
            raise ValueError('曲线控制点的 X 必须严格递增')
        if result and x-result[-1][0] < MIN_CURVE_SPACING:
            raise ValueError('曲线控制点过近，X 间距必须至少为 1/65535')
        result.append((x, y))
    if result[0][0] != 0.0 or result[-1][0] != 1.0:
        raise ValueError('曲线两端的 X 必须固定为 0 和 1')
    return tuple(result)


@lru_cache(maxsize=256)
def _coefficients(points):
    count = len(points)
    intervals = [points[index+1][0]-points[index][0] for index in range(count-1)]
    slopes = [(points[index+1][1]-points[index][1])/intervals[index]
              for index in range(count-1)]
    # Solve for second derivatives with the two endpoint derivatives fixed
    # to zero.  Thomas elimination is sufficient for this positive system.
    diagonal = [1.0]*count
    upper = [0.0]*count
    rhs = [0.0]*count
    for index in range(1, count-1):
        diagonal[index] = 2.0*(intervals[index-1]+intervals[index])
        upper[index] = intervals[index]
        rhs[index] = 6.0*(slopes[index]-slopes[index-1])
        factor = intervals[index-1]/diagonal[index-1]
        diagonal[index] -= factor*upper[index-1]
        rhs[index] -= factor*rhs[index-1]
    second = [0.0]*count
    for index in range(count-2, 0, -1):
        second[index] = (rhs[index]-upper[index]*second[index+1])/diagonal[index]
    result = []
    for index, width in enumerate(intervals):
        x0, a = points[index]
        b = slopes[index]-width*(2.0*second[index]+second[index+1])/6.0
        c = second[index]/2.0
        d = (second[index+1]-second[index])/(6.0*width)
        result.append((x0, points[index+1][0], a, b, c, d))
    return tuple(result)


def curve_coefficients(points) -> tuple[tuple[float, float, float, float, float, float], ...]:
    """Return (x0,x1,a,b,c,d) intervals for y=a+b*t+c*t²+d*t³, t=x-x0."""
    return _coefficients(canonical_curve(points))


def evaluate_curve(points, value: float) -> float:
    """Evaluate the same natural spline and clip the displayed output to 0–1."""
    points = canonical_curve(points)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('曲线输入必须是有限数字')
    x = min(1.0, max(0.0, float(value)))
    index = max(0, min(len(points)-2, bisect_right(points, (x, math.inf))-1))
    x0, _, a, b, c, d = _coefficients(points)[index]
    t = x-x0
    return min(1.0, max(0.0, a+t*(b+t*(c+t*d))))
