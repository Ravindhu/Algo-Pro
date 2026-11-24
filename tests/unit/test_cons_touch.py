"""Conservative-touch detection (owner risk tree, 2026-11-20).

A conservative touch arms the next slot group: the price must trade back into the setup's conservative level
after the key touch and before invalidation. These tests use small synthetic frames with known levels.
"""
from __future__ import annotations

import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.engines.alpha.strategies import cycle  # noqa: E402


def _bars(rows):
    idx = pd.date_range("2026-11-16 09:15", periods=len(rows), freq="15min")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


# ---- HTF conservative touch -------------------------------------------------------------------------------------
# bar 0 = key touch bar (index hp=0). Conservative level of the HTF OCL = 10.0.
HTF_ROWS = [
    (10.5, 10.6, 10.4, 10.5),   # 0 key bar
    (10.4, 10.5, 9.5, 9.8),     # 1 range 9.5-10.5 covers 10.0 -> the first covering bar AFTER the key bar
    (9.8, 9.9, 9.2, 9.4),       # 2 below
    (9.4, 10.1, 9.3, 10.05),    # 3 covers 10.0 again
]


def test_htf_cons_touch_is_the_first_covering_bar_after_the_key_bar():
    htf = _bars(HTF_ROWS)
    got = cycle._htf_cons_touch_time(htf, 0, {"conservative": 10.0}, inval_time=None)
    assert got == htf.index[1]


def test_htf_cons_touch_ignores_the_key_bar_itself():
    # Only the key bar covers the level; nothing after it does -> no touch yet.
    htf = _bars([(10.5, 10.6, 10.4, 10.5), (10.8, 10.9, 10.7, 10.85), (10.7, 10.8, 10.6, 10.75)])
    assert cycle._htf_cons_touch_time(htf, 0, {"conservative": 10.0}, inval_time=None) is None


def test_htf_cons_touch_before_invalidation_only():
    htf = _bars(HTF_ROWS)
    inval = htf.index[1]  # invalidated on bar 1: the touch on bar 1 is not allowed (t >= inval stops the scan)
    assert cycle._htf_cons_touch_time(htf, 0, {"conservative": 10.0}, inval_time=inval) is None


def test_htf_cons_touch_when_level_is_never_traded_is_none():
    htf = _bars(HTF_ROWS)
    assert cycle._htf_cons_touch_time(htf, 0, {"conservative": 50.0}, inval_time=None) is None


# ---- MTF conservative touch -------------------------------------------------------------------------------------
# The setup lookup is stubbed so the test isolates the detection rule (setup finding is tested elsewhere).
# Level 10.0. Bar 1 is the MTF touch bar: its range covers 10.0 but it must NOT count (detection starts after it).
MTF_ROWS = [
    (10.0, 10.2, 9.9, 10.1),    # 0 setup bar
    (10.2, 10.3, 9.8, 10.0),    # 1 MTF touch bar, range 9.8-10.3 covers 10.0 (ignored: same bar as touch)
    (10.1, 10.6, 10.1, 10.5),   # 2 range 10.1-10.6, above the level -> no
    (9.2, 9.6, 8.9, 9.0),       # 3 range 8.9-9.6, below the level -> no
    (9.5, 10.4, 9.4, 10.3),     # 4 range 9.4-10.4 covers 10.0 -> first conservative touch
    (10.3, 10.5, 10.1, 10.4),   # 5 covers again
]


def _stub_setup(monkeypatch, level, end_index):
    monkeypatch.setattr(cycle, "_newest_setup", lambda mtf, kind: (mtf.index[end_index], None))
    monkeypatch.setattr(cycle, "_mtf_conservative", lambda mtf, kind, end: level)


def test_mtf_cons_touch_is_first_covering_bar_after_the_mtf_touch(monkeypatch):
    mtf = _bars(MTF_ROWS)
    _stub_setup(monkeypatch, 10.0, 0)
    got = cycle._mtf_cons_touch_time("egpattern_open", mtf, mtf.index[1], mtf_inval_time=None)
    assert got == mtf.index[4]


def test_mtf_touch_bar_itself_does_not_count_as_conservative_touch(monkeypatch):
    mtf = _bars(MTF_ROWS[:2])  # only the setup bar and the touch bar (which covers 10.0)
    _stub_setup(monkeypatch, 10.0, 0)
    assert cycle._mtf_cons_touch_time("egpattern_open", mtf, mtf.index[1], mtf_inval_time=None) is None


def test_mtf_cons_touch_not_after_mtf_invalidation(monkeypatch):
    mtf = _bars(MTF_ROWS)
    _stub_setup(monkeypatch, 10.0, 0)
    # invalidated on bar 4: the covering bar 4 is at/after invalidation, so no conservative touch
    assert cycle._mtf_cons_touch_time("egpattern_open", mtf, mtf.index[1], mtf_inval_time=mtf.index[4]) is None


def test_mtf_cons_touch_none_when_setup_missing(monkeypatch):
    mtf = _bars(MTF_ROWS)
    monkeypatch.setattr(cycle, "_newest_setup", lambda mtf, kind: None)
    assert cycle._mtf_cons_touch_time("egpattern_open", mtf, mtf.index[1], mtf_inval_time=None) is None


# ---- wiring: a detected conservative touch reaches the tree decisions -----------------------------------------
def test_htf_cons_touch_event_is_passed_into_the_planner(monkeypatch):
    """Wiring check: when detection returns a time, run_cycles hands an htf_cons_touch event to the planner.
    Detection itself is covered above; this only checks the event is not dropped."""
    seen = []
    real_plan = cycle.tree_plan

    def spy(timed, pct, share):
        seen.append([e[0] for e in timed])
        return real_plan(timed, pct, share)

    monkeypatch.setattr(cycle, "tree_plan", spy)
    # The fixture's key bar is the last HTF bar, so no later bar exists: inject the key bar's own time
    # (the fixture's index type, so sort keys agree). Only the wiring is under test here.
    monkeypatch.setattr(cycle, "_htf_cons_touch_time", lambda htf, hp, ocl, inval_time: htf.index[hp])
    from tests.unit.test_cycle import MTF, HTF, _ltf, _frame, AGG_TOUCH, CONS_TOUCH, FLAT_ATR, EQUITY  # noqa
    cycle.run_cycle("egpattern_open", _frame(MTF), _frame(HTF), _ltf(AGG_TOUCH, CONS_TOUCH),
                    target_r=3.0, equity=EQUITY, loss_ceiling=0.01, atr_by_time=FLAT_ATR,
                    symbol_allocation_pct=4.0)
    assert seen and "htf_cons_touch" in seen[-1]
