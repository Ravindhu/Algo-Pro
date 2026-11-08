"""Engulfing pattern helpers. Ported from Algo-Lab (framework/strategies/engulfing_strategy.py, functions only).

Flow stage: Strategy setup (pattern math; no broker, no I/O)
Status: PHASE 2 IMPLEMENTED (paper only, ported 2026-11-08; not live). Only the functions are ported. The old standalone Engulfing class is not.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FIB_ENTRY = 0.886          # entry sits 88.6% of the way back into the engulfing candle's range


def fib_entry_level(high: float, low: float, direction: int, fib: float = FIB_ENTRY) -> float:
    """BUY entry level inside the engulfing candle: high - fib * range (a deep pullback from the high)."""
    rng = high - low
    return high - fib * rng if direction > 0 else low + fib * rng


def body_ratio(df: pd.DataFrame) -> pd.Series:
    """Candle body as a share of its full range. 0 where the range is 0."""
    rng = df["high"] - df["low"]
    body = (df["close"] - df["open"]).abs()
    return (body / rng.where(rng > 0)).fillna(0.0)


def engulfing(df: pd.DataFrame, min_body_pct: float = 0.5) -> pd.Series:
    """+1 bullish engulfing, -1 bearish engulfing, 0 none, per bar.

    Both candles of the pattern must have a body larger than min_body_pct of their range.
    Set min_body_pct to 0 to ignore body size."""
    o, c = df["open"], df["close"]
    po, pc = o.shift(), c.shift()
    bull = (pc < po) & (c > o) & (o <= pc) & (c >= po)
    bear = (pc > po) & (c < o) & (o >= pc) & (c <= po)
    if min_body_pct > 0:
        if not {"high", "low"} <= set(df.columns):
            raise ValueError("body filter needs high and low columns")
        ratio = body_ratio(df)
        ok = (ratio > min_body_pct) & (ratio.shift() > min_body_pct)
        bull, bear = bull & ok, bear & ok
    return pd.Series(np.where(bull, 1, np.where(bear, -1, 0)), index=df.index)
