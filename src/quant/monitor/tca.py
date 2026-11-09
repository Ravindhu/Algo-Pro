"""Transaction cost analysis: slippage against arrival price. Feeds the cost model.

Flow stage: TCA to cost model
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/backtest/costs.py
"""
from __future__ import annotations

from typing import Sequence

import numpy as np


def slippage_bps(side: str, fill: float, arrival: float) -> float:
    """Positive = worse than arrival (cost to us)."""
    raw = (fill - arrival) / arrival * 1e4
    return float(raw if side == "BUY" else -raw)


def mean_slippage_bps(rows: Sequence[dict]) -> float:
    vals = [slippage_bps(r["side"], r["fill"], r["arrival"]) for r in rows]
    return float(np.mean(vals)) if vals else 0.0
