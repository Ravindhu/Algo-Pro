"""Owner pattern checks for the four strategies (phase 1: detection only).

Flow stage: Strategy
Status: PHASE 1 IMPLEMENTED (2026-11-19). Plan: docs/IMPLEMENTATION_PLAN_2026-11-18_STRATEGY_SPEC.md

Detection only. No orders, sizing, entries, stops or exits here.
- Works on closed bars. Columns open, high, low, close. Oldest row first.
- A candle body is "more than 50%" when |close - open| / (high - low) > 0.5.
- Red candle: close < open. Green candle: close > open.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

BODY_MIN = 0.5


def _arrays(df: pd.DataFrame):
    return (df["open"].to_numpy(dtype=float), df["high"].to_numpy(dtype=float),
            df["low"].to_numpy(dtype=float), df["close"].to_numpy(dtype=float))


def _body_over_half(o, h, l, c, i: int) -> bool:
    rng = h[i] - l[i]
    return bool(rng > 0 and abs(c[i] - o[i]) / rng > BODY_MIN)


def is_red_green_engulf(df: pd.DataFrame, i: int) -> bool:
    """Engulfing (red then green): bar i-1 red, bar i green, both bodies > 50%, close i > high i-1."""
    if i < 1 or i >= len(df):
        return False
    o, h, l, c = _arrays(df)
    return bool(c[i - 1] < o[i - 1] and c[i] > o[i]
                and _body_over_half(o, h, l, c, i - 1) and _body_over_half(o, h, l, c, i)
                and c[i] > h[i - 1])


def find_sell_fail_trigger(df: pd.DataFrame, last: int, window: int = 10) -> Optional[dict]:
    """Sell-failure (top) check. `last` = newest closed bar index.

    Pattern at bar j: bar j-1 red, bar j green, both bodies > 50%, close j > low j-1.
    Level = the higher of the two highs of the pattern (assumption, see the spec doc).
    Trigger: the newest closed bar `last` is green, has a body > 50%, and closes above the level.
    Only the newest pattern in the window is checked. An earlier close above the level voids it.
    Returns {"pattern_index", "level", "trigger_index"} or None.
    """
    if last < 2 or last >= len(df):
        return None
    o, h, l, c = _arrays(df)
    for j in range(last - 1, max(1, last - window) - 1, -1):
        if not (c[j - 1] < o[j - 1] and c[j] > o[j]
                and _body_over_half(o, h, l, c, j - 1) and _body_over_half(o, h, l, c, j)
                and c[j] > l[j - 1]):
            continue
        level = max(h[j - 1], h[j])
        if any(c[k] > level for k in range(j + 1, last)):
            return None  # level already broken and not failed by a trigger: pattern is spent
        if c[last] > o[last] and _body_over_half(o, h, l, c, last) and c[last] > level:
            return {"pattern_index": j, "level": float(level), "trigger_index": last}
        return None  # newest pattern found and not triggered: nothing to act on
    return None


def find_ocl_rrg(df: pd.DataFrame, last: int) -> Optional[dict]:
    """OCL Red-Red-Green check on closed bars. `last` = newest closed bar index.

    Pattern: C1 red, C2 red, C3 green. Breaks (owner 2026-11-19):
      - 3-candle break: C3 closes above C2 open. Key level = C2 open. Checked on the 3rd candle.
      - 4-candle break: C4 closes above C2 open. Key level = C3 close. Checked on the 4th candle.
    The first break decides. If C3 already broke C2 open, the 4-candle break is ignored.
    Conservative level = lowest body bottom (min of open and close) among C1, C2, C3.
    Returns {"break", "key_level", "conservative", "trigger_index"} or None.
    """
    if last < 2 or last >= len(df):
        return None
    o, _, _, c = _arrays(df)

    def is_rrg(a: int) -> bool:
        return bool(c[a] < o[a] and c[a + 1] < o[a + 1] and c[a + 2] > o[a + 2])

    def conservative(a: int) -> float:
        return float(min(min(o[k], c[k]) for k in (a, a + 1, a + 2)))

    a = last - 2  # pattern = last-2, last-1, last
    if a >= 0 and is_rrg(a) and c[last] > o[a + 1]:
        return {"break": "3-candle", "key_level": float(o[a + 1]),
                "conservative": conservative(a), "trigger_index": last}
    a = last - 3  # pattern = last-3..last-1, C4 = last
    if a >= 0 and is_rrg(a) and not (c[a + 2] > o[a + 1]) and c[last] > o[a + 1]:
        return {"break": "4-candle", "key_level": float(c[a + 2]),
                "conservative": conservative(a), "trigger_index": last}
    return None


def is_swing_low_3(df: pd.DataFrame, i: int) -> bool:
    """3-candle swing low at bar i: low i is below the low of bar i-1 and bar i+1 (both must be closed)."""
    if i < 1 or i + 1 >= len(df):
        return False
    low = df["low"].to_numpy(dtype=float)
    return bool(low[i] < low[i - 1] and low[i] < low[i + 1])


def find_swing_low_sweep(df: pd.DataFrame, last: int, lookback: int = 20) -> Optional[dict]:
    """Liquidity sweep of a 3-candle swing low on closed bars. `last` = newest closed bar index.

    Newest swing low first. A sweep is any later bar (up to `last`) whose low goes below the swing low.
    A wick break and a body close below the level both count; a body close below the level
    also means the low is below it, so one test covers both (owner 2026-11-19).
    Returns {"swing_index", "swing_low", "sweep_index"} or None.
    """
    if last < 2 or last >= len(df):
        return None
    low = df["low"].to_numpy(dtype=float)
    for i in range(last - 1, max(1, last - lookback) - 1, -1):
        if i + 1 > last or not is_swing_low_3(df, i):
            continue
        level = low[i]
        for k in range(i + 1, last + 1):
            if low[k] < level:
                return {"swing_index": i, "swing_low": float(level), "sweep_index": k}
    return None
