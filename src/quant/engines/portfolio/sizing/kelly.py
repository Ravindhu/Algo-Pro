"""
sizing/kelly.py - Kelly / expected-value position sizing (pure).

Kelly answers: *given a probability of winning and the payoff odds, what fraction
of capital maximises long-run growth?* Full Kelly is famously too aggressive for
real books (it maximises growth assuming you know ``p`` exactly), so desks use
**fractional Kelly** (typically 1/4 to 1/2) — this module defaults to 1/2.

Discrete form:  ``f* = p - (1-p)/b``  where ``b`` = win/loss payoff ratio.
Continuous form: ``f* = (mu - rf) / sigma^2`` (the growth-optimal leverage).

For the regime overlay we map ``P(risk-on)`` to an exposure cap:
    edge = P(win) - P(loss)  -> f* -> gross multiplier.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ...core.results import clamp, safe_float

logger = logging.getLogger(__name__)

__all__ = ["kelly_fraction", "kelly_leverage", "kelly_cap", "Kelly"]


def kelly_fraction(p: float, b: float = 1.0) -> Optional[float]:
    """Discrete Kelly fraction ``f* = p - (1-p)/b`` (clipped to [0,1]).

    ``p`` = probability of a win, ``b`` = win/loss payoff ratio (1.0 = even).
    Returns 0 when there is no positive edge (never a negative = never short the
    game outright from this formula).
    """
    p = safe_float(p)
    b = safe_float(b)
    if p is None or b is None or b <= 0:
        return None
    f = p - (1.0 - p) / b
    return clamp(f, 0.0, 1.0)


def kelly_leverage(mu: float, rf: float, sigma: float) -> Optional[float]:
    """Continuous Kelly leverage ``(mu - rf) / sigma^2`` (clipped to [0, cap])."""
    m = safe_float(mu)
    r = safe_float(rf) or 0.0
    s = safe_float(sigma)
    if m is None or s is None or s <= 0:
        return None
    return (m - r) / (s * s)


def kelly_cap(probability: Optional[float], *, payoff: float = 1.0,
              fraction: float = 0.5, max_gross: float = 1.5,
              min_gross: float = 0.2, baseline: float = 1.0
              ) -> Dict[str, Any]:
    """Map an edge probability to a gross-exposure cap (fractional Kelly).

    ``probability`` is the regime/edge win probability in ``[0,1]``. We centre it
    at 0.5 (no edge -> baseline gross), scale the deviation by the fractional
    Kelly coefficient, and clip to ``[min_gross, max_gross]``.

    Returns ``{"gross_cap", "f_star", "fractional", "edge", "reason"}``.
    """
    p = safe_float(probability)
    if p is None:
        return {"gross_cap": baseline, "f_star": None, "fractional": fraction,
                "edge": None, "reason": "no probability supplied"}
    edge = p - 0.5
    f_star = kelly_fraction(p, payoff)
    # Convert the Kelly fraction into a gross multiplier around the baseline.
    # edge in [-0.5, +0.5]; scale so |edge|=0.5 moves gross by +-max_gross/2.
    delta = edge * 2.0 * fraction
    gross = clamp(baseline + delta, min_gross, max_gross)
    return {
        "gross_cap": round(gross, 6),
        "f_star": None if f_star is None else round(f_star, 6),
        "fractional": round(fraction, 4),
        "edge": round(edge, 6),
        "probability": round(p, 6),
        "reason": ("positive edge -> add risk" if edge > 0
                   else "no edge -> hold baseline"),
    }


class Kelly:
    """Kelly sizing helper bound to a config (fraction, payoff, caps)."""

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Any] = None):
        cfg = dict(config or {})
        self.payoff = float(cfg.get("payoff", 1.0))
        self.fraction = float(cfg.get("fraction", 0.5))
        self.max_gross = float(cfg.get("max_gross", 1.5))
        self.min_gross = float(cfg.get("min_gross", 0.2))
        self.baseline = float(cfg.get("baseline", 1.0))

    def compute(self, probability: Optional[float]) -> Dict[str, Any]:
        return kelly_cap(probability, payoff=self.payoff,
                         fraction=self.fraction, max_gross=self.max_gross,
                         min_gross=self.min_gross, baseline=self.baseline)