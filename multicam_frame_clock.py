"""Conservative metadata gates for phase-preserving render partitions.

Equal nominal/average rates and an integral timestamp step make a source
eligible for CFR partition planning. These metadata checks do not prove that
every frame in a file has a constant duration; unknown timing stays unsplit.
"""
from __future__ import annotations

from fractions import Fraction
import json
import math
import subprocess
from typing import Iterable

from exporter_core import CREATE_NO_WINDOW, ExportCancelled


_NTSC_RATES = tuple(Fraction(value, 1001) for value in
                    (12000, 15000, 24000, 30000, 48000, 60000, 120000, 240000))


def canonical_frame_rate(fps: int | float | Fraction) -> Fraction:
    """Recover common NTSC clocks rounded by the UI without changing custom rates."""
    if isinstance(fps, bool) or not isinstance(fps, (int, float, Fraction)):
        raise ValueError("帧率必须是正数。")
    value = float(fps)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("帧率必须是正数。")
    for rate in _NTSC_RATES:
        if abs(value - float(rate)) <= 1e-6:
            return rate
    return Fraction(fps).limit_denominator(100_000)


def _check_cancelled(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ExportCancelled("多机位导出已取消")


def probe_partition_rates(ffprobe: str, paths: Iterable[str], cancel_event=None) -> dict[str, Fraction]:
    """Read timing metadata, omitting files that cannot safely enter the planner.

    Fractions are compared exactly: a rounded average close to the nominal
    rate is insufficient. An integral number of time-base ticks per frame
    also avoids treating millisecond-quantized VFR-like clocks as exact CFR.
    No media is decoded or modified by this metadata-only probe.
    """
    rates: dict[str, Fraction] = {}
    _check_cancelled(cancel_event)
    for path in dict.fromkeys(str(path) for path in paths):
        _check_cancelled(cancel_event)
        try:
            result = subprocess.run(
                [str(ffprobe), "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=avg_frame_rate,r_frame_rate,time_base:stream_disposition=attached_pic",
                 "-of", "json", path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace", timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
            _check_cancelled(cancel_event)
            if result.returncode:
                continue
            payload = json.loads(result.stdout)
            streams = payload.get("streams") or []
            if not streams:
                continue
            stream = streams[0]
            if (stream.get("disposition") or {}).get("attached_pic"):
                continue
            average = Fraction(str(stream.get("avg_frame_rate", "0/0")))
            nominal = Fraction(str(stream.get("r_frame_rate", "0/0")))
            time_base = Fraction(str(stream.get("time_base", "0/0")))
            if average <= 0 or average != nominal or time_base <= 0:
                continue
            ticks_per_frame = 1 / average / time_base
            if ticks_per_frame.denominator != 1 or ticks_per_frame < 1:
                continue
            rates[path] = average
        except (OSError, subprocess.SubprocessError, ValueError, TypeError,
                ZeroDivisionError, OverflowError, AttributeError):
            # One unreadable/unsupported camera does not prevent exporting
            # other cameras; its original programme segments remain intact.
            _check_cancelled(cancel_event)
            continue
    _check_cancelled(cancel_event)
    return rates
