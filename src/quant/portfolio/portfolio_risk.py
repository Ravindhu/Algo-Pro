"""Portfolio caps (sector, cluster, beta), stress scenarios and historical CVaR.

Flow stage: Portfolio risk, Stress and CVaR, Beta limits, Live intraday VaR/CVaR
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/risk_engine.py
"""
from __future__ import annotations

from typing import Dict, List

import numpy as np


def group_exposure(weights: Dict[str, float], group: Dict[str, str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for sym, w in weights.items():
        g = group.get(sym, "UNKNOWN")
        out[g] = out.get(g, 0.0) + abs(w)
    return out


def cap_violations(weights: Dict[str, float], group: Dict[str, str], cap: float) -> List[str]:
    return [f"{g} exposure {e:.2%} above cap {cap:.2%}" for g, e in group_exposure(weights, group).items() if e > cap]


def beta_exposure(weights: Dict[str, float], betas: Dict[str, float]) -> float:
    return float(sum(w * betas.get(s, 1.0) for s, w in weights.items()))


def stress_pnl(weights: Dict[str, float], shocks: Dict[str, float]) -> float:
    """shocks: symbol -> return in the scenario (e.g. -0.30). Missing symbols get 0."""
    return float(sum(w * shocks.get(s, 0.0) for s, w in weights.items()))


def historical_cvar(pnl, alpha: float = 0.05) -> float:
    """Average loss in the worst alpha share of outcomes. Returned as a positive number."""
    x = np.sort(np.asarray(pnl, float))
    k = max(1, int(np.ceil(alpha * len(x))))
    return float(-x[:k].mean())
