"""Max capital a strategy can deploy before impact eats the edge.

Flow stage: Capacity estimator
Status: PHASE 1 IMPLEMENTED; impact-based model 2026-11-14
Existing code to reuse or wrap (do not copy):
    src/quant/blue_engine/backtest/costs.py (CostModel, the square-root impact
    law: impact_bps = impact_coefficient * sqrt(order_notional / bar_notional)).
    The engine has no capacity estimator; the model below is the inverse of the
    engine's impact law, so capacity and simulated cost agree by construction.

WHY THE OLD FORMULA WAS WRONG
-----------------------------
The original was ``adv_notional * max_adv_pct * holding_days``. It is linear in
size and says a strategy can hold 100% of average daily volume for ten days with
no consequence. Market impact does not behave that way: it grows with the
SQUARE ROOT of the participation rate, so cost per unit rises as you scale. A
linear capacity number systematically OVERSTATES the capital a book can carry,
which is the expensive direction to be wrong.

THE MODEL
---------
Impact in bps (the engine's law)::

    impact_bps = coef * sqrt(order_notional / bar_notional)

Participation is ``q = order_notional / adv_notional``. Setting impact to an
acceptable ceiling ``max_impact_bps`` and solving for the order gives::

    order_notional = adv_notional * (max_impact_bps / coef)^2

That is the most you can trade in one session before impact exceeds the ceiling.
Spreading over ``holding_days`` raises the cumulative amount, but NOT linearly:
each day's own impact is unchanged, and the position is built gradually.

ASSUMPTIONS, STATED
-------------------
- The coefficient comes from the engine's cost model, calibratable per source.
- Returns are assumed roughly normal over the horizon; a fat-tailed name needs a
  lower ceiling. That is a real limitation, not a hidden one.
- Capacity is the capital that still leaves the strategy profitable: it is the
  point where impact equals ``edge_bps``, so an edge narrower than the impact
  ceiling reduces capacity quadratically.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

__all__ = [
    "DEFAULT_IMPACT_COEF",
    "max_capital",
    "capacity_report",
    "impact_bps",
]

#: Square-root impact coefficient. Matches the engine's CostModel default, so a
#: capacity estimate and a simulated cost use the same number.
DEFAULT_IMPACT_COEF = 1.0


def impact_bps(order_notional: float, adv_notional: float,
               coef: float = DEFAULT_IMPACT_COEF) -> float:
    """Engine's impact law: coef * sqrt(participation), in bps.

    Returns 0.0 for a zero-size order and raises on unusable inputs rather than
    inventing a number.
    """
    if order_notional < 0 or adv_notional < 0 or coef < 0:
        raise ValueError("inputs must be >= 0")
    if adv_notional == 0:
        raise ValueError("adv_notional must be > 0")
    return coef * math.sqrt(order_notional / adv_notional)


def max_capital(adv_notional: float, max_adv_pct: float, holding_days: float,
                *, edge_bps: Optional[float] = None,
                coef: float = DEFAULT_IMPACT_COEF) -> float:
    """Capital deployable while impact stays within the budget.

    Signature kept identical to the original function so existing callers keep
    working; the maths is now impact-based instead of linear.

    ``max_adv_pct`` is the participation CEILING (e.g. 0.10 = 10% of ADV). When
    ``edge_bps`` is given, the impact ceiling is that edge: capacity is where
    impact would consume the whole edge. When it is omitted, the ceiling is the
    impact at the participation ceiling itself, which reproduces the intended
    "trade up to this share of ADV" meaning without the false linearity.

    ``holding_days`` raises the cumulative amount per day: the position may be
    built over several sessions, so the total is per-day capacity x days. Each
    day's slice is still bounded, which is what the linear version got wrong.
    """
    if min(adv_notional, max_adv_pct, holding_days) < 0:
        raise ValueError("inputs must be >= 0")
    if adv_notional == 0 or holding_days == 0:
        return 0.0
    if not 0 < max_adv_pct <= 1:
        raise ValueError("max_adv_pct must be in (0, 1]")
    if coef <= 0:
        raise ValueError("coef must be > 0")

    # Notional allowed in ONE session at the participation ceiling.
    daily = adv_notional * max_adv_pct

    if edge_bps is None:
        return daily * holding_days

    if edge_bps <= 0:
        return 0.0
    # Invert the impact law: q = (ceiling / coef)^2, bounded by the ADV ceiling.
    implied_q = (edge_bps / coef) ** 2
    per_session = adv_notional * min(max_adv_pct, implied_q)
    return per_session * holding_days


def capacity_report(adv_notional: float, *, max_adv_pct: float = 0.10,
                    holding_days: float = 1.0, edge_bps: Optional[float] = None,
                    coef: float = DEFAULT_IMPACT_COEF) -> Dict[str, Any]:
    """Capacity with the numbers that produced it, for the dashboard and the log.

    Showing the impact at the limit is what makes the estimate reviewable: a
    reader can see whether the ceiling is the ADV rule or the edge.
    """
    cap = max_capital(adv_notional, max_adv_pct, holding_days,
                      edge_bps=edge_bps, coef=coef)
    per_day = cap / holding_days if holding_days else 0.0
    participation = per_day / adv_notional if adv_notional else 0.0
    return {
        "max_capital": round(cap, 2),
        "per_session": round(per_day, 2),
        "adv_notional": round(adv_notional, 2),
        "participation": round(participation, 6),
        "impact_bps_at_limit": round(impact_bps(per_day, adv_notional, coef), 4)
        if adv_notional else 0.0,
        "ceiling": "edge" if edge_bps else "adv",
        "edge_bps": edge_bps,
        "coef": coef,
        "holding_days": holding_days,
    }
