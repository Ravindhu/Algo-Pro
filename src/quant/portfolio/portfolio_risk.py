"""Portfolio caps (sector, cluster, beta), stress scenarios and historical CVaR.

Flow stage: Portfolio risk, Stress and CVaR, Beta limits, Live intraday VaR/CVaR
Status: PHASE 2 IMPLEMENTED (stage 3 port 2026-11-06: historical CVaR is local; no Blue import)
Ported from: src/quant/blue_engine/portfolio/risk/tail.py (cvar, _quantile). Parity: tests/parity/test_stage3_parity.py
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

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


def _quantile(sorted_xs: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolated empirical quantile of a sorted sequence (same rule as Blue)."""
    n = len(sorted_xs)
    if n == 0:
        return None
    if n == 1:
        return sorted_xs[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_xs[lo]
    frac = pos - lo
    return sorted_xs[lo] * (1 - frac) + sorted_xs[hi] * frac


def historical_cvar(pnl, alpha: float = 0.05) -> float:
    """Average loss in the worst alpha share of outcomes, as a positive number.

    Non-finite values are dropped (same as Blue). Needs at least 2 finite outcomes.
    """
    rs = sorted(float(x) for x in pnl if x is not None and math.isfinite(float(x)))
    if len(rs) < 2:
        raise ValueError("CVaR undefined for this sample")
    cut = _quantile(rs, alpha)
    if cut is None:
        raise ValueError("CVaR undefined for this sample")
    tail = [r for r in rs if r <= cut]
    if not tail:
        return float(max(-cut, 0.0))
    m = sum(tail) / len(tail)
    return float(max(-m, 0.0))
