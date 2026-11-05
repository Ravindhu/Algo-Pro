"""Fractional Kelly sizing with vol targeting and hard caps. Thin wrapper over Blue_engine.

Flow stage: Fractional Kelly
Status: PHASE 2 IMPLEMENTED (Phase 0 wrap: wrapper: formulas delegated to Blue_engine; guards kept here)
Existing code to reuse or wrap (do not copy): Blue_engine/portfolio/sizing/kelly.py (kelly_leverage),
Blue_engine/portfolio/sizing/vol_target.py (vol_target_scale)

Equivalence checked before wrapping: 180 Kelly cases and 24 vol-target cases match the
pre-wrap formulas exactly (see docs/audits/PHASE0_REUSE_AUDIT.md, step 7).
"""
from __future__ import annotations

from quant.core.blue_bridge import load


def fractional_kelly(mu: float, sigma: float, fraction: float = 0.25, cap: float = 0.10) -> float:
    """Fraction of capital: fraction * full Kelly (mu / sigma^2), floored at 0 and capped at `cap`."""
    if sigma <= 0 or fraction <= 0 or cap <= 0:
        return 0.0
    full = load("portfolio.sizing.kelly").kelly_leverage(mu, 0.0, sigma)
    if full is None:
        return 0.0
    return float(min(cap, max(0.0, fraction * full)))


def vol_target_scale(realized_vol: float, target_vol: float, max_leverage: float = 1.0) -> float:
    """Scale that moves realized vol toward target, bounded to [0, max_leverage]."""
    if realized_vol <= 0:
        return 0.0
    result = load("portfolio.sizing.vol_target").vol_target_scale(
        realized_vol, target_vol, min_scale=0.0, max_scale=max_leverage)
    return float(result["scale"])
