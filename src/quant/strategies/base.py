"""Strategy interface: turn price history into long-only target weights.

Flow stage: Strategy
Status: PHASE 5 IMPLEMENTED

Rules
-----
- A strategy receives only the rows up to and including its decision date. It never sees
  the future, so the backtest cannot leak it (no look-ahead).
- It returns weights, not quantities. Sizing into whole shares lives in
  ``research/strategy_backtest.py``.
- A strategy never changes risk limits or position size beyond its own weights (AGENTS.md).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict

import numpy as np
import pandas as pd


class Strategy(ABC):
    """Base class. Subclasses set ``name`` and implement ``target_weights``."""

    name: str = "strategy"

    @abstractmethod
    def get_params(self) -> Dict[str, object]:
        """Parameters as a plain dict, written to the trial log and audit."""

    @abstractmethod
    def target_weights(self, close_history: pd.DataFrame) -> pd.Series:
        """Weights for the decision date (the last row of ``close_history``).

        ``close_history``: rows = dates up to and including the decision date, columns = symbols.
        Return a Series indexed by symbol. Symbols not listed get weight 0.
        """


def check_weights(weights: pd.Series, tol: float = 1e-9) -> pd.Series:
    """Validate a weight vector. Raises ValueError on NaN, negative weight, or total above 1."""
    w = pd.Series(weights, dtype=float)
    if w.isna().any():
        raise ValueError("weights contain NaN")
    if (w < 0).any():
        raise ValueError("long-only strategy returned a negative weight")
    total = float(w.sum())
    if total > 1.0 + tol:
        raise ValueError(f"weights sum to {total:.6f}, above 1")
    if not np.isfinite(total):
        raise ValueError("weights are not finite")
    return w
