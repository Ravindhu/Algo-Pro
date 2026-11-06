"""Fractional Kelly sizing with vol targeting and hard caps. Thin wrapper over Blue_engine.

Flow stage: Fractional Kelly
Status: PHASE 2 IMPLEMENTED (stage 3 port 2026-11-06: formulas are local; no Blue import)
Ported from: Blue_engine/portfolio/sizing/kelly.py (kelly_leverage) and
Blue_engine/portfolio/sizing/vol_target.py (vol_target_scale). Parity: tests/parity/test_stage3_parity.py

Blue quirk kept on purpose (review with owner): a realized vol at or below 1e-6 (but > 0)
returns scale 1.0 in Blue. Here the same value comes out of the local rule below.
"""
from __future__ import annotations

import math

VOL_FLOOR = 1e-6  # same floor as Blue vol_target_scale


def _kelly_leverage(mu: float, sigma: float) -> float | None:
    """Blue kelly_leverage with rf = 0: mu / sigma^2. None when sigma <= 0 or inputs are not finite."""
    if not (math.isfinite(mu) and math.isfinite(sigma)) or sigma <= 0:
        return None
    return mu / (sigma * sigma)


def fractional_kelly(mu: float, sigma: float, fraction: float = 0.25, cap: float = 0.10) -> float:
    """Fraction of capital: fraction * full Kelly (mu / sigma^2), floored at 0 and capped at `cap`."""
    if sigma <= 0 or fraction <= 0 or cap <= 0:
        return 0.0
    full = _kelly_leverage(float(mu), float(sigma))
    if full is None:
        return 0.0
    return float(min(cap, max(0.0, fraction * full)))


def vol_target_scale(realized_vol: float, target_vol: float, max_leverage: float = 1.0) -> float:
    """Scale that moves realized vol toward target, bounded to [0, max_leverage]."""
    if realized_vol <= 0:
        return 0.0
    # Blue rule: no change (scale 1.0) when the vol is at/below the floor, or the target is missing.
    if realized_vol <= VOL_FLOOR or not math.isfinite(target_vol):
        return 1.0
    raw = target_vol / realized_vol
    scale = min(max(raw, 0.0), float(max_leverage))
    return float(round(scale, 6))


def risk_budget_qty(equity: float, risk_fraction: float, entry: float, stop: float, lot_size: int = 1) -> int:
    """Shares so that a stop-out loses at most risk_fraction of equity. Rounds down to whole lots.

    Returns 0 when any input is missing or not positive. Used by the signal stage (not built yet).
    """
    per_share = abs(entry - stop)
    if not (math.isfinite(equity) and math.isfinite(entry) and math.isfinite(stop)):
        return 0
    if equity <= 0 or risk_fraction <= 0 or per_share <= 0 or lot_size <= 0:
        return 0
    shares = math.floor(equity * risk_fraction / per_share)
    return (shares // lot_size) * lot_size
