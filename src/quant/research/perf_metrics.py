"""Sharpe ratio and max drawdown, local copies of Blue_engine ml/evaluation/metrics.py.

Same formulas and edge cases as Blue (see docs/decisions/0011-decouple-from-blue-engine.md).
Parity with Blue is tested in tests/unit/test_perf_metrics_parity.py.

Flow stage: Trial logger + DSR/PBO, Live monitor
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): none. Replaces src/quant/blue_engine/ml/evaluation/metrics.py (stage 1 of docs/DECOUPLING_PLAN.md).
"""
from __future__ import annotations

import math
from typing import Optional, Sequence


def sharpe(returns: Sequence[Optional[float]], *, periods_per_year: float = 252.0,
           risk_free: float = 0.0) -> Optional[float]:
    """Annualised Sharpe from per-period simple returns. None when undefined (fewer than 2 values, or zero spread)."""
    clean = [float(r) for r in returns if r is not None]
    n = len(clean)
    if n < 2:
        return None
    excess = [r - risk_free for r in clean]
    mu = sum(excess) / n
    var = sum((r - mu) ** 2 for r in excess) / (n - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return (mu / sd) * math.sqrt(periods_per_year)


def max_drawdown(equity: Sequence[Optional[float]]) -> Optional[float]:
    """Largest peak-to-trough decline as a positive fraction (0.25 = -25%). None when fewer than 2 values."""
    clean = [float(e) for e in equity if e is not None]
    if len(clean) < 2:
        return None
    peak = clean[0]
    worst = 0.0
    for e in clean:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (peak - e) / peak
            if dd > worst:
                worst = dd
    return worst
