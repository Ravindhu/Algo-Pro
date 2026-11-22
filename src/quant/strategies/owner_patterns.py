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


def find_ocl_rrg(df: pd.DataFrame, last: int, ltf: bool = False) -> Optional[dict]:
    """OCL Red-Red-Green check on closed bars. `last` = newest closed bar index.

    Pattern: C1 red, C2 red, C3 green. Breaks (owner 2026-11-19, 2026-11-22):
      - 3-candle break: C3 closes above C2 open. Key level = C2 open. Checked on the 3rd candle.
      - 4-candle break: C4 closes above C2 open. Key level = C3 close. Checked on the 4th candle.
      - 5-candle break (LTF only, owner 2026-11-22): C3 and C4 did not close above C2 open, C5 closes above it.
        Key level = C4 close. Checked on the 5th candle. Conservative = lowest body bottom of C1..C4.
    The first break decides. A later break is ignored once an earlier one has fired.
    Conservative level = lowest body bottom (min of open and close) among the pattern candles (C1..C3).
    ltf=False (HTF and MTF) never returns the 5-candle break.
    Returns {"break", "key_level", "conservative", "trigger_index", "start", "pattern_len"} or None.
    `start` = index of C1; `pattern_len` = number of candles in the pattern before the trigger candle.
    """
    if last < 2 or last >= len(df):
        return None
    o, _, _, c = _arrays(df)

    def is_rrg(a: int) -> bool:
        return bool(c[a] < o[a] and c[a + 1] < o[a + 1] and c[a + 2] > o[a + 2])

    def conservative(a: int, n: int) -> float:
        return float(min(min(o[k], c[k]) for k in range(a, a + n)))

    a = last - 2  # pattern = last-2, last-1, last
    if a >= 0 and is_rrg(a) and c[last] > o[a + 1]:
        return {"break": "3-candle", "key_level": float(o[a + 1]),
                "conservative": conservative(a, 3), "trigger_index": last}
    a = last - 3  # pattern = last-3..last-1, C4 = last
    if a >= 0 and is_rrg(a) and not (c[a + 2] > o[a + 1]) and c[last] > o[a + 1]:
        return {"break": "4-candle", "key_level": float(c[a + 2]),
                "conservative": conservative(a, 3), "trigger_index": last}
    a = last - 4  # LTF only: pattern = last-4..last-2, C4 = last-1, C5 = last
    if ltf and a >= 0 and is_rrg(a) and not (c[a + 2] > o[a + 1]) and not (c[a + 3] > o[a + 1]) \
            and c[last] > o[a + 1]:
        return {"break": "5-candle", "key_level": float(c[a + 3]),
                "conservative": conservative(a, 4), "trigger_index": last}
    return None


_C1_OFFSET = {"3-candle": 2, "4-candle": 3, "5-candle": 4}  # C1 is this many bars before the trigger
_PATTERN_LEN = {"3-candle": 3, "4-candle": 3, "5-candle": 4}  # candles from C1 used for the pattern low


def _with_span(ocl: dict, last: int) -> dict:
    """Adds the pattern span to an OCL dict: start = index of C1, pattern_len = candles from C1 for the low."""
    return {**ocl, "start": last - _C1_OFFSET[ocl["break"]], "pattern_len": _PATTERN_LEN[ocl["break"]]}


def _all_bodies_over_half(df: pd.DataFrame, start: int, end: int) -> bool:
    """True when every candle from start to end (both included) has a body over 50 %."""
    o, h, l, c = _arrays(df)
    return all(_body_over_half(o, h, l, c, k) for k in range(start, end + 1))


def find_ocl_fail_top(df: pd.DataFrame, last: int) -> Optional[dict]:
    """Sell-fail top pattern (owner 2026-11-22): a 3- or 4-candle OCL whose C1..C3 bodies are all over 50 %.
    Level (touched) = highest body top of C1..C3. Conservative = open of the candle before the trigger.
    Confirmation is checked by the caller (fail_touch_from). Returns the OCL dict plus "level", or None."""
    ocl = find_ocl_rrg(df, last)  # 3- and 4-candle breaks only, never the LTF-only 5-candle break
    ocl = _with_span(ocl, last) if ocl is not None else None
    if ocl is None or not _all_bodies_over_half(df, ocl["start"], ocl["start"] + 2):
        return None
    mo, _, _, mc = _arrays(df)
    a = ocl["start"]
    level = float(max(max(mo[k], mc[k]) for k in range(a, a + 3)))
    return {**ocl, "level": level, "conservative": float(mo[last - 1])}


