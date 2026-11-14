"""Strategy health: rolling Sharpe and a one-sided CUSUM drift alarm that can auto-disable a strategy.

Flow stage: Strategy health
Status: PHASE 3 IMPLEMENTED (2026-11-14 input validation; 2026-11-14 NaN-safe CUSUM)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/live_monitor.py
    (the engine has drawdown/fill-quality monitoring; this drift alarm is the
    local implementation and is NOT a copy of it)

WHY THE VALIDATION MATTERS
--------------------------
This module decides when to DISABLE a strategy. A false negative here is silent:
the strategy keeps trading while it degrades. A false positive stops a working
strategy, which is also expensive. Both were possible:

1. **A NaN return silently disabled the alarm.** ``min(0.0, s + (nan - ...))``
   evaluates to ``0.0`` in CPython (``min`` returns the first argument when the
   comparison is False), so the cumulative sum RESET on every NaN and the alarm
   could never fire again. One bad mark-to-market silently switched off drift
   detection. NaN is now skipped rather than resetting the sum.

2. ``h <= 0`` fired the alarm immediately (any negative sum trips ``s < -h``).
   A zero threshold is a configuration error, not an alarm.

3. ``window = 0`` produced NaN with a RuntimeWarning instead of an error.

NAN POLICY
----------
A missing return is not evidence that the strategy is healthy, so it must not be
counted as a ZERO either (that would dilute the mean upward). It is skipped, and
the sample size therefore shrinks - which the caller can see in the report.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence

import numpy as np

__all__ = ["rolling_sharpe", "cusum_alarm", "cusum_detail", "emit_deviation_alert"]


def _finite(returns: Sequence[float]) -> List[float]:
    """Finite values only. NaN is skipped, never treated as a zero return.

    ``returns or []`` is NOT used: a numpy array raises on a truth-value test, and
    callers legitimately pass arrays. Iterate None explicitly instead.
    """
    if returns is None:
        return []
    out: List[float] = []
    for x in returns:
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out.append(v)
    return out


def rolling_sharpe(returns: Sequence[float], window: int = 60, periods: int = 252) -> np.ndarray:
    """Rolling annualised Sharpe. Positions before the first full window are NaN."""
    if window < 2:
        raise ValueError(f"window must be >= 2, got {window}")
    if periods <= 0:
        raise ValueError(f"periods must be > 0, got {periods}")
    r = np.asarray([float(x) if x is not None else np.nan for x in returns], float)
    out = np.full(len(r), np.nan)
    for i in range(window, len(r) + 1):
        w = r[i - window:i]
        w = w[np.isfinite(w)]
        if w.size < 2:
            continue
        s = w.std(ddof=1)
        # A constant series has a std of ~1e-18, not exactly 0, so ``s == 0`` is
        # False and the ratio explodes to about 9e+16 - an absurd Sharpe that ranks
        # a do-nothing strategy as the best in the book. Compare against a tiny
        # floor relative to the mean instead of testing for exact zero.
        scale = max(abs(float(w.mean())), 1e-12)
        if not np.isfinite(s) or s <= 1e-12 * scale:
            out[i - 1] = np.nan
            continue
        out[i - 1] = w.mean() / s * np.sqrt(periods)
    return out


def cusum_detail(returns: Sequence[float], mu0: float, k: float, h: float
                 ) -> Dict[str, Any]:
    """Lower CUSUM with the numbers, so a fired alarm can be explained.

        {"fired": bool, "statistic": float, "n_used": int, "h": float}

    ``h`` must be positive: a zero or negative threshold trips on any negative
    deviation, which is a configuration error rather than an alarm.
    """
    if not isinstance(h, (int, float)) or not math.isfinite(float(h)) or float(h) <= 0:
        raise ValueError(f"h must be a positive number, got {h!r}")
    if not isinstance(k, (int, float)) or not math.isfinite(float(k)):
        raise ValueError(f"k must be a finite number, got {k!r}")

    s = 0.0
    used = 0
    for x in _finite(returns):
        used += 1
        # NaN never reaches here (filtered), so a gap cannot reset the sum.
        s = min(0.0, s + (x - (mu0 - k)))
        if s < -float(h):
            return {"fired": True, "statistic": round(s, 6), "n_used": used, "h": float(h)}
    return {"fired": False, "statistic": round(s, 6), "n_used": used, "h": float(h)}


def cusum_alarm(returns: Sequence[float], mu0: float, k: float, h: float) -> bool:
    """Lower CUSUM: alarm when the mean falls below mu0 by more than k, accumulated past h."""
    return bool(cusum_detail(returns, mu0, k, h)["fired"])


def emit_deviation_alert(alerts, component: str, returns: Sequence[float], mu0: float,
                         k: float, h: float) -> bool:
    """Run the CUSUM drift alarm and emit an ERROR alert when it fires (flow item 22).

    ``alerts`` is any object with emit(level, component, message), e.g.
    notify.alerts.Alerts. The message now carries the statistic and sample size, so
    an operator can see WHY it fired instead of only that it did.
    """
    detail = cusum_detail(returns, mu0, k, h)
    if detail["fired"]:
        alerts.emit("ERROR", component,
                    f"strategy deviation: CUSUM drift alarm fired "
                    f"(statistic {detail['statistic']} beyond -h={detail['h']}, "
                    f"n={detail['n_used']})")
    return bool(detail["fired"])
