"""Algo router: POV child sizing and VWAP schedule. Only gate-approved parents are routed.

Flow stage: Algo router
Status: PHASE 3 IMPLEMENTED; input validation added 2026-11-14
Existing code to reuse or wrap (do not copy): none. The Blue POV state machine was
    dashboard state and was removed 2026-11-05.

DEFECTS FIXED (2026-11-14)
--------------------------
A child quantity is a real order. Each of these produced a number that would have
been sent:

1. **A NEGATIVE POV returned a negative quantity.** ``pov_child_qty(1000, 5000,
   -0.5)`` returned ``-2500``: a child order in the opposite direction to the
   parent. Now refused.

2. **A NaN POV raised a bare ValueError** from ``int(nan)``. The caller saw a
   conversion error, not "your POV is invalid". Now refused with a clear message.

3. **A tiny bar volume silently produced 0.** Zero children every cycle means the
   parent NEVER completes and the capital sits allocated but undeployed. That is
   not a hostile input, it is a quiet market, so it is reported as a stall rather
   than an exception - but it must not be mistaken for progress.

4. ``vwap_schedule`` with an EMPTY profile raised IndexError on ``out[-1]``.
   An empty profile is a configuration error and now says so.

POV is a participation RATE, so it must be in (0, 1]. Values above 1 are still
accepted because a caller may express 10% as 10 - that is normalised - but a rate
so large the child exceeds the remaining quantity is simply capped by ``min``.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

__all__ = ["pov_child_qty", "pov_child_qty_report", "vwap_schedule"]


def _normalise_pov(pov: float) -> float:
    """Validate a participation rate. Refuses anything that could invert an order.

    A value above 1 is read as a percentage (10 -> 0.10) rather than refused,
    because that is how a config or a dashboard field often states it.
    """
    try:
        rate = float(pov)
    except (TypeError, ValueError):
        raise ValueError(f"pov must be a number, got {pov!r}")
    if not math.isfinite(rate):
        raise ValueError(f"pov must be finite, got {pov!r}")
    if rate <= 0:
        raise ValueError(
            f"pov must be > 0, got {rate}; a zero or negative rate would either "
            "stall the parent forever or size a child in the wrong direction")
    if rate > 1.0:
        rate = rate / 100.0
    if rate <= 0:
        raise ValueError(f"pov is too small to size any child: {pov!r}")
    return rate


def pov_child_qty(remaining: int, bar_volume: float, pov: float = 0.1) -> int:
    """Child order = pov share of the last bar's volume, never more than what is left.

    Returns 0 when there is nothing to do (no remainder, or no volume this bar).
    Raises ValueError for an unusable ``pov``, because a bad RATE is a bug in the
    caller, whereas a quiet bar is simply no progress.
    """
    rate = _normalise_pov(pov)
    if remaining <= 0 or bar_volume is None or bar_volume <= 0:
        return 0
    if not math.isfinite(float(bar_volume)):
        return 0
    return max(0, min(int(remaining), int(float(bar_volume) * rate)))


def pov_child_qty_report(remaining: int, bar_volume: float, pov: float = 0.1
                         ) -> Tuple[int, str]:
    """(quantity, note). Same maths, but names a STALL instead of hiding it.

    A child of 0 because the bar was thin is a stall: the parent does not progress
    this cycle and the caller may want to alert on it. Returning the reason lets
    that decision be made, rather than a silent zero.
    """
    rate = _normalise_pov(pov)
    if remaining <= 0:
        return 0, "parent complete"
    if bar_volume is None or bar_volume <= 0:
        return 0, "stall: no volume this bar"
    if not math.isfinite(float(bar_volume)):
        return 0, "stall: unusable bar volume"
    qty = max(0, min(int(remaining), int(float(bar_volume) * rate)))
    if qty == 0:
        return 0, (f"stall: {rate:.2%} of {float(bar_volume):.0f} shares rounds to 0 "
                   "- raise POV or wait for a fuller bar")
    return qty, ""


def vwap_schedule(total_qty: int, profile: Sequence[float]) -> List[int]:
    """Split total_qty by an expected intraday volume profile. Rounding goes to the last bucket."""
    if total_qty < 0:
        raise ValueError(f"total_qty must be >= 0, got {total_qty}")
    if not profile:
        raise ValueError("profile is empty; a VWAP schedule needs at least one bucket")
    weights = [float(p) for p in profile]
    if any(not math.isfinite(w) for w in weights):
        raise ValueError("profile contains a non-finite weight")
    if any(w < 0 for w in weights):
        raise ValueError("profile weights must be >= 0")
    total = float(sum(weights))
    if total <= 0:
        raise ValueError("profile sums to zero; cannot distribute the quantity")
    out = [int(total_qty * w / total) for w in weights]
    out[-1] += total_qty - sum(out)
    return out
