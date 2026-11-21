"""Peg 15 s work (2026-11-21): between 5 min cycles a waiting peg parent places pegs only within the
cycle's allotment; fills and unfills are reported into the next cycle. No budget is added in between."""
import types
from types import SimpleNamespace

import pytest

from quant.execution import strategy_bot as sb
from datetime import datetime

from quant.execution.strategy_bot import BotConfig, StrategyBot

NOW = datetime(2026, 11, 21, 10, 0, 15)


class _Outcome(SimpleNamespace):
    pass


def _make_bot(*, replies, levels, entries_ok=True, faults=None, fraction=0.5):
    """replies: submit responses in order. levels: per tick, (price, bid_qty) or None (unverified)."""
    cfg = BotConfig(pov_mode="peg_band_buyer", peg_bid_fraction=fraction)
    calls = []
    level_iter = iter(levels)
    reply_iter = iter(replies)

    class _Exec:
        def submit_child(self, child, price_type, limit_px):
            calls.append({"qty": child[1], "px": limit_px})
            return _Outcome(sent=True, response=next(reply_iter))

    fake = SimpleNamespace(
        cfg=cfg, _executor=_Exec(), _parents={}, _faults=set(faults or ()),
        _entries_ok_last=entries_ok, _cycle_ts=None, _peg_15s={"placed": 0, "filled_shares": 0, "unfilled": 0},
        _log=lambda *a, **k: None, _alert=lambda *a, **k: None,
        _close=lambda key, reason: fake._parents.pop(key, None),
        _peg_price_now=lambda sym, cap: next(level_iter, None),
    )
    fake._submit_peg = types.MethodType(StrategyBot._submit_peg, fake)
    fake._settle_peg = types.MethodType(StrategyBot._settle_peg, fake)
    fake._drain_peg_report = types.MethodType(StrategyBot._drain_peg_report, fake)

    def book(p, key, active, qty, ref_px, response, report):  # real booking is covered elsewhere
        p.remaining -= qty
        p.cycle_spent += qty
    fake._book_child = book
    return fake, calls


@pytest.fixture(autouse=True)
def _fake_child(monkeypatch):
    monkeypatch.setattr(sb.PovExecutor, "make_child", staticmethod(lambda intent, qty, seq: ("child", qty, seq)))


def _parent(remaining=1000, cycle=False):
    return SimpleNamespace(intent=SimpleNamespace(symbol="A", ref_price=100.0), key="K", seq=0,
                           remaining=remaining, paused=False, cycle=cycle,
                           peg_waiting=False, peg_cap=None, cycle_budget=0, cycle_spent=0)


OK = {"status": "success", "avg_price": 99.1}
UNFILLED = {"status": "error", "reason": "limit not filled (open); cancelled"}


def test_arm_sets_this_cycle_allotment_and_resets_spent():
    fake, _ = _make_bot(replies=[], levels=[])
    p = _parent(); p.cycle_spent = 77
    StrategyBot._arm_peg(fake, p, 101.0, 300)
    assert (p.peg_waiting, p.cycle_budget, p.cycle_spent, p.peg_cap) == (True, 300, 0, 101.0)


def test_arm_refuses_cycle_parents_and_zero_budget():
    fake, _ = _make_bot(replies=[], levels=[])
    p = _parent(cycle=True); StrategyBot._arm_peg(fake, p, 101.0, 300)
    assert p.peg_waiting is False
    q = _parent(); StrategyBot._arm_peg(fake, q, 101.0, 0)
    assert q.peg_waiting is False


def test_15s_tick_spends_only_the_cycle_allotment():
    fake, calls = _make_bot(replies=[OK, OK], levels=[(99.1, 10_000), (99.1, 10_000)])
    p = _parent(remaining=1000); p.peg_waiting, p.peg_cap, p.cycle_budget = True, 101.0, 300
    fake._parents["K"] = p
    StrategyBot.peg_tick(fake, NOW)  # first tick: 300 allowed (bid allows 5000) -> one child of 300
    assert [c["qty"] for c in calls] == [300]
    StrategyBot.peg_tick(fake, NOW)  # allotment spent: no new child, no new budget
    assert len(calls) == 1
    assert p.remaining == 700 and p.cycle_spent == 300


def test_bid_fraction_still_caps_each_peg():
    fake, calls = _make_bot(replies=[OK], levels=[(99.1, 100)], fraction=0.5)
    p = _parent(remaining=1000); p.peg_waiting, p.peg_cap, p.cycle_budget = True, 101.0, 900
    fake._parents["K"] = p
    StrategyBot.peg_tick(fake, NOW)
    assert calls[0]["qty"] == 50   # 0.5 of the 100 shares at the level


def test_unverified_level_places_nothing_then_places_when_verified():
    fake, calls = _make_bot(replies=[OK], levels=[None, (99.1, 10_000)])
    p = _parent(); p.peg_waiting, p.peg_cap, p.cycle_budget = True, 101.0, 200
    fake._parents["K"] = p
    StrategyBot.peg_tick(fake, NOW)
    assert calls == []
    StrategyBot.peg_tick(fake, NOW)
    assert [c["qty"] for c in calls] == [200]


def test_unfilled_peg_leaves_budget_and_is_reported():
    fake, calls = _make_bot(replies=[UNFILLED, UNFILLED, UNFILLED], levels=[(99.1, 10_000)] * 3)
    p = _parent(); p.peg_waiting, p.peg_cap, p.cycle_budget = True, 101.0, 200
    fake._parents["K"] = p
    StrategyBot.peg_tick(fake, NOW)
    assert p.cycle_spent == 0 and p.remaining == 1000     # nothing filled, allotment still available
    assert fake._peg_15s["unfilled"] == 1 and fake._peg_15s["placed"] == 1


def test_fill_is_counted_into_next_cycle_report():
    fake, _ = _make_bot(replies=[OK], levels=[(99.1, 10_000)])
    p = _parent(); p.peg_waiting, p.peg_cap, p.cycle_budget = True, 101.0, 150
    fake._parents["K"] = p
    StrategyBot.peg_tick(fake, NOW)
    report = StrategyBot._drain_peg_report(fake)
    assert report == {"placed": 1, "filled_shares": 150, "unfilled": 0}
    assert StrategyBot._drain_peg_report(fake) == {"placed": 0, "filled_shares": 0, "unfilled": 0}


def test_nothing_placed_when_entries_blocked_or_faulted():
    fake, calls = _make_bot(replies=[OK], levels=[(99.1, 10_000)], entries_ok=False)
    p = _parent(); p.peg_waiting, p.peg_cap, p.cycle_budget = True, 101.0, 200
    fake._parents["K"] = p
    StrategyBot.peg_tick(fake, NOW)
    assert calls == []
    fake._entries_ok_last, fake._faults = True, {("stop", "X")}
    StrategyBot.peg_tick(fake, NOW)
    assert calls == []


def test_fixed_pov_mode_never_pegs_between_cycles():
    fake, calls = _make_bot(replies=[OK], levels=[(99.1, 10_000)])
    fake.cfg = BotConfig(pov_mode="fixed")
    p = _parent(); p.peg_waiting, p.peg_cap, p.cycle_budget = True, 101.0, 200
    fake._parents["K"] = p
    StrategyBot.peg_tick(fake, NOW)
    assert calls == []
