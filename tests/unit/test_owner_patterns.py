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


# ---- OCL Red-Red-Green (last 3 bars only) ----
RRG_C1 = (10.0, 10.2, 9.0, 9.2)    # red: open 10.0, close 9.2 (body bottom 9.2)
RRG_C2 = (11.0, 11.1, 10.1, 10.5)  # red: closes 10.5 > C1 open 10.0 (aggressive 1 break)
RRG_C3 = (10.4, 11.6, 10.3, 11.2)  # green: closes 11.2 > C2 close 10.5 (aggressive 2 break)


def test_ocl_rrg_detected_with_levels():
    res = op.find_ocl_rrg(_frame([RRG_C1, RRG_C2, RRG_C3]), last=2)
    assert res == {"aggressive_1": 10.0, "aggressive_2": 10.5,
                   "conservative": 9.2, "trigger_index": 2}


def test_ocl_rrg_uses_last_three_bars_only():
    older = (20.0, 21.0, 19.0, 19.5)  # an older bar with a lower body: must not count
    res = op.find_ocl_rrg(_frame([older, RRG_C1, RRG_C2, RRG_C3]), last=3)
    assert res["conservative"] == 9.2


def test_ocl_rrg_needs_c2_to_close_above_c1_open():
    c2_low = (11.0, 11.1, 9.5, 9.8)  # closes 9.8, below C1 open 10.0
    assert op.find_ocl_rrg(_frame([RRG_C1, c2_low, RRG_C3]), last=2) is None


def test_ocl_rrg_needs_c3_to_close_above_c2_close():
    c3_low = (10.4, 10.9, 10.3, 10.4)  # green but closes 10.4, below C2 close 10.5
    assert op.find_ocl_rrg(_frame([RRG_C1, RRG_C2, c3_low]), last=2) is None


def test_ocl_rrg_needs_red_red_green_colours():
    c3_red = (11.2, 11.3, 10.3, 10.6)  # red third bar
    assert op.find_ocl_rrg(_frame([RRG_C1, RRG_C2, c3_red]), last=2) is None


def test_ocl_rrg_short_history_is_safe():
    assert op.find_ocl_rrg(_frame([RRG_C1, RRG_C2]), last=1) is None
