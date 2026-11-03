"""Pre-trade sanity checks: quantity, lot size, price, per-order caps.

Flow stage: Pre-trade checks
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/risk_gate.py
"""
from __future__ import annotations

from typing import List

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent


def pretrade_checks(intent: TradeIntent, limits: RiskLimits) -> List[str]:
    reasons: List[str] = []
    if intent.qty <= 0:
        reasons.append("qty must be > 0")
    if intent.lot_size <= 0 or intent.qty % intent.lot_size:
        reasons.append(f"qty {intent.qty} not a multiple of lot {intent.lot_size}")
    if intent.ref_price <= 0:
        reasons.append("ref_price must be > 0")
    if intent.kind != "entry":
        return reasons  # exits and protection orders reduce risk; per-order caps do not apply
    if limits.max_order_quantity is None:
        reasons.append("limit not set: max_order_quantity")
    elif intent.qty > limits.max_order_quantity:
        reasons.append(f"qty {intent.qty} above max_order_quantity {limits.max_order_quantity}")
    if limits.max_order_notional is None:
        reasons.append("limit not set: max_order_notional")
    elif intent.notional > limits.max_order_notional:
        reasons.append(f"notional above max_order_notional {limits.max_order_notional}")
    return reasons
