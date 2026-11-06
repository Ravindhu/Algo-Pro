"""Pre-trade check for orders in a PCAS session: timing, limit-only, self-cross (decision 0025).

Flow stage: Risk gate
Status: PHASE 3 IMPLEMENTED (pure check; not yet wired into the gate or the POV loop)

Owner decision (2026-11-06): PCAS orders are limit orders only. Market orders are refused.
Every check fails closed: any failed check refuses the order, and all reasons are returned.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional, Tuple

from quant.core.pcas_schedule import orders_allowed, pcas_state
from quant.risk.self_cross import would_self_cross


def pcas_order_check(
    now: datetime,
    side: str,
    price_type: str,
    price: Optional[float],
    resting: Iterable[Tuple[str, Optional[float]]] = (),
) -> Tuple[bool, Tuple[str, ...]]:
    """Return (allowed, reasons) for one new PCAS order. Reasons are empty when allowed."""
    reasons = []
    if not orders_allowed(now):
        reasons.append(f"PCAS timing: {pcas_state(now)['reason']}")
    if price_type != "LIMIT":
        reasons.append(f"PCAS is limit-only; got {price_type!r}")
    if price_type == "LIMIT":
        crossed, why = would_self_cross(side, price, resting)
        if crossed:
            reasons.append(why)
    return (not reasons), tuple(reasons)
