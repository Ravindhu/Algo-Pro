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
    """OCL Red-Red-Green check on the last 3 closed bars only. `last` = newest closed bar index.

    Candles: C1 = last-2, C2 = last-1, C3 = last. Pattern: C1 red, C2 red, C3 green.
    The first break that happens confirms the setup (upward closes only, owner 2026-11-19):
      - "2-candle" break: C2 closes above C1 open. Key level = C1 open. Confirmed on C2.
      - "3-candle" break: C3 closes above C2 close. Key level = C2 close. Confirmed on C3.
    Conservative level = the lowest body bottom (min of open and close) among C1, C2, C3. It applies to both breaks.
    Returns {"break", "key_level", "conservative", "trigger_index"} or None.
    """
    if last < 2 or last >= len(df):
        return None
    o, _, _, c = _arrays(df)
    c1, c2, c3 = last - 2, last - 1, last
    if not (c[c1] < o[c1] and c[c2] < o[c2] and c[c3] > o[c3]):
        return None
    conservative = float(min(min(o[k], c[k]) for k in (c1, c2, c3)))
    if c[c2] > o[c1]:
        return {"break": "2-candle", "key_level": float(o[c1]),
                "conservative": conservative, "trigger_index": c2}
    if c[c3] > c[c2]:
        return {"break": "3-candle", "key_level": float(c[c2]),
                "conservative": conservative, "trigger_index": c3}
    return None
