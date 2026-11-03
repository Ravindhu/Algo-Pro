"""Fractional Kelly sizing with vol targeting and hard caps.

Flow stage: Fractional Kelly
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/risk_engine.py
"""
from __future__ import annotations


def fractional_kelly(mu: float, sigma: float, fraction: float = 0.25, cap: float = 0.10) -> float:
    """Fraction of capital. Full Kelly is mu/sigma^2. Only a fraction of it is used, and it is capped."""
    if sigma <= 0 or fraction <= 0 or cap <= 0:
        return 0.0
    return float(min(cap, max(0.0, fraction * mu / sigma**2)))


def vol_target_scale(realized_vol: float, target_vol: float, max_leverage: float = 1.0) -> float:
    if realized_vol <= 0:
        return 0.0
    return float(min(max_leverage, target_vol / realized_vol))
