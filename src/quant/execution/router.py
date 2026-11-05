"""Algo router: POV child sizing and VWAP schedule. Only gate-approved parents are routed.

Flow stage: Algo router
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): none. The Blue POV state machine was dashboard state and was removed 2026-11-05.
"""
from __future__ import annotations

from typing import List, Sequence


def pov_child_qty(remaining: int, bar_volume: float, pov: float = 0.1) -> int:
    """Child order = pov share of the last bar's volume, never more than what is left."""
    if remaining <= 0 or bar_volume <= 0:
        return 0
    return min(remaining, int(bar_volume * pov))


def vwap_schedule(total_qty: int, profile: Sequence[float]) -> List[int]:
    """Split total_qty by an expected intraday volume profile. Rounding goes to the last bucket."""
    s = float(sum(profile))
    if total_qty < 0 or s <= 0:
        raise ValueError("bad quantity or profile")
    out = [int(total_qty * p / s) for p in profile]
    out[-1] += total_qty - sum(out)
    return out
