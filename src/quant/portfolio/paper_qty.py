"""Max entry quantity from portfolio construction, for the PAPER scanner.

Flow stage: Sizing and portfolio (quantity input to the order intent)
Status: PHASE 2 IMPLEMENTED (paper only, 2026-11-08; not live).

Rule (owner, 2026-11-08: "as per portfolio construction the max qty should calculate and give to input"):
    base      = shares so a stop-out loses at most risk_per_trade of paper capital (risk_budget_qty)
    caps      = max_order_quantity, max_order_notional / entry, remaining open exposure / entry
    qty       = floor(min(base, every cap)), via decide_size (conviction tiers apply only if enabled)
Any missing limit fails closed: qty = 0, and the reason is returned.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from quant.core.config import RiskLimits
from quant.portfolio.sizing import risk_budget_qty
from quant.portfolio.tiers import TierTable, decide_size


@dataclass(frozen=True)
class QtyDecision:
    qty: int
    base_qty: int
    caps: dict
    binding: str
    reasons: tuple

    def as_audit_record(self) -> dict:
        return {"qty": self.qty, "base_qty": self.base_qty, "caps": dict(self.caps),
                "binding": self.binding, "reasons": list(self.reasons)}


def max_entry_qty(
    *,
    entry: float,
    stop: float,
    capital: float,
    risk_fraction: float,
    limits: RiskLimits,
    open_notional: float,
    tiers: TierTable,
    conviction: Optional[float] = None,
) -> QtyDecision:
    """Quantity for one BUY entry. Never raises for bad market inputs: returns qty 0 with a reason."""
    reasons = []
    if not (math.isfinite(entry) and entry > 0):
        return QtyDecision(0, 0, {}, "invalid_entry", ("entry price not positive",))
    if not (math.isfinite(stop) and stop < entry):
        return QtyDecision(0, 0, {}, "invalid_stop", ("stop must be below entry for a BUY",))
    if not (math.isfinite(capital) and capital > 0):
        return QtyDecision(0, 0, {}, "no_capital", ("paper capital missing or not positive",))
    if not (0 < risk_fraction <= 0.05):
        return QtyDecision(0, 0, {}, "bad_risk", ("risk_fraction must be in (0, 0.05]",))

    if limits.max_order_quantity is None:
        reasons.append("limit not set: max_order_quantity")
    if limits.max_order_notional is None:
        reasons.append("limit not set: max_order_notional")
    if limits.max_open_exposure is None:
        reasons.append("limit not set: max_open_exposure")
    if reasons:
        return QtyDecision(0, 0, {}, "limit_missing", tuple(reasons))

    base = risk_budget_qty(capital, risk_fraction, entry, stop, lot_size=1)
    caps = {
        "max_order_quantity": float(limits.max_order_quantity),
        "max_order_notional": math.floor(limits.max_order_notional / entry),
        "remaining_exposure": math.floor(max(0.0, limits.max_open_exposure - open_notional) / entry),
    }
    if tiers.enabled and conviction is None:
        raise ValueError("conviction score is required when conviction tiers are enabled")
    score = 1.0 if conviction is None else conviction   # tiers disabled: score does not change size
    d = decide_size(float(base), score, caps, tiers)
    qty = int(math.floor(d.size))
    if qty < 1:
        reasons.append("qty rounds to zero under current caps")
    return QtyDecision(qty if qty >= 1 else 0, base, caps, d.binding, tuple(reasons))
