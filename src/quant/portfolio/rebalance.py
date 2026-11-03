"""Drift check and no-trade bands. Trades only when weights drift outside the band.

Flow stage: Drift check, Rebalancer
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/jobs/daily_portfolio.py
"""
from __future__ import annotations

from typing import Dict


def drift(current: Dict[str, float], target: Dict[str, float]) -> Dict[str, float]:
    keys = set(current) | set(target)
    return {k: current.get(k, 0.0) - target.get(k, 0.0) for k in keys}


def rebalance_trades(current: Dict[str, float], target: Dict[str, float], band: float = 0.02) -> Dict[str, float]:
    """Weight changes to trade. Names inside the band are left alone (no-trade band)."""
    out = {}
    for k, d in drift(current, target).items():
        if abs(d) > band:
            out[k] = -d  # positive = buy weight, negative = sell weight
    return out
