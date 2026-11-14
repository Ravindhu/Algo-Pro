"""Liquidity screener (ADV known) and liquidity cap (max percent of ADV per order).

Flow stage: Liquidity screener, Liquidity cap
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/liquidity.py
"""
from __future__ import annotations

from typing import List

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent


def liquidity_checks(intent: TradeIntent, limits: RiskLimits) -> List[str]:
    if intent.kind != "entry":
        return []
    if intent.adv_shares is None:
        return ["ADV unknown: no new entries"]  # fail closed
    if limits.max_adv_pct is None:
        return ["limit not set: max_adv_pct"]
    cap = limits.max_adv_pct * intent.adv_shares
    if intent.qty > cap:
        return [f"qty {intent.qty} above {limits.max_adv_pct:.2%} of ADV ({cap:.0f})"]
    return []
