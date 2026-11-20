"""Tests for the cycle replay (rules doc section 11). Pure logic, phase 1 of docs/IMPLEMENTATION_PLAN_2026-11-19_CYCLE.md."""
from __future__ import annotations

import pathlib
import sys

import pandas as pd
import pytest

class _FlatAtr(dict):
    """Test helper: ATR(14) = 1.0 on every LTF bar. The sizing stop is then max(1.0, (price - HTF L) + 0.05 %)."""
    def get(self, key, default=None):
        return 1.0


FLAT_ATR = _FlatAtr()
EQUITY = 1_000_000.0  # paper capital, same as the bot's read_funds
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
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _ltf(AGG_TOUCH, CONS_TOUCH), target_r=3.0, equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert res.status == "open"
    assert [e.kind for e in res.entries] == ["aggressive", "conservative"]
    assert [e.price for e in res.entries] == [11.0, 9.2]
    # ladder sizes (plan P4) with the sizing stop (owner 2026-11-20), ATR = 1.0 on every bar:
    #   probe 0.10 % at 11.0: stop = 2.0 + 0.05 % x 11 = 2.0055 -> 1,000 / 2.0055 = 498 sh
    #   conservative 0.15 % at 9.2: stop = max(ATR floor 1.0, 0.2046) = 1.0 -> 1,500 sh
    assert res.avg_entry == pytest.approx((498 * 11.0 + 1500 * 9.2) / 1998)  # quantity-weighted, 9.6486
    assert res.htf_low == HTF_LOW
    assert res.mode == "trail" and res.target is None


def test_aggressive_only_books_at_backtested_target():
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _ltf(AGG_TOUCH), target_r=3.0, equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert [e.kind for e in res.entries] == ["aggressive"]
    # R = avg 11.0 - HTF low 9.0 = 2.0; target = 11.0 + 3 x 2.0 = 17.0
    assert res.mode == "target" and res.target == pytest.approx(17.0)


def test_target_pending_until_backtest_sets_n():
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _ltf(AGG_TOUCH), equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert res.mode == "target_pending" and res.target is None


HTF_FILL = (10.0, 10.2, 9.9, 10.1)
HTF_INVAL = HTF + [HTF_FILL] * 4 + [(9.5, 9.6, 8.5, 8.7)]  # HTF candle closes 8.7 < HTF low 9.0, at index 12


def test_htf_close_below_hard_stop_ends_the_cycle_after_its_entry():
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF_INVAL), _ltf(AGG_TOUCH, CONS_TOUCH), target_r=3.0, equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert res.status == "htf_invalidated"
    assert [e.kind for e in res.entries] == ["aggressive"]  # conservative touch came after the HTF invalidation


def test_htf_invalidated_before_any_entry_takes_no_entry():
    early = HTF + [(9.5, 9.6, 8.5, 8.7)]  # HTF close 8.7 at index 8, before the LTF touch
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(early), _ltf(AGG_TOUCH), target_r=3.0, equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert res.status == "htf_invalidated" and res.entries == []


def test_mtf_close_below_setup_low_ends_the_whole_cycle():
    mtf = _frame(MTF + [(9.0, 9.1, 7.0, 7.5)])  # closes 7.5 < MTF low 7.9
    res = cycle.run_cycle("egpattern_open", mtf, _frame(HTF), _ltf(AGG_TOUCH), target_r=3.0, equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert res.status == "mtf_invalidated"
    assert res.entries == []


def test_no_entry_while_only_waiting_for_ltf_ocl():
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _frame([LTF_FILL] * 12), equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert res.status == "waiting" and res.entries == [] and res.htf_low == HTF_LOW


def test_no_cycle_without_mtf_touch():
    no_touch = (10.8, 10.9, 10.5, 10.9)  # low 10.5 > level 10.0
    assert cycle.run_cycle("egpattern_open", _frame([RED, GREEN_ENGULF, no_touch]),
                           _frame(HTF), _ltf(AGG_TOUCH), equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR) is None


def test_no_cycle_without_htf_sweep():
    flat_htf = _frame([(10, 10.5, 9.5, 10.0)] * 8)
    assert cycle.run_cycle("egpattern_open", _frame(MTF), flat_htf, _ltf(AGG_TOUCH), equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR) is None


HTF_DEEP = [HTF[0], HTF[1], HTF[2], HTF[3], (10.0, 10.2, 8.5, 9.2)] + HTF[5:]  # HTF OCL C1 low 8.5 -> HTF low 8.5


def test_close_below_ltf_ocl_low_arms_a_new_ocl_that_adds_an_entry():
    below_low = (9.0, 9.1, 8.8, 8.9)              # red, closes 8.9 < OCL low 9.0 (stop-out level), above HTF low 8.5
    ocl2 = [(8.9, 8.95, 8.6, 8.6), (8.8, 8.85, 8.55, 8.6), (8.6, 8.95, 8.55, 8.9)]  # RRG, key = C2 open 8.8 < 9.0, closes above HTF low 8.5
    touch2 = (8.9, 9.0, 8.7, 8.85)                # touches key 8.8
    ltf = _ltf(AGG_TOUCH, below_low, *ocl2, touch2)
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF_DEEP), ltf, target_r=3.0, equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert res.htf_low == 8.5
    assert [e.price for e in res.entries] == [11.0, 8.8]
    from quant.strategies.risk_ladder import shares_for
    from quant.strategies.risk_ladder import sizing_stop
    q1 = shares_for(EQUITY, 0.0010, 11.0, 8.5, stop=sizing_stop(11.0, 8.5, 1.0))  # probe, sizing stop
    q2 = shares_for(EQUITY, 0.0010, 8.8, 8.5, stop=sizing_stop(8.8, 8.5, 1.0))    # new OCL below the stop-out: aggressive 0.10 % (its conservative +0.15 % is separate), nothing carried
    assert res.avg_entry == pytest.approx((q1 * 11.0 + q2 * 8.8) / (q1 + q2))
    assert res.status == "open"