def find_ocl_fail_bottom_level(df: pd.DataFrame, last: int) -> Optional[dict]:
    """Sell-fail bottom pattern level (owner 2026-11-22): a 3- or 4-candle OCL whose bodies from C1 up to the
    breakout candle are all over 50 %. Single level = lowest body bottom from C1 up to the breakout candle
    (owner 2026-11-19). Returns the OCL dict plus "level", or None."""
    ocl = find_ocl_rrg(df, last)
    ocl = _with_span(ocl, last) if ocl is not None else None
    if ocl is None or not _all_bodies_over_half(df, ocl["start"], last):
        return None
    mo, _, _, mc = _arrays(df)
    a = ocl["start"]
    return {**ocl, "level": float(min(min(mo[k], mc[k]) for k in range(a, last + 1)))}


def find_engulf_ltf(df: pd.DataFrame, last: int) -> Optional[dict]:
    """LTF engulfing (owner 2026-11-22: engulfing is an LTF entry pattern too). Pattern = candles last-1 and last:
    red then green, both bodies over 50 %, close of `last` above the high of `last-1`.
    Aggressive key level = open of the first candle (as the MTF engulfing). Conservative = lowest body bottom of the two."""
    if last < 1 or last >= len(df) or not is_red_green_engulf(df, last):
        return None
    o, _, _, c = _arrays(df)
    a = last - 1
    return {"break": "engulf", "key_level": float(o[a]),
            "conservative": float(min(o[a], c[a], o[last], c[last])),
            "trigger_index": last, "start": a, "pattern_len": 2}


def find_ltf_patterns(df: pd.DataFrame, last: int) -> list:
    """All LTF entry patterns that complete on bar `last` (owner 2026-11-22). Candidates are OCL (3/4/5-candle
    break), engulfing and sell-fail top; each one is a candidate and the first key-level touch wins.
    Each dict has: break, key_level (aggressive, the touched level), conservative, start, pattern_len,
    trigger_index, touch_from (first bar index whose range may touch the key level). A fail pattern whose
    confirmation is voided is left out; a fail pattern still waiting for its confirm candle gets a touch_from
    beyond the data, so it cannot touch yet."""
    out = []
    ocl = find_ocl_rrg(df, last, ltf=True)
    if ocl is not None:
        out.append({**_with_span(ocl, last), "touch_from": last + 1})
    eng = find_engulf_ltf(df, last)
    if eng is not None:
        out.append({**eng, "touch_from": last + 1})
    top = find_ocl_fail_top(df, last)
    if top is not None:
        touch_from = fail_touch_from(df, "ocl_top", last, top["level"], top["start"])
        if touch_from is not None:
            out.append({**top, "key_level": top["level"], "touch_from": touch_from})
    return out


def is_swing_low_3(df: pd.DataFrame, i: int) -> bool:
    """3-candle swing low at bar i: low i is below the low of bar i-1 and bar i+1 (both must be closed)."""
    if i < 1 or i + 1 >= len(df):
        return False
    low = df["low"].to_numpy(dtype=float)
    return bool(low[i] < low[i - 1] and low[i] < low[i + 1])


def swing_pattern(df: pd.DataFrame, i: int) -> Optional[tuple]:
    """Owner 2026-11-22: candle i is a candle of a break pattern on this timeframe (any candle of the pattern).
    Allowed breaks here: engulfing (2 candles), 3-candle and 4-candle OCL. The 5-candle break is LTF only.
      - engulfing: candle i is the red candle or the green engulf (is_red_green_engulf). Completes at the green candle.
        Level = low of candle i (no rule given for engulfing yet).
      - 3-candle OCL: pattern C1..C3 contains candle i. Completes at C3. Level = lowest low of C1..C3 (owner 2026-11-22).
      - 4-candle OCL: pattern C1..C3 contains candle i, trigger C4 closes above C2 open. Completes at C4.
        Level = lowest low of C1..C4 (owner 2026-11-22).
    Returns (completion_index, level) for the earliest such break, or None.
    A sweep counts only after completion_index, so the swing is never used before its pattern has closed."""
    n = len(df)
    if i < 0 or i >= n:
        return None
    _, _, low_arr, _ = _arrays(df)
    # Engulfing: candle i is the red candle (i+1 green) or the green engulf (i-1 red).
    for a in (i - 1, i):
        if a >= 0 and a + 1 < n and is_red_green_engulf(df, a + 1):
            return a + 1, float(low_arr[i])
    # OCL 3- and 4-candle breaks: the pattern runs from its C1 to its trigger, and must contain candle i.
    for j in range(i, min(i + 4, n)):
        ocl = find_ocl_rrg(df, j)  # HTF and MTF rules: 3- and 4-candle breaks only
        if ocl is None:
            continue
        c1 = j - _C1_OFFSET[ocl["break"]]
        if c1 < 0 or c1 > i:
            continue
        level = float(min(low_arr[c1: j + 1]))  # C1..C3 for a 3-candle break, C1..C4 for a 4-candle break
        return j, level
    return None


