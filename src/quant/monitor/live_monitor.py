"""Live monitor: drawdown and per-trade MAE/MFE (adverse and favourable excursion).

Flow stage: Live monitor
Status: PHASE 3 IMPLEMENTED (Phase 0 wrap: max_drawdown wrapped; excursions kept local)
Existing code to reuse or wrap (do not copy): Blue_engine/ml/evaluation/metrics.py (max_drawdown)
"""
from __future__ import annotations

from typing import Sequence, Tuple

import numpy as np


def max_drawdown(equity: Sequence[float]) -> float:
    """Largest peak-to-trough fall as a positive fraction. 0.0 when undefined. Delegates to Blue_engine.

    Blue skips non-positive peaks. The old local version divided by zero there and returned NaN.
    """
    from quant.core.blue_bridge import load

    value = load("ml.evaluation.metrics").max_drawdown([float(x) for x in equity])
    return 0.0 if value is None else float(value)


def excursions(entry: float, prices: Sequence[float], side: int) -> Tuple[float, float]:
    """(MAE, MFE) as positive fractions of entry price, measured in the trade's direction."""
    p = np.asarray(prices, float)
    moves = side * (p / entry - 1)
    return float(max(0.0, -moves.min())), float(max(0.0, moves.max()))
