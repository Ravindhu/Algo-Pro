"""Bar helpers for the BUY strategies. Ported from Algo-Lab (framework/strategies/ocl.py, multi.py).

Flow stage: Strategy (bar helpers; no broker, no I/O)
Status: PHASE 2 IMPLEMENTED (paper only, ported 2026-11-08; not live).

Frames are pandas DataFrames indexed by time with columns open, high, low, close (and volume).
Oldest bar first. The LAST row is the bar still forming.
"""
from __future__ import annotations

from typing import NamedTuple

import pandas as pd


class Bar(NamedTuple):
    open: float
    high: float
    low: float
    close: float

    @property
    def red(self) -> bool:
        return self.close < self.open

    @property
    def green(self) -> bool:
        return self.close > self.open


def closed(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the still-forming last row."""
    return df.iloc[:-1]


def last_bars(df: pd.DataFrame, n: int) -> list[Bar]:
    """The last n rows as Bar tuples, oldest first."""
    rows = df[["open", "high", "low", "close"]].iloc[-n:].to_numpy(dtype=float)
    return [Bar(*map(float, r)) for r in rows]