def swing_break_index(df: pd.DataFrame, i: int) -> Optional[int]:
    """Completion index of the swing pattern at candle i (see swing_pattern)."""
    got = swing_pattern(df, i)
    return got[0] if got is not None else None


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
        if i + 1 > last:
            continue
        got = swing_pattern(df, i)  # owner 2026-11-22: any candle of a break pattern; level from the pattern
        if got is None or got[0] > last:
            continue
        j, level = got
        for k in range(max(i + 1, j + 1), last + 1):  # sweep after the break pattern has closed
            if low[k] < level:
                return {"swing_index": i, "swing_low": float(level), "sweep_index": k}
    return None


# ---- Sequence strategies (owner rules 2026-11-19) ----
# Five strategies share one sequence. They differ only in the MTF setup and its key level.
#   MTF setup confirmed on bar close -> later MTF bar touches the level -> HTF swing low swept
#   -> LTF OCL forms after the sweep -> LTF bar touches the OCL aggressive key level = trigger.
# SEQUENCE_KINDS: strategy name -> MTF setup kind. Levels follow docs/STRATEGY_RULES_2026-11-19.md section 2 and 4.1.
SEQUENCE_KINDS = {
    "engulfing": "egpattern_open",          # owner r4: aggressive level = open of candle 1
    "engulfing_fail": "egpattern_body",     # owner 2026-11-19: level = highest candle body top of the two candles
    "ocl_seq": "ocl_key",                   # OCL on MTF; level = OCL aggressive key level
    "ocl_sell_fail_top_seq": "ocl_top",     # owner 2026-11-19: aggressive level = highest candle body top of the OCL pattern
    "ocl_sell_fail_bottom_seq": "ocl_bottom",  # owner 2026-11-19: one level only (lowest body bottom of the OCL pattern); NO HTF sweep
}

# Kinds that skip the HTF sweep step (owner 2026-11-19: the bottom conservative level is already a swing, so no sweep needed)
NO_SWEEP_KINDS = {"ocl_bottom"}


def _ocl_start(end: int, ocl: dict) -> int:
    """Index of C1 for an OCL that ends (trigger candle) at `end`."""
    if "start" in ocl:
        return int(ocl["start"])
    return end - _C1_OFFSET[ocl["break"]]


# Fail kinds (owner 2026-11-22): every fail pattern needs >50 % body candles and a confirm candle.
#   egpattern_body (engulfing_fail), ocl_top (sell-fail top), ocl_bottom (sell-fail bottom).
FAIL_KINDS = {"egpattern_body", "ocl_top", "ocl_bottom"}


def fail_touch_from(df: pd.DataFrame, kind: str, end: int, level: float, start: int) -> Optional[int]:
    """Confirmation for a fail pattern (owner 2026-11-20 rule 3.3, extended 2026-11-22 to every fail pattern).
    The first close after the pattern (`end`) above the pattern's highest HIGH must be a green candle with a
    body over 50 %. That candle is the confirm, and touches of the level count from the next bar.
    Any other first close above the high voids the pattern (returns None).
    Returns the first bar index whose range may touch the level. If no close has gone above the high yet,
    returns len(df)+1 (pending: the level cannot be touched yet)."""
    o, h, l, c = _arrays(df)
    pattern_high = float(max(h[start: end + 1]))
    for k in range(end + 1, len(df)):
        if c[k] > pattern_high:
            if c[k] > o[k] and _body_over_half(o, h, l, c, k):
                return k + 1
            return None
    return len(df) + 1


def setup_touch_from(mtf: pd.DataFrame, kind: str, end: int, level: float) -> Optional[int]:
    """First MTF bar index whose range may touch the setup level. Non-fail kinds: the bar after the pattern.
    Fail kinds: the bar after the confirm candle (see fail_touch_from). None when voided or still pending."""
    if kind not in FAIL_KINDS:
        return end + 1
    start = end - 1 if kind == "egpattern_body" else _ocl_start(end, find_ocl_rrg(mtf, end))
    tf = fail_touch_from(mtf, kind, end, level, start)
    if tf is None or tf >= len(mtf):
        return None
    return tf


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
    if kind == "ocl_key":
        for m in range(n_m - 1, 1, -1):
            ocl = find_ocl_rrg(mtf, m)
            if ocl is not None:
                return m, float(ocl["key_level"])
        return None
    if kind in ("ocl_top", "ocl_bottom"):
        # Fail kinds (owner 2026-11-22): every candle of the pattern must have a body over 50 %.
        for m in range(n_m - 1, 1, -1):
            fail = find_ocl_fail_top(mtf, m) if kind == "ocl_top" else find_ocl_fail_bottom_level(mtf, m)
            if fail is not None:
                return m, float(fail["level"])
        return None
    raise ValueError(f"unknown setup kind {kind!r}")


