"""
scoring.py - pure signal scoring (level, change, and surprise, blended).

The baseline engine scored each signal as ``tanh(level / scale)``. This module
generalises that into a three-view blend so the composite reflects *what markets
price* rather than a raw level:

    level      tanh(value / scale)                 structural position
    delevel    tanh(z_of_change)                   pace vs own recent trend
    surprise   tanh(z_of_surprise)                 print vs expectation

A per-signal ``blend`` weight selects how much each view contributes (defaults
lean on surprise, which the desk found most informative; see plan weakness #1).
All views are signed by the signal *direction* (+1 higher=risk-on).

Pure: no I/O, no numpy. Uses only stdlib + the shared helpers.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from ..core.results import clamp, safe_float

#: Default view blend. Surprise dominates, level anchors, delevel seasons.
DEFAULT_BLEND = {"level": 0.35, "delevel": 0.25, "surprise": 0.40}


def saturate(x: float) -> float:
    """tanh with NaN safety (saturates to (-1, 1))."""
    if x != x:  # NaN
        return 0.0
    return math.tanh(x)


def level_view(value: Optional[float], scale: float) -> Optional[float]:
    """Structural view: tanh(value / scale)."""
    if value is None:
        return None
    scale = safe_float(scale) or 1.0
    return saturate(value / scale)


def change_view(z_delta: Optional[float]) -> Optional[float]:
    """Pace view: tanh of the standardised change."""
    if z_delta is None:
        return None
    return saturate(z_delta)


def surprise_view(z_surprise: Optional[float]) -> Optional[float]:
    """Expectation view: tanh of the standardised surprise."""
    if z_surprise is None:
        return None
    return saturate(z_surprise)


def score_signal(*, direction: float, level: Optional[float],
                 scale: float, z_delta: Optional[float] = None,
                 z_surprise: Optional[float] = None,
                 blend: Optional[Dict[str, float]] = None
                 ) -> Dict[str, Any]:
    """Blend the three standardised views into one signed score in [-1, 1].

    ``direction`` is +1 (higher = risk-on) or -1 (higher = risk-off). Views that
    are missing are dropped and the remaining blend weights renormalised, so a
    signal with only a level still scores cleanly (backwards compatible with the
    baseline behaviour).
    """
    blend = {**DEFAULT_BLEND, **(blend or {})}
    # Sign each view by direction *before* blending, so every view is already
    # in risk-space (positive = risk-ON). This is what makes a below-forecast
    # CPI a risk-ON surprise: direction=-1 flips tanh(z) into +.
    views: Dict[str, Optional[float]] = {
        "level": _sign(level_view(level, scale), direction),
        "delevel": _sign(change_view(z_delta), direction),
        "surprise": _sign(surprise_view(z_surprise), direction),
    }

    weighted = 0.0
    weight_total = 0.0
    used: Dict[str, float] = {}
    for name, raw in views.items():
        if raw is None:
            continue
        w = max(0.0, safe_float(blend.get(name)) or 0.0)
        if w == 0:
            continue
        weighted += raw * w
        weight_total += w
        used[name] = round(raw, 4)

    base = (weighted / weight_total) if weight_total > 0 else 0.0
    signed = clamp(base, -1.0, 1.0)
    return {
        "score": round(signed, 4),
        "raw": round(base, 4),
        "views": used,
        "views_used": sorted(used.keys()),
    }


def _sign(value: Optional[float], direction: float) -> Optional[float]:
    """Flip a view into risk-space using the signal direction (+1/-1)."""
    if value is None:
        return None
    return clamp(direction * value, -1.0, 1.0)


__all__ = [
    "saturate", "level_view", "change_view", "surprise_view",
    "score_signal", "DEFAULT_BLEND",
]