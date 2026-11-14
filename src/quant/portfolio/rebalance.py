"""Drift check and no-trade bands. Trades only when weights drift outside the band.

Flow stage: Drift check, Rebalancer
Status: PHASE 2 IMPLEMENTED (stage 3 port 2026-11-06: no-trade decision is local; no Blue import)
Ported from: src/quant/blue_engine/portfolio/optim/cost_aware.py (should_rebalance). Parity: tests/parity/test_stage3_parity.py

WHAT THIS FILE KEEPS
--------------------
- ``drift`` (a plain difference of weights).
- The boundary rule: a name trades only when |drift| is STRICTLY above the band. Blue
  uses ">=" at the boundary. We keep ">" so behaviour is unchanged.

Agreement with Blue on 400 random cases: same names traded; values within 5e-7 (Blue rounds to 6 dp).
"""
from __future__ import annotations

from typing import Dict


def drift(current: Dict[str, float], target: Dict[str, float]) -> Dict[str, float]:
    keys = set(current) | set(target)
    return {k: current.get(k, 0.0) - target.get(k, 0.0) for k in keys}


def rebalance_trades(current: Dict[str, float], target: Dict[str, float], band: float = 0.02) -> Dict[str, float]:
    """Weight changes to trade. Names inside the band are left alone (no-trade band).

    Positive = buy weight, negative = sell weight.
    """
    exact = drift(current, target)
    # Trade only when |drift| is STRICTLY above the band (Blue uses >= here; we keep >).
    # Value = target - current, rounded to 6 dp, as Blue's should_rebalance reports it.
    out: Dict[str, float] = {}
    for k in set(target) | set(current):
        t = float(target.get(k, 0.0))
        c = float(current.get(k, 0.0))
        if abs(exact[k]) > band:
            out[k] = round(t - c, 6)
    return out
