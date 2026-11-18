"""Find highlight moments in a local video (your own gameplay, or a VOD a
campaign told you to clip) when there is no live chat to listen to.

Signal: loudness. Shouting, laughing and explosions push short-term RMS well
above the recording's running median.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from pathlib import Path

from . import tools

_RMS = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[\d.]+|-inf)")


@dataclass
class Moment:
    at: float  # seconds into the file (peak)
    score: float  # dB above the local median


def loudness_series(path: Path, step: float = 1.0) -> list[float]:
    """RMS level (dBFS) per `step` seconds of audio."""
    n = int(16000 * step)
    proc = tools.run([
        tools.ffmpeg(), "-hide_banner", "-nostats", "-i", str(path), "-vn",
        "-af", f"aresample=16000,asetnsamples=n={n}:p=0,astats=metadata=1:reset=1,"
               "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-f", "null", "-",
    ], timeout=3600)
    out = []
    for m in _RMS.finditer(proc.stdout + proc.stderr):
        v = m.group(1)
        out.append(-90.0 if v == "-inf" else max(-90.0, float(v)))
    return out


def find_moments(
    series: list[float],
    step: float = 1.0,
    context: int = 120,
    min_excess_db: float = 9.0,
    min_len: int = 2,
    cooldown: float = 45.0,
    top: int = 10,
) -> list[Moment]:
    """Peaks where loudness stays >= min_excess_db above the rolling median for
    at least `min_len` steps. Best `top` moments, at least `cooldown` apart."""
    cands: list[Moment] = []
    run_start, run_peak, run_best = None, 0.0, 0.0
    for i, v in enumerate(series):
        lo, hi = max(0, i - context), min(len(series), i + context)
        med = statistics.median(series[lo:hi])
        excess = v - med
        if excess >= min_excess_db and v > -50:
            if run_start is None:
                run_start, run_peak, run_best = i, i, excess
            elif excess > run_best:
                run_peak, run_best = i, excess
        elif run_start is not None:
            if i - run_start >= min_len:
                cands.append(Moment(run_peak * step, round(run_best + 0.5 * (i - run_start), 2)))
            run_start = None
    if run_start is not None and len(series) - run_start >= min_len:
        cands.append(Moment(run_peak * step, round(run_best, 2)))
    cands.sort(key=lambda m: m.score, reverse=True)
    picked: list[Moment] = []
    for m in cands:
        if all(abs(m.at - p.at) >= cooldown for p in picked):
            picked.append(m)
        if len(picked) >= top:
            break
    return sorted(picked, key=lambda m: m.at)