def _mtf_conservative(mtf: pd.DataFrame, kind: str, end: int) -> Optional[float]:
    """Conservative level of the MTF setup that ends at candle `end` (see rules section 4.1)."""
    mo, _, _, mc = _arrays(mtf)
    if kind in ("egpattern_open", "egpattern_body"):
        return float(min(mo[end - 1], mc[end - 1], mo[end], mc[end]))  # lowest body bottom of the two candles
    if kind == "ocl_top":
        return float(mo[end - 1])
    return None


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
    mo, _, _, _ = _arrays(mtf)
    # Conservative level (owner 2026-11-19, rules section 4.1). Recorded, not used for the touch:
    #   engulfing / engulfing_fail: lowest body bottom of the two engulfing candles
    #   ocl_top: open of the candle before the trigger (C2 for 3-candle, C3 for 4-candle)
    #   ocl_seq: the LTF OCL conservative level (set below, when the LTF OCL is found)
    #   ocl_bottom: none (single level)
    conservative_level = _mtf_conservative(mtf, kind, end)
    _, mh, ml, _ = _arrays(mtf)
    n_m = len(mtf) - 1
    # Fail kinds: the pattern counts only after its confirm candle (owner 2026-11-20, extended 2026-11-22).
    first_touch_bar = setup_touch_from(mtf, kind, end, level)
    if first_touch_bar is None:
        return None
    touch = next((k for k in range(first_touch_bar, n_m + 1) if ml[k] <= level <= mh[k]), None)
    if touch is None:
        return None
    touch_time = mtf.index[touch]
    if kind in NO_SWEEP_KINDS:
        sweep_time = touch_time  # no HTF sweep: the LTF OCL only has to form after the touch
    else:
        sweep = find_swing_low_sweep(htf, len(htf) - 1)
        if sweep is None or htf.index[sweep["sweep_index"]] < touch_time:
            return None
        sweep_time = htf.index[sweep["sweep_index"]]
    lo, lh, ll, _ = _arrays(ltf)
    n_l = len(ltf) - 1
    for m in range(n_l, 1, -1):
        if ltf.index[m] < sweep_time:
            break
        # Every LTF pattern that completes on bar m is a candidate; the first key touch wins (owner 2026-11-22).
        for pat in find_ltf_patterns(ltf, m):
            key = pat["key_level"]
            touches = [p for p in range(max(m + 1, pat["touch_from"]), n_l + 1) if ll[p] <= key <= lh[p]]
            if not touches:
                continue  # formed after the sweep, not yet touched
            if fresh and touches[0] != n_l:
                continue  # touched earlier: stale, do not enter on an old bar
            return {"pattern_time": mtf.index[end], "touch_time": touch_time, "sweep_time": sweep_time,
                    "conservative_level": pat["conservative"] if kind == "ocl_key" else conservative_level,
                    "ocl_time": ltf.index[m], "ocl_start_time": ltf.index[pat["start"]],
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


def setup_low(mtf: pd.DataFrame, kind: str, end: int) -> float:
    """Lowest low of the MTF setup candles. This is the MTF invalidation price (owner 2026-11-19)."""
    _, _, ml, _ = _arrays(mtf)
    if kind in ("egpattern_open", "egpattern_body"):
        return float(min(ml[end - 1], ml[end]))
    ocl = find_ocl_rrg(mtf, end)
    start = _ocl_start(end, ocl)
    return float(min(ml[start: end + 1]))


def first_sweep_after(htf: pd.DataFrame, touch_time) -> Optional[tuple]:
    """Sweep for the cycle (owner 2026-11-19: one sweep is enough). Takes the NEWEST 3-candle swing low that
    formed before the MTF touch and is broken (wick or body) at or after the touch.
    Returns (swing_index, sweep_index) or None."""
    low = htf["low"].to_numpy(dtype=float)
    best = None
    for i in range(1, len(htf) - 1):
        if htf.index[i] >= touch_time:
            break
        got = swing_pattern(htf, i)  # owner 2026-11-22: any candle of a break pattern; level from the pattern
        if got is None:
            continue
        j, level = got
        for k in range(max(i + 1, j + 1), len(htf)):
            if htf.index[k] >= touch_time and low[k] < level:
                best = (i, k)
                break
    return best
