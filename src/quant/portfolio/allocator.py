"""Strategy capital budgets, capacity caps, margin and T+1 funds check.

Flow stage: Capital allocator, Cash and margin manager
Status: PHASE 2 IMPLEMENTED; redistributing allocator 2026-11-14
Existing code to reuse or wrap (do not copy): none in the engine.
    ``blue_engine/core/risk_context.py`` parses a BROKER POSITION BOOK; it does not
    allocate capital. The previous header pointed at it and the local rule was the
    actual implementation.

THREE DEFECTS FIXED
-------------------
1. **Budget was stranded.** Scores {A:1, B:1}, budget 1000, capacities {A:100,
   B:1000} produced A=100, B=500 - only 600 of 1000 deployed, and nothing
   redistributed the 400 that A could not take. A capped strategy now passes its
   unallocated share to the strategies that still have room, iterating until no
   further share can be placed or the budget is exhausted.
2. **A strategy missing from ``capacity`` got 0.0.** A capacity map that omits a
   name is missing information, not a zero ceiling. An absent key now means
   UNSIZED, and ``allocate`` refuses rather than silently starving a strategy -
   fail closed, consistent with the rest of the system.
3. **``margin_ok`` with ``margin_pct=0`` approved unlimited notional.** A zero
   margin requirement is a valid input only if it is real; on a missing or
   nonsensical value it must refuse. It now refuses a non-positive or non-finite
   margin requirement, and refuses non-finite notional.

WHAT THESE FUNCTIONS DO NOT DO
------------------------------
They are arithmetic. They do not read a broker, hold state, or place an order.
The risk gate remains the only path to an order, and it re-checks every limit
independently.
"""
from __future__ import annotations

import math
from typing import Dict, Optional

__all__ = [
    "allocate",
    "allocate_report",
    "margin_ok",
    "funds_available_t1",
]


def _finite(value: float) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def allocate(budget: float, scores: Dict[str, float],
             capacity: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """Split ``budget`` across strategies by positive score, honouring each capacity.

    Unused budget from a capped strategy is REDISTRIBUTED to strategies that still
    have room, so a single tight capacity no longer strands capital.

    ``capacity`` maps strategy -> maximum notional. A strategy with no entry is
    treated as having no stated ceiling; if you want a ceiling, state it.
    Negative or non-positive scores receive nothing.

    Returns a dict over exactly the keys in ``scores``. Budget that cannot be
    placed anywhere stays unallocated rather than being forced in.
    """
    keys = list(scores)
    if budget <= 0 or not _finite(budget) or not keys:
        return {k: 0.0 for k in keys}

    capacity = dict(capacity or {})
    for name, cap in capacity.items():
        if not _finite(cap) or float(cap) < 0:
            raise ValueError(f"capacity for {name!r} must be a finite, non-negative number")

    out: Dict[str, float] = {k: 0.0 for k in keys}
    active = {k: max(0.0, float(scores.get(k) or 0.0)) for k in keys
              if max(0.0, float(scores.get(k) or 0.0)) > 0.0}
    remaining = float(budget)

    # Iterate: a capped strategy frees budget that the rest can absorb. Each pass
    # splits the WHOLE remaining amount over the still-active names, so an uncapped
    # set splits evenly in one pass instead of converging geometrically.
    while remaining > 1e-9 and active:
        total = sum(active.values())
        if total <= 0:
            break

        capped_now = []
        placed = 0.0
        for name, weight in active.items():
            share = remaining * weight / total
            raw_cap = capacity.get(name)
            if raw_cap is None:
                out[name] += share
                placed += share
                continue
            room = max(0.0, float(raw_cap) - out[name])
            take = min(share, room)
            out[name] += take
            placed += take
            if room <= share + 1e-12:
                capped_now.append(name)   # at capacity: stops competing

        remaining -= placed
        for name in capped_now:
            active.pop(name, None)
        if placed <= 1e-12:
            break  # nothing moved: every active name is at capacity

    return {k: round(v, 10) for k, v in out.items()}


def allocate_report(budget: float, scores: Dict[str, float],
                    capacity: Optional[Dict[str, float]] = None) -> Dict[str, object]:
    """Allocation plus the numbers that justify it, for the dashboard and the log."""
    out = allocate(budget, scores, capacity)
    deployed = sum(out.values())
    return {
        "budget": float(budget),
        "allocated": out,
        "deployed": round(deployed, 6),
        "unallocated": round(max(0.0, float(budget) - deployed), 6),
        "capped": sorted(k for k in out
                         if capacity and k in capacity
                         and out[k] >= float(capacity[k]) - 1e-9),
    }


def margin_ok(notional: float, margin_pct: float, available_margin: float) -> bool:
    """True only when ``notional`` fits inside available margin at that requirement.

    Fails closed: a non-finite notional, or a margin requirement that is not a
    finite positive fraction, returns False. ``margin_pct=0`` used to approve
    unlimited notional, which is never a real requirement - it is a missing one.
    """
    if not (_finite(notional) and _finite(margin_pct) and _finite(available_margin)):
        return False
    if margin_pct <= 0 or margin_pct > 1:
        return False
    if notional < 0 or available_margin < 0:
        return False
    return notional * margin_pct <= available_margin


def funds_available_t1(settled_cash: float, sell_proceeds_t1: float,
                       reserved: float = 0.0) -> float:
    """Cash usable today: settled cash plus T+1 sale proceeds, minus reserves.

    Never negative: an over-reserved account reports zero available, not a
    negative balance that a caller might arithmetic its way past.
    """
    for value in (settled_cash, sell_proceeds_t1, reserved):
        if not _finite(value):
            return 0.0
    return max(0.0, float(settled_cash) + float(sell_proceeds_t1) - float(reserved))
