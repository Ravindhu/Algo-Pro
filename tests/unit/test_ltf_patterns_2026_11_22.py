"""LTF entry patterns and fail-pattern rules (owner 2026-11-22).

- OCL 3/4/5-candle breaks on the LTF only; the 5-candle break key is C4 close, conservative = lowest body bottom C1..C4.
- Engulfing is an LTF entry pattern (key = open of the first candle).
- Sell-fail top is an LTF candidate too (highest body top of C1..C3).
- Every fail pattern needs >50 % body candles and a confirm candle (green, body >50 %, closes above the pattern high).
- All LTF candidates are tracked; the first key-level touch wins.
"""
from __future__ import annotations

import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "quant"))

from quant.strategies import cycle, owner_patterns as op  # noqa: E402


def _frame(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


# OCL pattern used across these tests: C1 red, C2 red (open 11.0 = key level), C3 green closing above 11.0.
RRG_C1 = (10.0, 10.2, 9.0, 9.2)              # red, body 80 %
RRG_C2 = (11.0, 11.1, 10.1, 10.5)            # red, body exactly 50 %: fails the fail-pattern body rule
RRG_C2_STRONG = (11.0, 11.0, 9.8, 10.0)      # red, body 83 %
C3_BREAK = (10.4, 11.6, 10.3, 11.2)          # green, body 61 %, closes 11.2 above C2 open 11.0
FAIL_CONFIRM = (11.0, 12.0, 10.9, 11.9)      # green, body 82 %, closes above the pattern high 11.6


# ---- 5-candle break (LTF only) ----
C1_5 = (10.0, 10.2, 9.0, 9.2)                # red
C2_5 = (11.0, 11.0, 9.8, 10.0)               # red, open 11.0 = break level
C3_5 = (9.9, 10.5, 9.8, 10.4)                # green, closes 10.4: no break
C4_5 = (10.3, 10.6, 10.0, 10.5)              # green, closes 10.5 (the key level): no break
C5_5 = (10.4, 11.5, 10.3, 11.2)              # green, closes 11.2 above 11.0: 5-candle break


def test_five_candle_break_is_ltf_only():
    df = _frame([C1_5, C2_5, C3_5, C4_5, C5_5])
    assert op.find_ocl_rrg(df, 4) is None                      # HTF / MTF rules: no 5-candle break
    ocl = op.find_ocl_rrg(df, 4, ltf=True)
    assert ocl["break"] == "5-candle"
    assert ocl["key_level"] == 10.5                            # C4 close
    assert ocl["conservative"] == 9.2                          # lowest body bottom of C1..C4 (C1 body bottom)
    assert ocl["trigger_index"] == 4


def test_five_candle_break_refused_when_c3_or_c4_already_broke_c2_open():
    c4_breaks = (10.3, 11.4, 10.2, 11.1)                       # C4 closes 11.1 above 11.0
    df = _frame([C1_5, C2_5, C3_5, c4_breaks, C5_5])
    ocl = op.find_ocl_rrg(df, 4, ltf=True)
    assert ocl is None or ocl["break"] != "5-candle"


# ---- LTF engulfing ----
RED = (10.0, 10.2, 8.0, 8.2)
GREEN_ENGULF = (8.0, 11.0, 7.9, 10.8)


def test_ltf_engulf_key_is_open_of_first_candle_and_conservative_is_lowest_body_bottom():
    eng = op.find_engulf_ltf(_frame([RED, GREEN_ENGULF]), 1)
    assert eng["break"] == "engulf"
    assert eng["key_level"] == 10.0                            # open of the red candle
    assert eng["conservative"] == 8.0                          # min(10.0, 8.2, 8.0, 10.8)
    assert eng["start"] == 0 and eng["pattern_len"] == 2


# ---- fail patterns: body rule and confirm candle ----
def test_fail_top_needs_bodies_over_half():
    assert op.find_ocl_fail_top(_frame([RRG_C1, RRG_C2, C3_BREAK]), 2) is None      # C2 body exactly 50 %
    fail = op.find_ocl_fail_top(_frame([RRG_C1, RRG_C2_STRONG, C3_BREAK]), 2)
    assert fail is not None and fail["level"] == 11.2                              # highest body top of C1..C3


def test_fail_top_waits_for_confirm_candle_before_touch():
    # no confirm candle: the level cannot be touched yet
    mtf = _frame([RRG_C1, RRG_C2_STRONG, C3_BREAK, (11.3, 11.8, 10.9, 11.5)])
    assert op.sequence(mtf, _htf_sweep_late(), _ltf_pattern_with_sweep(), "ocl_top") is None


def test_fail_top_voided_by_red_close_above_high():
    red_break = (11.9, 12.0, 11.5, 11.7)                       # closes 11.7 above pattern high 11.6, red
    mtf = _frame([RRG_C1, RRG_C2_STRONG, C3_BREAK, red_break, (11.3, 11.8, 10.9, 11.5)])
    assert op.fail_touch_from(mtf, "ocl_top", 2, 11.2, 0) is None


def test_fail_touch_from_is_bar_after_green_confirm():
    mtf = _frame([RRG_C1, RRG_C2_STRONG, C3_BREAK, FAIL_CONFIRM])
    assert op.fail_touch_from(mtf, "ocl_top", 2, 11.2, 0) == 4


def test_fail_touch_pending_when_no_close_above_high_yet():
    mtf = _frame([RRG_C1, RRG_C2_STRONG, C3_BREAK])
    assert op.fail_touch_from(mtf, "ocl_top", 2, 11.2, 0) == len(mtf) + 1


# ---- LTF candidates: all tracked, first key touch wins ----
def test_ltf_candidates_include_ocl_and_sell_fail_top():
    df = _frame([RRG_C1, RRG_C2_STRONG, C3_BREAK])
    pats = op.find_ltf_patterns(df, 2)
    # OCL (key 11.0), engulfing C2-C3 (key = open of C2 = 11.0) and sell-fail top (level = highest body top 11.2)
    assert [p["break"] for p in pats] == ["3-candle", "engulf", "3-candle"]
    assert sorted(p["key_level"] for p in pats) == [11.0, 11.0, 11.2]
    fail = next(p for p in pats if p["key_level"] == 11.2)
    assert fail["touch_from"] == len(df) + 1                   # pending: no confirm candle yet


def test_ltf_loop_first_key_touch_wins_and_cancels_other_candidates():
    # 3-candle OCL key 11.0 and sell-fail top level 11.2 both form on bar 2.
    # Bar 3 (FAIL_CONFIRM) touches 11.0 first, so the OCL wins; the sell-fail top is cancelled.
    ltf = _frame([RRG_C1, RRG_C2_STRONG, C3_BREAK, FAIL_CONFIRM, (11.3, 11.8, 10.9, 11.5)])
    entries, _, _, _, _, _ = cycle._ltf_loop(ltf, -1, 9.0, None, None)
    assert [(e.kind, e.price) for e in entries][0] == ("aggressive", 11.0)
    assert all(e.price != 11.2 for e in entries)


def test_ltf_five_candle_entry_then_conservative_touch():
    touch5 = (10.8, 10.9, 10.2, 10.6)          # touches key 10.5
    cons6 = (9.5, 9.6, 9.0, 9.4)               # touches conservative 9.2, closes above the pattern low 9.0
    ltf = _frame([C1_5, C2_5, C3_5, C4_5, C5_5, touch5, cons6])
    entries, any_cons, status, _, _, _ = cycle._ltf_loop(ltf, -1, 9.0, None, None)
    assert [(e.kind, e.price) for e in entries] == [("aggressive", 10.5), ("conservative", 9.2)]
    assert any_cons is True


# helpers for the fail-confirm test: a sweep after the MTF touch and one LTF OCL
def _htf_sweep_late():
    return _frame([(10, 10.5, 9.5, 10.0), (9.8, 10.0, 9.0, 9.4), (9.6, 9.9, 9.3, 9.7), (9.6, 9.9, 9.3, 9.6),
                   (9.5, 9.8, 9.2, 9.6), (9.5, 9.6, 8.5, 9.0)])


def _ltf_pattern_with_sweep():
    return _frame([(9.0, 9.5, 8.8, 9.2)] * 3 + [RRG_C1, RRG_C2_STRONG, C3_BREAK, (11.3, 11.8, 10.9, 11.5)])


# ---- HTF swing low must be the last red candle of a break pattern (owner 2026-11-22) ----
def test_swing_low_ends_a_three_candle_break():
    df = _frame([(10.0, 10.2, 9.5, 9.6), (9.8, 10.0, 9.0, 9.4), (9.6, 10.0, 9.3, 9.9)])
    assert op.swing_break_index(df, 1) == 2


def test_swing_low_ends_a_four_candle_break():
    df = _frame([(10.0, 10.2, 9.5, 9.6), (9.8, 10.0, 9.0, 9.4), (9.5, 9.7, 9.3, 9.6), (9.6, 10.0, 9.4, 9.9)])
    assert op.swing_break_index(df, 1) == 3


def test_swing_low_ends_an_engulfing():
    df = _frame([(10.0, 10.2, 9.5, 9.6), (9.9, 10.0, 9.0, 9.1), (9.0, 10.5, 9.1, 10.1)])
    assert op.swing_break_index(df, 1) == 2


def test_five_candle_break_does_not_count_on_the_htf():
    # C3 and C4 do not close above C2 open (9.8), C5 closes above it: a 5-candle break, LTF only
    df = _frame([(10.0, 10.2, 9.5, 9.6), (9.8, 10.0, 9.0, 9.4), (9.5, 9.7, 9.3, 9.6),
                 (9.5, 9.7, 9.3, 9.6), (9.6, 10.2, 9.4, 10.0)])
    assert op.swing_break_index(df, 1) is None


def test_swing_without_a_break_is_not_a_swing_low_for_the_sweep():
    # swing candle 1 is a red candle but no break completes after it: no sweep can count
    bars = [(10.0, 10.2, 9.5, 9.6), (9.8, 10.0, 9.0, 9.4), (9.6, 9.9, 9.3, 9.5), (9.4, 9.6, 8.7, 9.0)]
    assert op.find_swing_low_sweep(_frame(bars), last=3) is None
