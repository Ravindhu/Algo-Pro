"""Tests for the cycle replay (rules doc section 11). Pure logic, phase 1 of docs/IMPLEMENTATION_PLAN_2026-11-19_CYCLE.md."""
from __future__ import annotations

import pathlib
import sys

import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.strategies import cycle  # noqa: E402

def _frame(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


RED = (10.0, 10.2, 8.0, 8.2)
GREEN_ENGULF = (8.0, 11.0, 7.9, 10.8)          # closes above RED high -> egpattern; body top 10.8
ENG_M_TOUCH = (10.8, 10.9, 9.5, 10.2)          # touches MTF level 10.0 (open of RED)
MTF = [RED, GREEN_ENGULF, ENG_M_TOUCH]           # MTF low = 7.9

# HTF: swing low at 1, sweep at 3, then HTF OCL (C1..C3 at 4..6), key level 11.0, touched at 7
HTF = [(10, 10.5, 9.5, 10.0), (9.8, 10.0, 9.0, 9.4), (9.6, 9.9, 9.3, 9.7), (9.5, 9.6, 8.7, 9.1),
       (10.0, 10.2, 9.0, 9.2), (11.0, 11.1, 10.1, 10.5), (10.4, 11.6, 10.3, 11.2), (11.3, 11.8, 10.9, 11.5)]
# HTF OCL C1..C3 = indices 4..6 -> low 9.0 (HTF stop)
HTF_LOW = 9.0

LTF_FILL = (9.0, 9.5, 8.8, 9.2)
RRG_C1 = (10.0, 10.2, 9.0, 9.2)
RRG_C2 = (11.0, 11.1, 10.1, 10.5)
C3_BREAK = (10.4, 11.6, 10.3, 11.2)            # LTF OCL: key (aggressive) 11.0, conservative 9.2, low 9.0
AGG_TOUCH = (11.3, 11.8, 10.9, 11.5)           # touches 11.0
CONS_TOUCH = (9.5, 9.6, 9.0, 9.4)              # touches 9.2, close 9.4 above OCL low 9.0


def _ltf(*extra):
    return _frame([LTF_FILL] * 8 + [RRG_C1, RRG_C2, C3_BREAK] + list(extra))


def test_full_cycle_aggressive_then_conservative_trails():
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _ltf(AGG_TOUCH, CONS_TOUCH), target_r=3.0)
    assert res.status == "open"
    assert [e.kind for e in res.entries] == ["aggressive", "conservative"]
    assert [e.price for e in res.entries] == [11.0, 9.2]
    assert res.avg_entry == pytest.approx(10.1)
    assert res.htf_low == HTF_LOW
    assert res.mode == "trail" and res.target is None


def test_aggressive_only_books_at_backtested_target():
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _ltf(AGG_TOUCH), target_r=3.0)
    assert [e.kind for e in res.entries] == ["aggressive"]
    # R = avg 11.0 - HTF low 9.0 = 2.0; target = 11.0 + 3 x 2.0 = 17.0
    assert res.mode == "target" and res.target == pytest.approx(17.0)


def test_target_pending_until_backtest_sets_n():
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _ltf(AGG_TOUCH))
    assert res.mode == "target_pending" and res.target is None


def test_htf_close_below_hard_stop_invalidates_the_cycle():
    below_htf_low = (9.5, 9.6, 8.5, 8.7)  # closes 8.7 < HTF low 9.0
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _ltf(AGG_TOUCH, below_htf_low), target_r=3.0)
    assert res.status == "htf_invalidated"
    assert len(res.entries) == 1  # entry before the invalidation is kept in the result


def test_mtf_close_below_setup_low_ends_the_whole_cycle():
    mtf = _frame(MTF + [(9.0, 9.1, 7.0, 7.5)])  # closes 7.5 < MTF low 7.9
    res = cycle.run_cycle("egpattern_open", mtf, _frame(HTF), _ltf(AGG_TOUCH), target_r=3.0)
    assert res.status == "mtf_invalidated"
    assert res.entries == []


def test_no_entry_while_only_waiting_for_ltf_ocl():
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _frame([LTF_FILL] * 12))
    assert res.status == "waiting" and res.entries == [] and res.htf_low == HTF_LOW


def test_no_cycle_without_mtf_touch():
    no_touch = (10.8, 10.9, 10.5, 10.9)  # low 10.5 > level 10.0
    assert cycle.run_cycle("egpattern_open", _frame([RED, GREEN_ENGULF, no_touch]),
                           _frame(HTF), _ltf(AGG_TOUCH)) is None


def test_no_cycle_without_htf_sweep():
    flat_htf = _frame([(10, 10.5, 9.5, 10.0)] * 8)
    assert cycle.run_cycle("egpattern_open", _frame(MTF), flat_htf, _ltf(AGG_TOUCH)) is None


HTF_DEEP = [HTF[0], HTF[1], HTF[2], HTF[3], (10.0, 10.2, 8.5, 9.2)] + HTF[5:]  # HTF OCL C1 low 8.5 -> HTF low 8.5


def test_close_below_ltf_ocl_low_arms_a_new_ocl_that_adds_an_entry():
    below_low = (9.0, 9.1, 8.8, 8.9)              # red, closes 8.9 < OCL low 9.0 (stop-out level), above HTF low 8.5
    ocl2 = [(8.9, 8.95, 8.6, 8.6), (8.8, 8.85, 8.55, 8.6), (8.6, 8.95, 8.55, 8.9)]  # RRG, key = C2 open 8.8 < 9.0, closes above HTF low 8.5
    touch2 = (8.9, 9.0, 8.7, 8.85)                # touches key 8.8
    ltf = _ltf(AGG_TOUCH, below_low, *ocl2, touch2)
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF_DEEP), ltf, target_r=3.0)
    assert res.htf_low == 8.5
    assert [e.price for e in res.entries] == [11.0, 8.8]
    assert res.avg_entry == pytest.approx(9.9)
    assert res.status == "open"


def test_new_ocl_must_form_below_the_previous_stop_out_level():
    below_low = (9.0, 9.1, 8.8, 8.9)
    ocl_above = [(9.3, 9.4, 9.0, 9.1), (9.4, 9.5, 9.1, 9.2), (9.1, 9.9, 9.0, 9.7)]  # key 9.4 is not below 9.0
    touch_above = (9.6, 9.8, 9.2, 9.5)
    ltf = _ltf(AGG_TOUCH, below_low, *ocl_above, touch_above)
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF_DEEP), ltf, target_r=3.0)
    assert [e.price for e in res.entries] == [11.0]
