"""Review gaps S-1, W-1 and P-1 (2026-11-26).

S-1: entry sizing clamps to the bot's own limits as well as the owner-cap file.
W-1: the peg tick decides the cut-off from the clock now, not the policy saved at the last cycle.
P-1: a filled buy books exposure at the worse of the reference and the fill price.

Flow stage: Execution (sizing, peg slicing, booking)
Status: PHASE 3 IMPLEMENTED
"""
from datetime import datetime, time
from types import SimpleNamespace

from quant.core import owner_limits_store
from quant.execution import strategy_bot as sb


def test_s1_sizing_clamps_to_bot_limits(monkeypatch):
    monkeypatch.setattr(owner_limits_store, "load", lambda: {})  # no owner caps set
    bot_limits = SimpleNamespace(max_order_quantity=50, max_order_notional=None)
    assert sb._cap_to_owner_order(1000, 100.0, bot_limits) == 50


def test_s1_sizing_clamps_to_bot_notional(monkeypatch):
    monkeypatch.setattr(owner_limits_store, "load", lambda: {})
    bot_limits = SimpleNamespace(max_order_quantity=None, max_order_notional=10_000.0)
    assert sb._cap_to_owner_order(1000, 100.0, bot_limits) == 100


def test_s1_owner_cap_still_applies_without_bot_limits(monkeypatch):
    monkeypatch.setattr(owner_limits_store, "load", lambda: {"max_order_quantity": 20})
    assert sb._cap_to_owner_order(1000, 100.0) == 20


def _peg_fake(now_time: time, block_saved: bool):
    class Peg:
        ticks = 0

        def tick(self, parents, now):
            Peg.ticks += 1

    return SimpleNamespace(
        cfg=SimpleNamespace(pov_mode="peg_band_buyer", product="MIS"),
        _entries_ok_last=True, _faults=set(), _parents={},
        _policy={"block_entries": block_saved},  # stale: saved before the cut-off
        _square_off_at=time(15, 10), _flatten_at=time(15, 10),
        _peg=Peg(),
    ), Peg


def test_w1_peg_tick_stops_after_cutoff_even_if_saved_policy_is_stale():
    fake, peg = _peg_fake(time(15, 12), block_saved=False)
    sb.StrategyBot.peg_slicer_tick(fake, datetime(2026, 11, 26, 15, 12))
    assert peg.ticks == 0


def test_w1_peg_tick_runs_before_cutoff():
    fake, peg = _peg_fake(time(14, 30), block_saved=True)  # stale saved policy must not block early buys
    sb.StrategyBot.peg_slicer_tick(fake, datetime(2026, 11, 26, 14, 30))
    assert peg.ticks == 1


def test_p1_exposure_uses_fill_price_when_above_reference():
    booked = {}
    fake = SimpleNamespace(state=SimpleNamespace(open_notional=0.0), _booked=booked,
                           _record_fill=lambda *a, **k: None, _log=lambda *a, **k: None)
    p = SimpleNamespace(remaining=100, peg=None)
    report = {"children_sent": 0}
    sb.StrategyBot._book_child(fake, p, "k", None, 10, 100.0, {"avg_price": 101.0}, report)
    assert fake.state.open_notional == 10 * 101.0
    assert booked["k"] == 10 * 101.0  # released by the same amount on exit


def test_p1_exposure_keeps_reference_when_fill_is_below():
    fake = SimpleNamespace(state=SimpleNamespace(open_notional=0.0), _booked={},
                           _record_fill=lambda *a, **k: None, _log=lambda *a, **k: None)
    p = SimpleNamespace(remaining=100, peg=None)
    sb.StrategyBot._book_child(fake, p, "k", None, 10, 100.0, {"avg_price": 99.0}, {"children_sent": 0})
    assert fake.state.open_notional == 10 * 100.0
