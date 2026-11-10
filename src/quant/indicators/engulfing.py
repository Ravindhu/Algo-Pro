"""Flow stage: Signal and conviction (engulfing candle logic, shared by strategies).
Status: PORTED COPY of the candle logic in Algo-Lab src/algolab/framework/strategies/engulfing_strategy.py
(body_ratio, engulfing, fib_entry_level). Tests: src/quant/tests/test_signals_engulfing.py.

Candle logic only. Used by the OCL sell-failure top and bottom strategies (pattern check) and by the
separate engulfing strategy (which adds fib entries). The DataFrame form and the single-candle form
apply the same rules; a parity test keeps them equal.

Single-candle form takes dicts with open, high, low, close (prev = older candle, cur = newer candle).
"""
from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd

DEFAULT_MIN_BODY_PCT = 0.5
FIB_ENTRY = 0.886   # entry sits 88.6% of the way back into the engulfing candle's range


def fib_entry_level(high: float, low: float, direction: int, fib: float = FIB_ENTRY) -> float:
    """Entry level inside the engulfing candle, measured from its extreme in the trade direction.
    BUY : high - fib * range (a deep pullback from the high).
    SELL: low  + fib * range (a deep pullback from the low)."""
    rng = high - low
    return high - fib * rng if direction > 0 else low + fib * rng


def body_ratio(df: pd.DataFrame) -> pd.Series:
    """Candle body as a share of its full range (high - low). 0 where the range is 0."""
    rng = df["high"] - df["low"]
    body = (df["close"] - df["open"]).abs()
    return (body / rng.where(rng > 0)).fillna(0.0)


def engulfing(df: pd.DataFrame, min_body_pct: float = DEFAULT_MIN_BODY_PCT) -> pd.Series:
    """+1 bullish engulfing, -1 bearish engulfing, 0 none (evaluated per bar).

    Both candles of the pattern must have a body larger than min_body_pct of their range.
    The default is 0.5 (more than 50 percent). Set it to 0 to ignore body size."""
    o, c = df["open"], df["close"]
    po, pc = o.shift(), c.shift()
    bull = (pc < po) & (c > o) & (o <= pc) & (c >= po)
    bear = (pc > po) & (c < o) & (o >= pc) & (c <= po)
    if min_body_pct > 0:
        if not {"high", "low"} <= set(df.columns):
            raise ValueError("body filter needs high and low columns")
        ok = (body_ratio(df) > min_body_pct) & (body_ratio(df).shift() > min_body_pct)
        bull, bear = bull & ok, bear & ok
    return pd.Series(np.where(bull, 1, np.where(bear, -1, 0)), index=df.index)


def _candle_body_fraction(c: Mapping[str, float]) -> float:
    """Body / range for one candle; 0 when the range is 0 (same rule as body_ratio)."""
    rng = float(c["high"]) - float(c["low"])
    if rng <= 0:
        return 0.0
    return abs(float(c["close"]) - float(c["open"])) / rng


def _bodies_pass(prev: Mapping[str, float], cur: Mapping[str, float], min_body_pct: float) -> bool:
    if min_body_pct <= 0:
        return True
    return _candle_body_fraction(prev) > min_body_pct and _candle_body_fraction(cur) > min_body_pct


def engulfing_bull_pair(prev: Mapping[str, float], cur: Mapping[str, float],
                        min_body_pct: float = DEFAULT_MIN_BODY_PCT) -> bool:
    """True when cur is a bullish engulfing of prev (same rule as engulfing(), bull side)."""
    po, pc = float(prev["open"]), float(prev["close"])
    o, c = float(cur["open"]), float(cur["close"])
    shape = (pc < po) and (c > o) and (o <= pc) and (c >= po)
    return shape and _bodies_pass(prev, cur, min_body_pct)


def engulfing_bear_pair(prev: Mapping[str, float], cur: Mapping[str, float],
                        min_body_pct: float = DEFAULT_MIN_BODY_PCT) -> bool:
    """True when cur is a bearish engulfing of prev (same rule as engulfing(), bear side)."""
    po, pc = float(prev["open"]), float(prev["close"])
    o, c = float(cur["open"]), float(cur["close"])
    shape = (pc > po) and (c < o) and (o >= pc) and (c <= po)
    return shape and _bodies_pass(prev, cur, min_body_pct)
