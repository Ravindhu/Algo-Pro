"""PegSlicer (2026-11-21): between 5 min cycles a waiting peg parent places pegs only within the cycle's
allotment; fills and unfills are reported into the next cycle. No budget is added in between."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from quant.execution import strategy_bot as sb
from quant.execution.peg_slicer import PegAllotment, PegSlicer
from quant.execution.strategy_bot import BotConfig, StrategyBot

NOW = datetime(2026, 11, 21, 10, 0, 15)


class _Outcome(SimpleNamespace):
    pass


def _make_bot(*, replies, levels, entries_ok=True, faults=None, fraction=0.5, pov_mode="peg_band_buyer"):
    """replies: submit responses in order. levels: per tick, (price, bid_qty) or None (unverified).
    Returns (bot, calls). The bot is a stand-in that carries a real PegSlicer."""
    cfg = BotConfig(pov_mode=pov_mode, peg_bid_fraction=fraction)
    calls = []
    level_iter = iter(levels)
    reply_iter = iter(replies)

    class _Exec:
        def submit_child(self, child, price_type, limit_px):
            calls.append({"qty": child[1], "px": limit_px})
            return _Outcome(sent=True, response=next(reply_iter))

    noop = lambda *a, **k: None  # noqa: E731
    slicer = PegSlicer(cfg=cfg, executor=_Exec(), verifier=None, depth_provider=None,
                       make_child=lambda intent, qty, seq: sb.PovExecutor.make_child(intent, qty, seq),
                       log=noop, alert=noop, close=lambda key, reason: bot._parents.pop(key, None),
                       book=lambda p, key, active, qty, ref_px, resp, report: _book(p, qty),
                       add_fault=noop)
    slicer.price_now = lambda sym, cap: next(level_iter, None)

    def _book(p, qty):  # real booking is covered elsewhere; here only the allotment and remaining move
        p.remaining -= qty
        p.peg.spent += qty

    bot = SimpleNamespace(cfg=cfg, _peg=slicer, _parents={}, _faults=set(faults or ()),
                          _entries_ok_last=entries_ok)
    return bot, calls


@pytest.fixture(autouse=True)
def _fake_child(monkeypatch):
    monkeypatch.setattr(sb.PovExecutor, "make_child", staticmethod(lambda intent, qty, seq: ("child", qty, seq)))


def _parent(remaining=1000, cycle=False):
    return SimpleNamespace(intent=SimpleNamespace(symbol="A", ref_price=100.0), key="K", seq=0,
                           remaining=remaining, paused=False, cycle=cycle, peg=None)


def _waiting(bot, p, cap, budget):
    p.peg = PegAllotment(waiting=True, cap=cap, budget=budget, spent=0)
    bot._parents["K"] = p
    return p


def _tick(bot):
    StrategyBot.peg_slicer_tick(bot, NOW)


OK = {"status": "success", "avg_price": 99.1}
UNFILLED = {"status": "error", "reason": "limit not filled (open); cancelled"}


def test_arm_sets_this_cycle_allotment():
    bot, _ = _make_bot(replies=[], levels=[])
    p = _parent()
    bot._peg.arm(p, 101.0, 300)
    assert (p.peg.waiting, p.peg.budget, p.peg.spent, p.peg.cap) == (True, 300, 0, 101.0)


def test_arm_refuses_cycle_parents_and_zero_budget():
    bot, _ = _make_bot(replies=[], levels=[])
    p = _parent(cycle=True); bot._peg.arm(p, 101.0, 300)
    assert p.peg.waiting is False
    q = _parent(); bot._peg.arm(q, 101.0, 0)
    assert q.peg.waiting is False


def test_tick_spends_only_the_cycle_allotment():
    bot, calls = _make_bot(replies=[OK, OK], levels=[(99.1, 10_000), (99.1, 10_000)])
    p = _waiting(bot, _parent(remaining=1000), 101.0, 300)
    _tick(bot)  # 300 allowed (bid allows 5000) -> one child of 300
    assert [c["qty"] for c in calls] == [300]
    _tick(bot)  # allotment spent: no new child, no new budget
    assert len(calls) == 1
    assert p.remaining == 700 and p.peg.spent == 300


def test_bid_fraction_still_caps_each_peg():
    bot, calls = _make_bot(replies=[OK], levels=[(99.1, 100)], fraction=0.5)
    _waiting(bot, _parent(remaining=1000), 101.0, 900)
    _tick(bot)
    assert calls[0]["qty"] == 50   # 0.5 of the 100 shares at the level


def test_unverified_level_places_nothing_then_places_when_verified():
    bot, calls = _make_bot(replies=[OK], levels=[None, (99.1, 10_000)])
    _waiting(bot, _parent(), 101.0, 200)
    _tick(bot)
    assert calls == []
    _tick(bot)
    assert [c["qty"] for c in calls] == [200]


def test_unfilled_peg_leaves_allotment_and_is_reported():
    bot, calls = _make_bot(replies=[UNFILLED, UNFILLED, UNFILLED], levels=[(99.1, 10_000)] * 3)
    p = _waiting(bot, _parent(), 101.0, 200)
    _tick(bot)
    assert p.peg.spent == 0 and p.remaining == 1000     # nothing filled, allotment still available
    report = bot._peg.drain_report()
    assert report["unfilled"] == 1 and report["placed"] == 1


def test_fill_is_counted_into_next_cycle_report():
    bot, _ = _make_bot(replies=[OK], levels=[(99.1, 10_000)])
    _waiting(bot, _parent(), 101.0, 150)
    _tick(bot)
    assert bot._peg.drain_report() == {"placed": 1, "filled_shares": 150, "unfilled": 0}
    assert bot._peg.drain_report() == {"placed": 0, "filled_shares": 0, "unfilled": 0}


def test_nothing_placed_when_entries_blocked_or_faulted():
    bot, calls = _make_bot(replies=[OK], levels=[(99.1, 10_000)] * 2, entries_ok=False)
    _waiting(bot, _parent(), 101.0, 200)
    _tick(bot)
    assert calls == []
    bot._entries_ok_last, bot._faults = True, {("stop", "X")}
    _tick(bot)
    assert calls == []


def test_fixed_pov_mode_never_pegs_between_cycles():
    bot, calls = _make_bot(replies=[OK], levels=[(99.1, 10_000)], pov_mode="fixed")
    _waiting(bot, _parent(), 101.0, 200)
    _tick(bot)
    assert calls == []
