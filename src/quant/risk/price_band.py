"""Price band (circuit limit) and ASM/GSM surveillance checks.

Flow stage: Price band + ASM/GSM
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/band.py
"""
from __future__ import annotations

from typing import List

from quant.core.types import TradeIntent


def price_band_checks(intent: TradeIntent) -> List[str]:
    reasons: List[str] = []
    if intent.kind == "entry" and intent.asm_gsm:
        reasons.append("ASM/GSM surveillance flag: no new entries")
    if intent.prev_close is not None and intent.circuit_pct is not None:
        lo = intent.prev_close * (1 - intent.circuit_pct)
        hi = intent.prev_close * (1 + intent.circuit_pct)
        if not lo <= intent.ref_price <= hi:
            reasons.append(f"price {intent.ref_price} outside circuit band [{lo:.2f}, {hi:.2f}]")
    return reasons
