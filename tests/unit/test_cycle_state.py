"""Cycle state for the bot wiring: htf_key_time (cycle id), ltf_paused (stop-out waiting for a new LTF OCL),
and SequenceStrategy.cycle_for. Same bar data as tests/unit/test_cycle.py. Flow stage: Strategy bot cycle."""
from __future__ import annotations

import pathlib
import sys
from datetime import datetime

import pandas as pd

EQUITY = 1_000_000.0  # paper capital, same as the bot's read_funds
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.engines.alpha.strategies import cycle  # noqa: E402
from quant.engines.alpha.strategies.engulf_sequence_strategy import EngulfSequenceStrategy  # noqa: E402

RED = (10.0, 10.2, 8.0, 8.2)
GREEN_ENGULF = (8.0, 11.0, 7.9, 10.8)
ENG_M_TOUCH = (10.8, 10.9, 9.5, 10.2)
MTF = [RED, GREEN_ENGULF, ENG_M_TOUCH]
HTF = [(10.0, 10.2, 9.5, 9.6), (9.8, 10.0, 9.0, 9.4), (9.6, 10.0, 9.3, 9.9), (9.5, 9.6, 8.7, 9.1),
       (10.0, 10.2, 9.0, 9.2), (11.0, 11.1, 10.1, 10.5), (10.4, 11.6, 10.3, 11.2), (11.3, 11.8, 10.9, 11.5)]
LTF_FILL = (9.0, 9.5, 8.8, 9.2)
RRG_C1 = (10.0, 10.2, 9.0, 9.2)
RRG_C2 = (11.0, 11.1, 10.1, 10.5)
C3_BREAK = (10.4, 11.6, 10.3, 11.2)
AGG_TOUCH = (11.3, 11.8, 10.9, 11.5)
STOP_OUT = (9.4, 9.5, 8.6, 8.8)  # closes 8.8, below the LTF OCL low 9.0


def _frame(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def _ltf(*extra):
    return _frame([LTF_FILL] * 8 + [RRG_C1, RRG_C2, C3_BREAK] + list(extra))


class _FlatAtr(dict):
    """Test helper: ATR(14) = 1.0 on every LTF bar (owner 2026-11-20 sizing stop)."""
    def get(self, key, default=None):
        return 1.0


FLAT_ATR = _FlatAtr()

def test_result_carries_htf_cycle_id():
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _ltf(AGG_TOUCH), target_r=3.0, equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert res.htf_key_time == 7  # the HTF bar that touched the key (index 7)
    assert res.ltf_paused is False


def test_stop_out_without_new_ocl_is_paused():
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _ltf(AGG_TOUCH, STOP_OUT), equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert [e.kind for e in res.entries] == ["aggressive"]
    assert res.ltf_paused is True
    assert res.status == "open"


def test_new_ltf_ocl_after_stop_out_clears_pause_only_after_it_forms():
    # After the stop-out, a new LTF OCL forms below 9.0: the pause ends when it forms.
    NEW_C1 = (8.8, 8.9, 8.0, 8.1)     # red
    NEW_C2 = (8.9, 8.95, 8.1, 8.2)    # red, open 8.9 = new key level (below 9.0)
    NEW_C3 = (8.4, 9.6, 8.3, 9.5)     # green, closes above C2 open
    res = cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _ltf(AGG_TOUCH, STOP_OUT, NEW_C1, NEW_C2, NEW_C3), equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR)
    assert res.ltf_paused is False


class _FakeFeed:
    def __init__(self, frames):
        self._frames = frames

    def closed_frames(self, symbol, now):
        return self._frames


def test_cycle_for_returns_all_cycles_newest_time_and_atr():
    frames = {"mtf": _frame(MTF), "htf": _frame(HTF),
              "ltf": _ltf(*([LTF_FILL] * 20), AGG_TOUCH)}
    strat = EngulfSequenceStrategy(_FakeFeed(frames))
    view = strat.cycle_for("INFY", datetime(2026, 11, 17, 10, 15))
    assert view is not None
    assert view.results and view.results[-1].htf_key_time == 7
    assert view.newest_ltf_time == frames["ltf"].index[-1]
    assert view.atr is not None and view.atr > 0


def test_cycle_for_none_when_feed_not_ready():
    strat = EngulfSequenceStrategy(_FakeFeed(None))
    assert strat.cycle_for("INFY", datetime(2026, 11, 17, 10, 15)) is None
    assert strat.is_cycle is True
