"""Max capital a strategy can deploy before it moves the market, from ADV participation.

Flow stage: Capacity estimator
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/backtest/metrics.py
"""
from __future__ import annotations


def max_capital(adv_notional: float, max_adv_pct: float, holding_days: float) -> float:
    """Capital that can be traded in `holding_days` while staying under `max_adv_pct` of daily turnover."""
    if min(adv_notional, max_adv_pct, holding_days) < 0:
        raise ValueError("inputs must be >= 0")
    return adv_notional * max_adv_pct * holding_days
