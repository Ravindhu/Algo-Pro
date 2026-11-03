"""Drift check and no-trade bands. Trades only when weights drift outside the band.

Flow stage: Drift check, Rebalancer
Status: PHASE 2 IMPLEMENTED (no-trade decision delegated to Blue_engine; boundary policy kept here)
Existing code to reuse or wrap (do not copy): Blue_engine/portfolio/optim/cost_aware.py (should_rebalance)

WHAT THIS FILE KEEPS
--------------------
- ``drift`` (a plain difference of weights).
- The boundary rule: a name trades only when |drift| is STRICTLY above the band. Blue
  uses ">=" at the boundary. We keep ">" so behaviour is unchanged.

Agreement with Blue on 400 random cases: same names traded; values within 5e-7 (Blue rounds to 6 dp).
"""
from __future__ import annotations

from typing import Dict

from quant.core.blue_bridge import load


def drift(current: Dict[str, float], target: Dict[str, float]) -> Dict[str, float]:
    keys = set(current) | set(target)
    return {k: current.get(k, 0.0) - target.get(k, 0.0) for k in keys}


def rebalance_trades(current: Dict[str, float], target: Dict[str, float], band: float = 0.02) -> Dict[str, float]:
    """Weight changes to trade. Names inside the band are left alone (no-trade band).

    Positive = buy weight, negative = sell weight.
    """
    exact = drift(current, target)
    names = list(exact)
    moves = load("portfolio.optim.cost_aware").should_rebalance(
        target, current, band={name: band for name in names})["moves"]
    # Keep the strict boundary: trade only when |drift| is above the band.
    return {k: v for k, v in moves.items() if abs(exact[k]) > band}
