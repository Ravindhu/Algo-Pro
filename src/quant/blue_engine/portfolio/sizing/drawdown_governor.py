"""
sizing/drawdown_governor.py - de-risk on drawdown / vol-of-vol (pure).

The vol target reacts to *forecast* volatility; the drawdown governor reacts to
*realised* loss from a peak. Two classic, complementary governors:

* **Drawdown governor** — as the equity curve falls from its high-water mark,
  cut gross along a schedule (e.g. -5% -> 0.85x, -10% -> 0.6x, -20% -> 0.25x).
  This is the "live to trade another day" rule.
* **Vol-of-vol governor** — when the volatility *of volatility* spikes, the
  regime is unstable and correlations converge to 1; de-risk pre-emptively.

Both are pure functions of an equity series and/or a vol series, so they are
unit-testable with a hand-built curve.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...core.results import clamp, safe_float, stdev

logger = logging.getLogger(__name__)

__all__ = ["drawdown_scale", "vol_of_vol_scale", "DrawdownGovernor",
           "DEFAULT_SCHEDULE"]

#: Default de-risk schedule: (drawdown_threshold, gross_scale). Applied as a
#: step function — the *deepest* breached threshold wins.
DEFAULT_SCHEDULE: List[Tuple[float, float]] = [
    (0.05, 0.85),
    (0.10, 0.60),
    (0.15, 0.40),
    (0.20, 0.25),
    (0.30, 0.10),
]


def drawdown_scale(drawdown: Optional[float], *,
                   schedule: Optional[Sequence[Tuple[float, float]]] = None
                   ) -> Dict[str, Any]:
    """Gross scale from the current drawdown (positive fraction, e.g. 0.12).

    Returns ``{"scale", "threshold", "drawdown", "breached", "reason"}``. A
    drawdown below the smallest threshold returns 1.0 (no de-risk).
    """
    dd = safe_float(drawdown)
    if dd is None or dd < 0:
        dd = abs(dd) if dd is not None else None
    if dd is None:
        return {"scale": 1.0, "threshold": None, "drawdown": None,
                "breached": False, "reason": "no drawdown supplied"}
    sch = sorted(schedule or DEFAULT_SCHEDULE, key=lambda x: x[0])
    scale, threshold, breached = 1.0, None, False
    for thr, sc in sch:
        if dd >= thr:
            scale, threshold, breached = sc, thr, True
    return {
        "scale": round(clamp(scale, 0.0, 1.0), 6),
        "threshold": threshold,
        "drawdown": round(dd, 6),
        "breached": breached,
        "reason": (f"drawdown {dd * 100:.1f}% >= {threshold * 100:.0f}% -> "
                   f"x{scale:.2f}" if breached else "drawdown within tolerance"),
    }


def vol_of_vol_scale(vol_series: Optional[Sequence[float]], *,
                     window: int = 20, threshold: float = 0.5,
                     floor: float = 0.4) -> Dict[str, Any]:
    """Gross scale from the volatility-of-volatility.

    ``vov = std(vol_series, window)``. When ``vov`` exceeds ``threshold`` (in the
    same units as vol), scale linearly down to ``floor`` at ``2*threshold``.
    """
    vs = [safe_float(v) for v in (vol_series or [])]
    vs = [v for v in vs if v is not None]
    if len(vs) < max(window, 3):
        return {"scale": 1.0, "vol_of_vol": None, "reason": "insufficient vol history"}
    recent = vs[-window:]
    vov = stdev(recent)
    if vov is None or threshold <= 0:
        return {"scale": 1.0, "vol_of_vol": None, "reason": "degenerate vol series"}
    if vov <= threshold:
        scale = 1.0
    else:
        # Linear from 1.0 at `threshold` to `floor` at `2*threshold`, then clipped.
        frac = (vov - threshold) / threshold
        scale = clamp(1.0 - frac * (1.0 - floor), floor, 1.0)
    return {"scale": round(scale, 6), "vol_of_vol": round(vov, 6),
            "threshold": threshold,
            "reason": (f"vol-of-vol {vov:.4f} vs {threshold} -> x{scale:.2f}"
                       if scale < 1.0 else "vol-of-vol within tolerance")}


class DrawdownGovernor:
    """Stateful drawdown + vol-of-vol governor (tracks the high-water mark)."""

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Any] = None):
        cfg = dict(config or {})
        raw = cfg.get("schedule")
        self.schedule: List[Tuple[float, float]] = (
            [(float(a), float(b)) for a, b in raw] if raw else DEFAULT_SCHEDULE)
        self.peak: Optional[float] = None
        self.vol_window = int(cfg.get("vol_window", 20))
        self.vov_threshold = float(cfg.get("vov_threshold", 0.5))
        self.vov_floor = float(cfg.get("vov_floor", 0.4))

    def observe(self, equity: Optional[float]) -> Optional[float]:
        """Update the high-water mark from a new equity value; return drawdown."""
        e = safe_float(equity)
        if e is None:
            return None
        self.peak = e if self.peak is None else max(self.peak, e)
        if self.peak and self.peak != 0:
            return max((self.peak - e) / self.peak, 0.0)
        return 0.0

    def compute(self, equity: Optional[float],
                vol_series: Optional[Sequence[float]] = None
                ) -> Dict[str, Any]:
        dd = self.observe(equity)
        dd_out = drawdown_scale(dd, schedule=self.schedule)
        vov_out = vol_of_vol_scale(vol_series, window=self.vol_window,
                                   threshold=self.vov_threshold,
                                   floor=self.vov_floor)
        # The binding governor is the more conservative one.
        scale = min(dd_out["scale"], vov_out["scale"])
        binding = ("drawdown" if dd_out["scale"] <= vov_out["scale"]
                   else "vol_of_vol")
        return {
            "scale": round(scale, 6),
            "binding": binding,
            "drawdown": dd_out,
            "vol_of_vol": vov_out,
            "reason": (dd_out["reason"] if binding == "drawdown"
                       else vov_out["reason"]),
        }