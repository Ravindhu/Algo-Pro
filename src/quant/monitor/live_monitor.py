"""Live monitor: drawdown and per-trade MAE/MFE (adverse and favourable excursion).

Flow stage: Live monitor
Status: PHASE 3 IMPLEMENTED (2026-11-14 excursion validation)
Existing code to reuse or wrap (do not copy): none for max_drawdown (local copy in
    research/perf_metrics.py since 2026-11-05)

DEFECTS FIXED (2026-11-14)
--------------------------
MAE/MFE feed the post-trade review: "how much heat did this trade take before it
worked?" A wrong number here silently rewrites the assessment of a strategy.

1. ``entry = 0`` divided by zero and returned ``inf`` for MFE. An infinite
   excursion then dominates any average it enters. Now refused.
2. Empty ``prices`` raised a bare ``ValueError`` from ``min()`` with no context.
   Now the caller is told the series was empty.
3. ``side = 0`` returned (0.0, 0.0), which reads as "no excursion" - a flat trade.
   Side 0 is meaningless: an excursion needs a direction. Now refused.

SIGN CONVENTION (unchanged)
---------------------------
Both values are POSITIVE fractions of the entry price, measured in the trade's
direction. MAE is the worst adverse move, MFE the best favourable one, so a long
with prices 90 then 110 from 100 gives MAE 0.10 and MFE 0.10.
"""
from __future__ import annotations

import math
from typing import Sequence, Tuple

import numpy as np

__all__ = ["max_drawdown", "excursions"]


def max_drawdown(equity: Sequence[float]) -> float:
    """Largest peak-to-trough fall as a positive fraction. 0.0 when undefined.

    Local copy of research/perf_metrics.max_drawdown. Blue skips non-positive
    peaks; the old local version divided by zero there and returned NaN.
    """
    from quant.research.perf_metrics import max_drawdown as _max_dd

    value = _max_dd([float(x) for x in equity])
    return 0.0 if value is None else float(value)


def excursions(entry: float, prices: Sequence[float], side: int) -> Tuple[float, float]:
    """(MAE, MFE) as positive fractions of entry price, measured in the trade's direction.

    Raises ValueError for an unusable entry price, an empty series, or a side that
    is not exactly +1 (long) or -1 (short).
    """
    try:
        e = float(entry)
    except (TypeError, ValueError):
        raise ValueError(f"entry must be a number, got {entry!r}")
    if not math.isfinite(e) or e <= 0:
        raise ValueError(f"entry must be a finite positive price, got {entry!r}")

    if side not in (1, -1):
        raise ValueError(f"side must be +1 (long) or -1 (short), got {side!r}")

    # ``prices or []`` would raise on a numpy array's truth-value test; callers
    # legitimately pass arrays, so None is handled explicitly.
    source = [] if prices is None else prices
    values = [float(p) for p in source if p is not None]
    if not values:
        raise ValueError("prices is empty; an excursion needs at least one observation")

    p = np.asarray(values, float)
    if not np.isfinite(p).all():
        p = p[np.isfinite(p)]
        if p.size == 0:
            raise ValueError("prices contains no finite values")

    moves = side * (p / e - 1.0)
    return float(max(0.0, -moves.min())), float(max(0.0, moves.max()))