def test_new_ocl_must_form_below_the_previous_stop_out_level():
    below_low = (9.0, 9.1, 8.8, 8.9)
    ocl_above = [(9.3, 9.4, 9.0, 9.1), (9.4, 9.5, 9.1, 9.2), (9.1, 9.9, 9.0, 9.7)]  # key 9.4 is not below 9.0
    touch_above = (9.6, 9.8, 9.2, 9.5)
    ltf = _ltf(AGG_TOUCH, below_low, *ocl_above, touch_above)
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF_DEEP), ltf, target_r=3.0, equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert [e.price for e in res.entries] == [11.0]


# ---- next HTF cycle: HTF invalidated at 12, new HTF OCL below 9.0 forms at 13..15, key touched at 16 ----
HTF_RECYCLE = HTF_INVAL + [
    (8.9, 8.95, 8.6, 8.6),   # 13 red
    (8.8, 8.85, 8.55, 8.6),  # 14 red, C2 open 8.8 = key
    (8.6, 9.0, 8.55, 8.9),   # 15 green, closes above 8.8 -> 3-candle break, key 8.8 (< 9.0)
    (8.9, 9.0, 8.7, 8.85),   # 16 touches key 8.8
]
LTF_RECYCLE = [LTF_FILL] * 8 + [RRG_C1, RRG_C2, C3_BREAK, AGG_TOUCH] + [LTF_FILL] * 5 + [
    (9.0, 9.1, 8.5, 8.6),    # 17 red
    (8.8, 8.9, 8.3, 8.4),    # 18 red, open 8.8 = key
    (8.4, 9.0, 8.3, 8.9),    # 19 green, closes above 8.8
    (8.9, 9.0, 8.6, 8.85),   # 20 touches 8.8
]


def test_next_htf_cycle_runs_after_htf_invalidation():
    results = cycle.run_cycles("egpattern_open", _frame(MTF), _frame(HTF_RECYCLE), _frame(LTF_RECYCLE), target_r=3.0, equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert [r.status for r in results] == ["htf_invalidated", "open"]
    assert results[0].htf_low == 9.0 and [e.price for e in results[0].entries] == [11.0]
    assert results[1].htf_low == pytest.approx(8.55)
    assert [e.price for e in results[1].entries] == [8.8]


def test_no_new_htf_ocl_below_the_invalidated_price_means_no_second_cycle():
    results = cycle.run_cycles("egpattern_open", _frame(MTF), _frame(HTF_INVAL), _ltf(AGG_TOUCH), target_r=3.0, equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert len(results) == 1


def test_fresh_entries_keeps_only_the_newest_bar():
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _ltf(AGG_TOUCH, CONS_TOUCH), target_r=3.0, equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert [e.time for e in cycle.fresh_entries(res, newest_ltf_time=12)] == [12]
    assert cycle.fresh_entries(res, newest_ltf_time=99) == []
    assert cycle.fresh_entries(None, newest_ltf_time=12) == []


def test_no_atr_on_the_touch_bar_means_no_replayed_order():
    """Owner 2026-11-20 sizing stop: a touch on a bar with no ATR gets no order, the same as the bot."""
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _ltf(AGG_TOUCH, CONS_TOUCH),
                          target_r=3.0, equity=EQUITY, loss_ceiling=0.01, atr_by_time={})
    assert res is not None and res.entries == []


def test_tree_decisions_are_empty_without_an_allocation():
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _ltf(AGG_TOUCH, CONS_TOUCH),
                          target_r=3.0, equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert res.tree_decisions == []


def test_tree_decisions_budget_the_full_cycle_at_the_owner_numbers():
    """4 % allocation: the HTF OCL gets 0.8 %, its LTF OCL 0.16 %, and the leaf splits 0.096 % / 0.064 %."""
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _ltf(AGG_TOUCH, CONS_TOUCH),
                          target_r=3.0, equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR,
                          symbol_allocation_pct=4.0)
    d = res.tree_decisions
    htf_ocl = [x for x in d if x.event[0] == "htf_ocl"]
    ltf_ocl = [x for x in d if x.event[0] == "ltf_ocl"]
    agg = [x for x in d if x.event[0] == "ltf_agg_touch"]
    cons = [x for x in d if x.event[0] == "ltf_cons_touch"]
    assert htf_ocl and htf_ocl[0].fraction == pytest.approx(0.008)
    assert ltf_ocl and ltf_ocl[0].fraction == pytest.approx(0.0016)
    assert agg and agg[0].fraction == pytest.approx(0.00096)
    assert cons and cons[0].fraction == pytest.approx(0.00064)
    assert agg[0].refused is None and cons[0].refused is None
