"""Owner pattern checks and the five sequence strategies.

Flow stage: Strategy, Engulf sequence, Engulf signal, Engulfing fail, OCL strategy (RRG), OCL sell-fail top, OCL sell-fail bottom
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

from quant.indicators.indicators import atr as atr_series

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


# ---- Sequence strategies (owner rules 2026-11-19) ----
# Five strategies share one sequence. They differ only in the MTF setup and its key level.
#   MTF setup confirmed on bar close -> later MTF bar touches the level -> HTF swing low swept
#   -> LTF OCL forms after the sweep -> LTF bar touches the OCL aggressive key level = trigger.
# SEQUENCE_KINDS: strategy name -> MTF setup kind. The level rules are ASSUMPTIONS where
# marked (owner to confirm, see docs/STRATEGY_RULES_2026-11-19.md).
SEQUENCE_KINDS = {
    "engulfing": "egpattern_open",          # owner r4: aggressive level = open of candle 1
    "engulfing_fail": "egpattern_body",     # owner 2026-11-19: level = highest candle body top of the two candles
    "ocl_seq": "ocl_key",                   # OCL on MTF; level = OCL aggressive key level
    "ocl_sell_fail_top_seq": "ocl_top",     # ASSUMED: level = open of the candle before the trigger (C2 open for 3-candle, C3 open for 4-candle)
    "ocl_sell_fail_bottom_seq": "ocl_bottom",  # owner 2026-11-19: level = lowest low from OCL C1 to the sell-fail candle (swing low)
}


def _ocl_start(end: int, ocl: dict) -> int:
    """Index of C1 for an OCL that ends (trigger candle) at `end`."""
    return end - 2 if ocl["break"] == "3-candle" else end - 3


def _newest_setup(mtf: pd.DataFrame, kind: str) -> Optional[tuple]:
    """Newest completed MTF setup as (end_index, level). end_index = last candle of the pattern.
    The pattern must have a later bar, so the search starts one bar before the newest."""
    mo, mh, ml, mc = _arrays(mtf)
    n_m = len(mtf) - 1
    if kind in ("egpattern_open", "egpattern_body"):
        j = next((i for i in range(n_m - 1, 0, -1) if is_red_green_engulf(mtf, i)), None)
        if j is None:
            return None
        if kind == "egpattern_open":
            level = float(mo[j - 1])
        else:  # highest body top (max of open and close) of the two candles
            level = float(max(max(mo[j - 1], mc[j - 1]), max(mo[j], mc[j])))
        return j, level
    if kind in ("ocl_key", "ocl_top", "ocl_bottom"):
        for m in range(n_m - 1, 1, -1):
            ocl = find_ocl_rrg(mtf, m)
            if ocl is None:
                continue
            start = _ocl_start(m, ocl)
            if kind == "ocl_key":
                level = float(ocl["key_level"])
            elif kind == "ocl_top":
                level = float(mo[m - 1])  # open of the candle before the trigger: C2 (3-candle) or C3 (4-candle)
            else:
                level = float(min(ml[start:m + 1]))
            return m, level
        return None
    raise ValueError(f"unknown setup kind {kind!r}")


def sequence(mtf: pd.DataFrame, htf: pd.DataFrame, ltf: pd.DataFrame,
             kind: str = "egpattern_open", fresh: bool = False) -> Optional[dict]:
    """Sequence check on closed bars of three timeframes. Returns the time of each step and the entry, or None.

    fresh=True: the trigger counts only when the touch is on the newest closed LTF bar (owner 2026-11-19).
    An older touch means the setup is already stale, so it returns None.
    """
    if len(mtf) < 3 or len(htf) < 4 or len(ltf) < 4:
        return None
    setup = _newest_setup(mtf, kind)
    if setup is None:
        return None
    end, level = setup
    _, mh, ml, _ = _arrays(mtf)
    n_m = len(mtf) - 1
    touch = next((k for k in range(end + 1, n_m + 1) if ml[k] <= level <= mh[k]), None)
    if touch is None:
        return None
    touch_time = mtf.index[touch]
    sweep = find_swing_low_sweep(htf, len(htf) - 1)
    if sweep is None or htf.index[sweep["sweep_index"]] < touch_time:
        return None
    sweep_time = htf.index[sweep["sweep_index"]]
    lo, lh, ll, _ = _arrays(ltf)
    n_l = len(ltf) - 1
    for m in range(n_l, 1, -1):
        if ltf.index[m] < sweep_time:
            break
        ocl = find_ocl_rrg(ltf, m)
        if ocl is None:
            continue
        key = ocl["key_level"]
        touches = [p for p in range(m + 1, n_l + 1) if ll[p] <= key <= lh[p]]
        if not touches:
            return None  # OCL formed after the sweep, not yet touched
        if fresh and touches[0] != n_l:
            return None  # touched earlier: stale, do not enter on an old bar
        return {"pattern_time": mtf.index[end], "touch_time": touch_time, "sweep_time": sweep_time,
                "ocl_time": ltf.index[m], "ocl_start_time": ltf.index[_ocl_start(m, ocl)],
                "trigger_time": ltf.index[touches[0]], "entry": float(key)}
    return None


def engulf_sequence(mtf: pd.DataFrame, htf: pd.DataFrame, ltf: pd.DataFrame) -> Optional[dict]:
    """Engulfing sequence (kind egpattern_open), no freshness rule. Kept for the first tests."""
    return sequence(mtf, htf, ltf, "egpattern_open", fresh=False)


def sequence_signal(kind: str, mtf: pd.DataFrame, htf: pd.DataFrame, ltf: pd.DataFrame, *,
                    name: str, fresh: bool = True, atr_period: int = 14, rr: float = 3.0) -> Optional[dict]:
    """Bot signal from a sequence. ASSUMED rules (owner to confirm):
    entry = the LTF OCL aggressive key level that was touched;
    stop = lowest low of the LTF OCL pattern candles C1..C3;
    target = rr x stop distance (3R); ATR = ATR(atr_period) on LTF closed bars.
    Returns the bot signal dict or None.
    """
    res = sequence(mtf, htf, ltf, kind, fresh=fresh)
    if res is None:
        return None
    s = ltf.index.get_loc(res["ocl_start_time"])
    if s + 3 > len(ltf):
        return None
    lows = ltf["low"].to_numpy(dtype=float)
    stop = float(min(lows[s: s + 3]))
    entry = float(res["entry"])
    sl_dist = entry - stop
    if sl_dist <= 0:
        return None
    highs = ltf["high"].to_numpy(dtype=float).tolist()
    closes = ltf["close"].to_numpy(dtype=float).tolist()
    atr_val = atr_series(highs, lows.tolist(), closes, period=atr_period)[-1]
    if atr_val is None or atr_val <= 0:
        return None
    return {"side": "BUY", "entry": entry, "sl_dist": float(sl_dist), "tp_dist": float(rr * sl_dist),
            "atr": float(atr_val), "time": str(res["trigger_time"]), "strategy": name}


def engulf_signal(mtf: pd.DataFrame, htf: pd.DataFrame, ltf: pd.DataFrame, **kwargs) -> Optional[dict]:
    """Engulfing signal (kind egpattern_open). Kept as the named entry point for the Engulfing strategy."""
    return sequence_signal("egpattern_open", mtf, htf, ltf, name="engulfing", **kwargs)
