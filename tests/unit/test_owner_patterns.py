"""Owner pattern checks (phase 1). Plan: docs/IMPLEMENTATION_PLAN_2026-11-18_STRATEGY_SPEC.md."""
from __future__ import annotations

import pathlib
import sys

import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "quant"))

from quant.strategies import owner_patterns as op  # noqa: E402


def _frame(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


RED = (10.0, 10.2, 8.0, 8.2)          # red, body 1.8 of 2.2 range
GREEN_ENGULF = (8.0, 11.0, 7.9, 10.8)  # green, body 2.8 of 3.1, closes above RED high 10.2


def test_red_green_engulf_detected():
    assert op.is_red_green_engulf(_frame([RED, GREEN_ENGULF]), 1) is True


def test_weak_body_refused():
    weak = (8.0, 11.0, 7.0, 8.5)  # green, body 0.5 of 4.0 = 12.5%
    assert op.is_red_green_engulf(_frame([RED, weak]), 1) is False


def test_close_must_be_above_first_high():
    no_break = (8.0, 11.0, 7.9, 10.0)  # close 10.0 is below RED high 10.2
    assert op.is_red_green_engulf(_frame([RED, no_break]), 1) is False


def test_wrong_colours_refused():
    green_first = (8.2, 10.2, 8.0, 10.0)
    assert op.is_red_green_engulf(_frame([green_first, GREEN_ENGULF]), 1) is False


def test_index_bounds_are_safe():
    f = _frame([RED, GREEN_ENGULF])
    assert op.is_red_green_engulf(f, 0) is False
    assert op.is_red_green_engulf(f, 5) is False


def test_body_exactly_half_is_not_more_than_half():
    half = (10.0, 10.0, 8.0, 9.0)  # body 1.0 of range 2.0 = 50%, not MORE than 50%
    assert op.is_red_green_engulf(_frame([RED, half]), 1) is False


SELL_FAIL_PATTERN = [RED, GREEN_ENGULF]  # pattern at j=1: close 10.8 > low 8.0 of bar 0


def test_sell_fail_trigger_fires_on_newest_bar():
    trig = (10.5, 12.0, 10.3, 11.8)  # green, body 1.3 of 1.7 = 76%, closes above level 11.0
    res = op.find_sell_fail_trigger(_frame(SELL_FAIL_PATTERN + [trig]), last=2)
    assert res == {"pattern_index": 1, "level": 11.0, "trigger_index": 2}


def test_sell_fail_needs_close_above_level():
    no = (10.5, 11.5, 10.3, 10.9)  # closes 10.9, below level 11.0
    assert op.find_sell_fail_trigger(_frame(SELL_FAIL_PATTERN + [no]), last=2) is None


def test_sell_fail_trigger_needs_50_percent_body():
    weak = (10.5, 12.0, 10.3, 11.1)  # body 0.6 of 1.7 = 35%
    assert op.find_sell_fail_trigger(_frame(SELL_FAIL_PATTERN + [weak]), last=2) is None


def test_sell_fail_voided_by_earlier_break():
    early = (11.2, 11.5, 11.0, 11.4)  # closes above level 11.0 before the trigger bar
    trig = (10.5, 12.0, 10.3, 11.8)
    assert op.find_sell_fail_trigger(_frame(SELL_FAIL_PATTERN + [early, trig]), last=3) is None


def test_sell_fail_short_history_is_safe():
    assert op.find_sell_fail_trigger(_frame([RED]), last=0) is None


# ---- OCL Red-Red-Green: 3-candle or 4-candle break (levels from C2) ----
RRG_C1 = (10.0, 10.2, 9.0, 9.2)    # red: open 10.0, close 9.2 (body bottom 9.2)
RRG_C2 = (11.0, 11.1, 10.1, 10.5)  # red: open 11.0 (level), close 10.5 (body bottom 10.5)
C3_BREAK = (10.4, 11.6, 10.3, 11.2)  # green: closes 11.2 above C2 open 11.0 -> 3-candle break
C3_NO_BREAK = (10.4, 10.9, 10.3, 10.8)  # green: closes 10.8, below C2 open 11.0
C4_BREAK = (10.7, 11.5, 10.6, 11.4)  # closes 11.4 above C2 open 11.0 -> 4-candle break


def test_three_candle_break_uses_c2_open():
    res = op.find_ocl_rrg(_frame([RRG_C1, RRG_C2, C3_BREAK]), last=2)
    assert res == {"break": "3-candle", "key_level": 11.0, "conservative": 9.2, "trigger_index": 2}


def test_four_candle_break_uses_c3_close():
    res = op.find_ocl_rrg(_frame([RRG_C1, RRG_C2, C3_NO_BREAK, C4_BREAK]), last=3)
    assert res == {"break": "4-candle", "key_level": 10.8, "conservative": 9.2, "trigger_index": 3}


def test_first_break_decides_c3_break_blocks_c4_break():
    # C3 already broke C2 open, so a later C4 close above C2 open is ignored
    res = op.find_ocl_rrg(_frame([RRG_C1, RRG_C2, C3_BREAK, C4_BREAK]), last=3)
    assert res is None


def test_no_break_means_no_signal():
    c4_flat = (10.6, 10.9, 10.5, 10.7)  # closes 10.7, below C2 open 11.0
    assert op.find_ocl_rrg(_frame([RRG_C1, RRG_C2, C3_NO_BREAK, c4_flat]), last=3) is None


def test_ocl_rrg_needs_red_red_green_colours():
    c3_red = (10.6, 10.7, 10.0, 10.1)
    assert op.find_ocl_rrg(_frame([RRG_C1, RRG_C2, c3_red]), last=2) is None


def test_ocl_rrg_short_history_is_safe():
    assert op.find_ocl_rrg(_frame([RRG_C1, RRG_C2]), last=1) is None


def test_engulfing_is_still_two_candles():
    assert op.is_red_green_engulf(_frame([RED, GREEN_ENGULF]), 1) is True


# ---- 3-candle swing low and liquidity sweep ----
def test_swing_low_needs_both_neighbours_higher():
    f = _frame([(10, 10.5, 9.5, 10.0), (9.8, 10.0, 9.0, 9.4), (9.6, 9.9, 9.3, 9.7)])
    assert op.is_swing_low_3(f, 1) is True
    assert op.is_swing_low_3(_frame([(10, 10.5, 9.5, 10.0), (9.8, 10.0, 9.6, 9.9), (9.9, 10.2, 9.3, 9.8)]), 1) is False


def test_sweep_detected_when_later_low_breaks_swing_low():
    bars = [(10.0, 10.2, 9.5, 9.6), (9.8, 10.0, 9.0, 9.4), (9.6, 10.0, 9.3, 9.9), (9.5, 9.6, 8.7, 9.1)]  # swing 1 = red-candle break
    res = op.find_swing_low_sweep(_frame(bars), last=3)
    # any candle of the break pattern can be the swing (owner 2026-11-22): the newest is candle 2.
    # Its 3-candle pattern C1..C3 has lows 9.5, 9.0, 9.3, so the swing level is the lowest, 9.0.
    assert res == {"swing_index": 2, "swing_low": 9.0, "sweep_index": 3}


def test_no_sweep_when_no_low_breaks_swing_low():
    # swing candidates have lows 9.0 (candle 1) and 9.3 (candle 2); the last low 9.35 breaks neither
    bars = [(10.0, 10.2, 9.5, 9.6), (9.8, 10.0, 9.0, 9.4), (9.6, 10.0, 9.3, 9.9), (9.5, 9.8, 9.35, 9.6)]
    assert op.find_swing_low_sweep(_frame(bars), last=3) is None


def test_sweep_short_history_is_safe():
    assert op.find_swing_low_sweep(_frame([(10, 10.5, 9.5, 10.0)]), last=0) is None


# ---- Engulfing strategy sequence: MTF pattern -> touch -> HTF sweep -> LTF OCL -> touch ----
ENG_M_TOUCH = (10.8, 10.9, 9.5, 10.2)  # low 9.5 <= level 10.0 <= high 10.9
HTF_BARS = [(10.0, 10.2, 9.5, 9.6), (9.8, 10.0, 9.0, 9.4), (9.6, 10.0, 9.3, 9.9), (9.5, 9.6, 8.7, 9.1)]  # swing 1 = red-candle break (2026-11-22)
# A fail pattern with a confirm candle touches one bar later, so the HTF sweep needs one more bar.
# swing low 9.0 at index 1, first sweep (low 8.5) at index 5: the sweep comes after an MTF touch at index 4
HTF_SWEEP_LATE = [(10.0, 10.2, 9.5, 9.6), (9.8, 10.0, 9.0, 9.4), (9.6, 10.0, 9.3, 9.9), (9.6, 9.9, 9.3, 9.6),
                  (9.5, 9.8, 9.2, 9.6), (9.5, 9.6, 8.5, 9.0)]
LTF_FILL = (9.0, 9.5, 8.8, 9.2)
LTF_TOUCH = (11.3, 11.8, 10.9, 11.5)   # low 10.9 <= OCL key level 11.0 <= high 11.8


def _ltf_with_ocl(touch=LTF_TOUCH):
    return _frame([LTF_FILL, LTF_FILL, LTF_FILL, RRG_C1, RRG_C2, C3_BREAK, touch])


def test_engulf_sequence_full_path_triggers():
    res = op.engulf_sequence(_frame([RED, GREEN_ENGULF, ENG_M_TOUCH]), _frame(HTF_BARS), _ltf_with_ocl())
    assert res == {"pattern_time": 1, "touch_time": 2, "sweep_time": 3,
                   "ocl_time": 5, "ocl_start_time": 3, "conservative_level": 8.0, "trigger_time": 6, "entry": 11.0}
    # conservative = lowest body bottom of the two engulfing candles: min(10.0, 8.2, 8.0, 10.8) = 8.0 (rules 4.1)


def test_engulf_sequence_waits_when_no_mtf_touch():
    no_touch = (10.8, 10.9, 10.1, 10.5)    # low 10.1 is above the level 10.0
    assert op.engulf_sequence(_frame([RED, GREEN_ENGULF, no_touch]), _frame(HTF_BARS), _ltf_with_ocl()) is None


def test_engulf_sequence_needs_htf_sweep():
    no_sweep = HTF_BARS[:3] + [(9.5, 9.6, 9.35, 9.5)]  # last low 9.35 breaks no swing low (9.0 or 9.3)
    assert op.engulf_sequence(_frame([RED, GREEN_ENGULF, ENG_M_TOUCH]), _frame(no_sweep), _ltf_with_ocl()) is None


def test_engulf_sequence_waits_for_ltf_touch():
    no_ltf_touch = (11.3, 11.8, 11.1, 11.5)  # low 11.1 is above the OCL key level 11.0
    assert op.engulf_sequence(_frame([RED, GREEN_ENGULF, ENG_M_TOUCH]), _frame(HTF_BARS), _ltf_with_ocl(no_ltf_touch)) is None


def test_engulf_sequence_short_history_is_safe():
    assert op.engulf_sequence(_frame([RED]), _frame(HTF_BARS), _ltf_with_ocl()) is None


# ---- Sequence kinds (five strategies), freshness, stop ----
MTF_OCL_TOUCH = (11.3, 11.8, 10.9, 11.5)    # touches OCL key 11.0 and OCL top 11.6
# Fail kinds need >50 % bodies and a confirm candle (owner 2026-11-22). RRG_C2 has a body of exactly 50 %,
# so the fail-pattern tests use this C2: same open 11.0 (key level), body 1.0 of range 1.2 (83 %).
RRG_C2_STRONG = (11.0, 11.0, 9.8, 10.0)
FAIL_CONFIRM = (11.0, 12.0, 10.9, 11.9)     # green, body 82 %, closes above the pattern high 11.6
MTF_OCL_BOTTOM_TOUCH = (9.5, 9.6, 8.9, 9.2)  # touches OCL bottom 9.0
MTF_EG_HIGH_TOUCH = (10.8, 11.2, 10.5, 10.9)  # body-top level is 10.8 (green body top); this bar touches it
SELL_FAIL_TRIGGER = (10.9, 11.6, 10.8, 11.4)  # green, body 0.5 of range 0.8 (>50 %), closes above the pattern high 11.0


@pytest.mark.parametrize("kind, mtf_rows", [
    ("egpattern_open", [RED, GREEN_ENGULF, ENG_M_TOUCH]),
    ("egpattern_body", [RED, GREEN_ENGULF, SELL_FAIL_TRIGGER, MTF_EG_HIGH_TOUCH]),
    ("ocl_key", [RRG_C1, RRG_C2, C3_BREAK, MTF_OCL_TOUCH]),
    ("ocl_top", [RRG_C1, RRG_C2_STRONG, C3_BREAK, FAIL_CONFIRM, MTF_OCL_TOUCH]),
    ("ocl_bottom", [RRG_C1, RRG_C2_STRONG, C3_BREAK, FAIL_CONFIRM, MTF_OCL_BOTTOM_TOUCH]),
])
def test_each_sequence_kind_triggers_on_its_own_setup(kind, mtf_rows):
    htf = HTF_SWEEP_LATE if kind.startswith("ocl_") and len(mtf_rows) == 5 else HTF_BARS
    res = op.sequence(_frame(mtf_rows), _frame(htf), _ltf_with_ocl(), kind)
    assert res is not None
    assert res["trigger_time"] == 6


def test_ocl_top_level_is_highest_body_top_so_a_bar_below_it_does_not_touch():
    # top level = highest body top of C1..C3 = C3 close 11.2; this bar tops out at 10.9, so no touch
    no_top_touch = (10.3, 10.9, 10.3, 10.7)
    assert op.sequence(_frame([RRG_C1, RRG_C2_STRONG, C3_BREAK, FAIL_CONFIRM, no_top_touch]), _frame(HTF_BARS),
                       _ltf_with_ocl(), "ocl_top") is None


def test_engulfing_fail_level_is_highest_body_top_not_wick():
    # RED body top 10.0, GREEN body top 10.8 -> level 10.8 (the GREEN high 11.0 is a wick)
    bar_above_body_top_only_by_wick = (10.9, 11.0, 10.85, 10.85)  # low 10.85 > 10.8: no touch
    assert op.sequence(_frame([RED, GREEN_ENGULF, bar_above_body_top_only_by_wick]), _frame(HTF_BARS),
                       _ltf_with_ocl(), "egpattern_body") is None


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        op.sequence(_frame([RED, GREEN_ENGULF, ENG_M_TOUCH]), _frame(HTF_BARS), _ltf_with_ocl(), "nope")


def test_fresh_rule_refuses_a_trigger_that_is_not_on_the_last_bar():
    stale_ltf = _frame([LTF_FILL, LTF_FILL, LTF_FILL, RRG_C1, RRG_C2, C3_BREAK, LTF_TOUCH, LTF_FILL])
    args = (_frame([RED, GREEN_ENGULF, ENG_M_TOUCH]), _frame(HTF_BARS), stale_ltf, "egpattern_open")
    assert op.sequence(*args, fresh=False) is not None
    assert op.sequence(*args, fresh=True) is None


def test_fresh_rule_accepts_a_trigger_on_the_last_bar():
    args = (_frame([RED, GREEN_ENGULF, ENG_M_TOUCH]), _frame(HTF_BARS), _ltf_with_ocl(), "egpattern_open")
    assert op.sequence(*args, fresh=True)["trigger_time"] == 6


def test_sequence_signal_stop_is_low_of_ocl_c1_to_c3():
    # ATR(14) needs history, so put 20 flat bars before the OCL
    ltf = _frame([LTF_FILL] * 20 + [RRG_C1, RRG_C2, C3_BREAK, LTF_TOUCH])
    sig = op.sequence_signal("egpattern_open", _frame([RED, GREEN_ENGULF, ENG_M_TOUCH]), _frame(HTF_BARS),
                             ltf, name="engulfing")
    # OCL C1..C3 = RRG_C1, RRG_C2, C3_BREAK; lowest low 9.0; entry 11.0 -> stop distance 2.0, target 6.0
    assert sig["entry"] == 11.0
    assert sig["sl_dist"] == pytest.approx(2.0)
    assert sig["tp_dist"] == pytest.approx(6.0)
    assert sig["strategy"] == "engulfing"


def test_ocl_bottom_needs_no_htf_sweep():
    # HTF with no swing low, so no sweep exists. The bottom strategy still triggers; the key strategy does not.
    no_sweep_htf = _frame([(10, 10.5, 9.5, 10.0), (9.8, 10, 9.6, 9.9), (9.9, 10.2, 9.7, 10.1), (10, 10.3, 9.9, 10.2)])
    mtf = _frame([RRG_C1, RRG_C2_STRONG, C3_BREAK, FAIL_CONFIRM, MTF_OCL_BOTTOM_TOUCH])
    assert op.sequence(mtf, no_sweep_htf, _ltf_with_ocl(), "ocl_bottom") is not None
    assert op.sequence(mtf, no_sweep_htf, _ltf_with_ocl(), "ocl_key") is None


def test_ocl_bottom_level_is_lowest_body_bottom_not_wick():
    # body bottoms: C1 9.2, C2 10.5, C3 10.4 -> level 9.2 (C1 wick low 9.0 is not used)
    mtf = _frame([RRG_C1, RRG_C2_STRONG, C3_BREAK, FAIL_CONFIRM, (9.1, 9.3, 9.0, 9.15)])  # low 9.0 <= 9.2 <= high 9.3
    assert op.sequence(mtf, _frame(HTF_BARS), _ltf_with_ocl(), "ocl_bottom") is not None
    no_touch_body = _frame([RRG_C1, RRG_C2_STRONG, C3_BREAK, FAIL_CONFIRM, (9.4, 9.8, 9.3, 9.5)])  # low 9.3 > 9.2
    assert op.sequence(no_touch_body, _frame(HTF_BARS), _ltf_with_ocl(), "ocl_bottom") is None


def test_ocl_top_records_conservative_level_c2_open():
    # top has two key levels: aggressive (body top, touched) and conservative (C2 open 11.0, recorded)
    res = op.sequence(_frame([RRG_C1, RRG_C2_STRONG, C3_BREAK, FAIL_CONFIRM, MTF_OCL_TOUCH]), _frame(HTF_SWEEP_LATE),
                      _ltf_with_ocl(), "ocl_top")
    assert res["conservative_level"] == 11.0


def test_bottom_has_no_second_level():
    res = op.sequence(_frame([RRG_C1, RRG_C2_STRONG, C3_BREAK, FAIL_CONFIRM, MTF_OCL_BOTTOM_TOUCH]), _frame(HTF_BARS),
                      _ltf_with_ocl(), "ocl_bottom")
    assert res["conservative_level"] is None


def test_engulfing_fail_level_is_highest_body_top_and_conservative_is_lowest_body_bottom():
    mtf = _frame([RED, GREEN_ENGULF, (10.8, 10.9, 10.5, 10.6)])  # a later bar is needed (the setup must be complete)
    kind_level = op._newest_setup(mtf, "egpattern_body")
    assert kind_level is not None and kind_level[1] == 10.8        # highest body top: max(10.0, 8.2, 8.0, 10.8)
    assert op._mtf_conservative(mtf, "egpattern_body", kind_level[0]) == 8.0


def test_ocl_top_level_uses_pattern_c1_to_c3_not_the_four_candle_trigger():
    # C1 red, C2 red, C3 green (does not break C2 open), C4 green closes above C2 open: 4-candle break.
    c1 = (10.0, 10.2, 9.0, 9.1)     # body top 10.0
    c2 = (9.6, 9.7, 8.9, 9.0)       # body top 9.6, body bottom 9.0
    c3 = (9.0, 9.6, 8.9, 9.5)       # green body 71 %, closes 9.5 below C2 open 9.6: no 3-candle break
    c4 = (9.1, 10.5, 9.0, 9.9)      # trigger: closes 9.9 above C2 open 9.6; body top 9.9 (must NOT count)
    c5 = (9.9, 10.0, 9.8, 9.95)     # a later bar, so the completed setup is found
    mtf = _frame([c1, c2, c3, c4, c5])
    ocl = op.find_ocl_rrg(mtf, 3)
    assert ocl is not None and ocl["break"] == "4-candle"
    _, level = op._newest_setup(mtf, "ocl_top")
    assert level == 10.0            # highest body top of C1..C3 only
    assert op._mtf_conservative(mtf, "ocl_top", 3) == 9.0   # open of C3 (candle before the 4-candle trigger)


# ---- sell-fail confirmation for engulfing_fail (owner 2026-11-20) ----
def _eg_fail_seq(trigger, touch=MTF_EG_HIGH_TOUCH):
    mtf = _frame([RED, GREEN_ENGULF, trigger, touch])
    return op.sequence(mtf, _frame(HTF_BARS), _ltf_with_ocl(), kind="egpattern_body")


def test_engulfing_fail_needs_the_sell_fail_trigger_candle():
    assert _eg_fail_seq((10.9, 10.95, 10.6, 10.7)) is None          # no close above the high 11.0


def test_sell_fail_trigger_must_be_green_with_body_over_half():
    red_trigger = (11.4, 11.6, 10.8, 10.9)                           # closes above 11.0 but is red
    small_body = (10.9, 11.6, 10.8, 11.05)                           # green but body 0.15 of range 0.8
    assert _eg_fail_seq(red_trigger) is None
    assert _eg_fail_seq(small_body) is None


def test_an_earlier_close_above_the_level_voids_the_sell_fail_pattern():
    early = (10.9, 11.5, 10.8, 11.2)                                  # first close above 11.0, not a trigger
    assert _eg_fail_seq(early) is None
