"""Reference strategy: top-N positive momentum, equal weight.

Flow stage: Strategy
Status: PHASE 5 IMPLEMENTED

Rule: on each decision date, rank symbols by total return over ``lookback`` bars that ends
``shift`` bars ago (uses ``models/factors.momentum``, which only reads the past). Take the top
``top_n`` with positive momentum. Each gets weight 1/top_n, so unfilled slots stay in cash.
"""
from __future__ import annotations

from typing import Dict

import pandas as pd

from quant.models.factors import momentum
from quant.strategies.base import Strategy


class MomentumStrategy(Strategy):
    name = "momentum_topn"

    def __init__(self, lookback: int = 126, top_n: int = 5, shift: int = 1) -> None:
        if int(lookback) < 2:
            raise ValueError("lookback must be at least 2 bars")
        if int(top_n) < 1:
            raise ValueError("top_n must be at least 1")
        if int(shift) < 0:
            raise ValueError("shift must be >= 0 (negative would read the future)")
        self.lookback = int(lookback)
        self.top_n = int(top_n)
        self.shift = int(shift)

    def get_params(self) -> Dict[str, object]:
        return {"lookback": self.lookback, "top_n": self.top_n, "shift": self.shift}

    def target_weights(self, close_history: pd.DataFrame) -> pd.Series:
        if close_history.empty:
            return pd.Series(dtype=float)
        score = momentum(close_history, window=self.lookback, shift=self.shift).iloc[-1]
        score = score.dropna()
        score = score[score > 0].sort_values(ascending=False).head(self.top_n)
        weights = pd.Series(0.0, index=close_history.columns, dtype=float)
        if score.empty:
            return weights
        weights.loc[score.index] = 1.0 / self.top_n
        return weights
