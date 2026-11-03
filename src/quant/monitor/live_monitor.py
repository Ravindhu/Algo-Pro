"""Live monitor: drawdown and per-trade MAE/MFE (adverse and favourable excursion).

Flow stage: Live monitor
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/live_monitor.py
"""
from __future__ import annotations

from typing import Sequence, Tuple

import numpy as np


def max_drawdown(equity: Sequence[float]) -> float:
    e = np.asarray(equity, float)
    peak = np.maximum.accumulate(e)
    return float(np.max((peak - e) / peak)) if len(e) else 0.0


def excursions(entry: float, prices: Sequence[float], side: int) -> Tuple[float, float]:
    """(MAE, MFE) as positive fractions of entry price, measured in the trade's direction."""
    p = np.asarray(prices, float)
    moves = side * (p / entry - 1)
    return float(max(0.0, -moves.min())), float(max(0.0, moves.max()))
