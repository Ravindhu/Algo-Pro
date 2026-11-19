"""Flow stage: Strategy bot cycle, Risk gate, Approved order queue (cycle rules, owner 2026-11-19).
Status: TESTS for the cycle wiring in strategy_bot.py (paper). Real gate, real POV executor and the
PaperBroker; only the bar feed and the cycle strategy are scripted fakes.

Rules checked here:
- one parent per HTF cycle; the first fresh trigger sizes it through the risk gate;
- no entry from an older bar (freshness: newest closed LTF bar only);
- an LTF stop-out pauses slicing; the next LTF touch resumes it (same quantity, also after a restart);
- a cycle that has ended is never opened again after a restart;
- cycle parents are not dropped by the max_cycles age limit;
- cycle positions: no breakeven; trail from entry only after a conservative touch; target None until N is set.
"""
import json

import pytest
from datetime import datetime, timedelta

import pandas as pd
import yaml

from quant.core.config import RiskLimits
from quant.execution.openalgo_client import PaperBroker
from quant.execution.position_store import PositionStore
from quant.execution.strategy_bot import BotConfig, PaperSender, StrategyBot
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState
from quant.risk.kill_switch import KillSwitch
from quant.strategies.cycle import CycleResult, Entry
from quant.strategies.engulf_sequence_strategy import CycleView


@pytest.fixture(autouse=True)
def _test_only_owner_caps(tmp_path, monkeypatch):
    """Owner caps in config/risk_limits.yaml (10 shares, 10,000 INR) would refuse these test
    entries. Point the gate at a test-only file with large caps. The real file is not changed."""
    from quant.core import owner_limits_store as ols
    p = tmp_path / "owner_caps_for_tests.yaml"
    p.write_text("max_order_quantity: 1000000\nmax_order_notional: 100000000\n"
                 "max_open_exposure: 100000000\nblocked: false\n", encoding="utf-8")
    monkeypatch.setattr(ols, "DEFAULT_PATH", p)
    monkeypatch.setattr(ols, "_loaded_from", None)
    monkeypatch.setattr(ols, "_cached", {})
    yield

NOW = datetime(2026, 11, 17, 10, 15)
T_OLD = pd.Timestamp("2026-11-17 10:00")
T_NEW = pd.Timestamp("2026-11-17 10:05")
T_NEXT = pd.Timestamp("2026-11-17 10:10")
NEXT_DAY = NOW + timedelta(days=1)
T_DAY2 = pd.Timestamp("2026-11-18 10:05")
HTF_KEY = pd.Timestamp("2026-11-16 15:15")
HTF_LOW = 98.0  # entry 100 -> R = 2 -> 1250 shares with the test risk budget (same as test_strategy_bot_paper)


class Audit:
    def __init__(self):
        self.records = []

    def append(self, kind, payload):
        self.records.append((kind, payload))
        return "h"

    def kinds(self):
        return [k for k, _ in self.records]


class Bars:
    def bars(self, symbol, now):
        return pd.DataFrame({"open": [100.0] * 40, "high": [100.0] * 40, "low": [100.0] * 40,
                             "close": [100.0] * 40, "volume": [5000.0] * 40})

    def daily_stats(self, symbol, now):
        return {"adv_shares": 5_000_000.0, "prev_close": 100.0}


class ScriptedCycle:
    """A cycle strategy that returns one scripted CycleView per bot cycle (None when the script is empty)."""
    is_cycle = True

    def __init__(self, name, views):
        self.name = name
        self.views = list(views)

    def cycle_for(self, symbol, now):
        return self.views.pop(0) if self.views else None


# target N = 2.1 (owner): avg 100 + 2.1 x (100 - 98) = 104.2. target_pending (N unset) is tested separately.
def _result(entries=(), paused=False, mode="target", target=104.2, status="open"):
    return CycleResult(kind="egpattern_open", status=status, htf_low=HTF_LOW, entries=list(entries),
                       avg_entry=None, mode=mode, target=target, htf_key_time=HTF_KEY, ltf_paused=paused)


def _view(result, newest):
    return CycleView(results=[result], newest_ltf_time=newest, atr=1.0)


def _limits():
    return RiskLimits(max_order_quantity=10_000, max_order_notional=500_000.0,
                      max_open_exposure=2_000_000.0, max_daily_loss=50_000.0,
                      max_strategy_loss=25_000.0, max_adv_pct=0.01, max_symbols_per_portfolio=20)


def _bot(tmp_path, strategy, store=None, restore_at=None, **cfg_over):
    screen = tmp_path / "latest.json"
    as_of = restore_at or NOW  # a restarted bot reads a fresh screen (the live screen is refreshed each cycle)
    screen.write_text(json.dumps({"as_of": as_of.isoformat(), "results": [
        {"symbol": "INFY", "selected": True, "asm_gsm": False, "circuit_pct": 0.2}]}), encoding="utf-8")
    sources = tmp_path / "data_sources.yaml"
    sources.write_text(yaml.safe_dump({"sources": {"live_feed": {"live_allowed": True}}}), encoding="utf-8")
    cfg = BotConfig(mode="paper", screen_path=str(screen), data_sources_path=str(sources), **cfg_over)
    audit = Audit()
    bot = StrategyBot(cfg=cfg, strategies=[strategy], bars=Bars(), sender=PaperSender(PaperBroker()),
                      audit=audit, api_key="KEY", read_funds=lambda: 1_000_000.0, limits=_limits(),
                      kill=KillSwitch(tmp_path / "kill.json"), safe=SafeMode(), halt=HaltHandler(),
                      state=RiskState(), position_store=store)
    if restore_at is not None:
        bot.restore_positions(restore_at)  # before startup recon, as in the live runner
    bot.run_startup_recon(None)
    return bot, audit


def _key():
    return f"INFY|scripted|BUY|cycle|{HTF_KEY}"


def _child_qtys(audit):
    return [p["qty"] for k, p in audit.records if k == "child_sent"]


def test_first_fresh_trigger_opens_one_cycle_parent_with_hard_htf_stop(tmp_path):
    strat = ScriptedCycle("scripted", [_view(_result([Entry(T_NEW, 100.0, "aggressive")]), T_NEW)])
    bot, audit = _bot(tmp_path, strat)
    bot.cycle(NOW)
    assert _key() in bot._parents
    pos = bot._positions[_key()]
    assert pos.stop == HTF_LOW
    assert pos.breakeven_enabled is False
    assert pos.trail_from_start is False
    assert pos.target == pytest.approx(104.2)  # target = avg + N x R, N = 2.1 (owner)
    assert _child_qtys(audit)[0] == 500  # POV 10% of 5000 bar volume, first child


def test_entry_on_older_bar_does_not_open_a_cycle(tmp_path):
    strat = ScriptedCycle("scripted", [_view(_result([Entry(T_OLD, 100.0, "aggressive")]), T_NEW)])
    bot, audit = _bot(tmp_path, strat)
    bot.cycle(NOW)
    assert _key() not in bot._parents
    assert _child_qtys(audit) == []


def test_ltf_stop_out_pauses_and_next_ltf_touch_resumes_same_quantity(tmp_path):
    strat = ScriptedCycle("scripted", [
        _view(_result([Entry(T_NEW, 100.0, "aggressive")]), T_NEW),                  # open, 500 sent
        _view(_result([Entry(T_NEW, 100.0, "aggressive")], paused=True), T_NEXT),    # stop-out, paused
        _view(_result([Entry(T_NEXT, 100.0, "aggressive")]), T_NEXT),                # new touch, resume
    ])
    bot, audit = _bot(tmp_path, strat)
    bot.cycle(NOW)
    assert _child_qtys(audit) == [500]
    bot.cycle(NOW + timedelta(minutes=5))
    assert _child_qtys(audit) == [500]  # paused: nothing new sent
    assert bot._parents[_key()].paused is True
    bot.cycle(NOW + timedelta(minutes=10))
    assert bot._parents[_key()].paused is False
    assert sum(_child_qtys(audit)) == 1000
    bot.cycle(NOW + timedelta(minutes=15))  # no new view: the parent keeps filling the same quantity
    assert sum(_child_qtys(audit)) == 1250


def test_restart_mid_cycle_refills_the_remaining_quantity(tmp_path):
    # Day 1: 500 of 1250 filled. Restart on day 2 with a fresh LTF touch: the remaining 750 come back
    # from disk and keep filling. The quantity is not sized again.
    store = PositionStore(tmp_path / "positions.json")
    day1 = ScriptedCycle("scripted", [_view(_result([Entry(T_NEW, 100.0, "aggressive")]), T_NEW)])
    bot1, audit1 = _bot(tmp_path, day1, store=store)
    bot1.cycle(NOW)
    assert _child_qtys(audit1) == [500]

    day2 = ScriptedCycle("scripted", [_view(_result([Entry(T_DAY2, 100.0, "aggressive")]), T_DAY2)])
    bot2, audit2 = _bot(tmp_path, day2, store=store, restore_at=NEXT_DAY)
    assert _key() in bot2._positions  # the open position came back
    assert bot2._parents[_key()].remaining == 750
    bot2.cycle(NEXT_DAY)
    assert _child_qtys(audit2) == [500]
    assert bot2._parents[_key()].remaining == 250


def test_restart_after_stop_out_keeps_the_parent_paused_then_refills(tmp_path):
    # Stop-out on day 1 (position gone, parent kept with 750 left and paused). Restart on day 2.
    # The next LTF touch refills it; nothing is sent while it is still paused.
    store = PositionStore(tmp_path / "positions.json")
    day1 = ScriptedCycle("scripted", [
        _view(_result([Entry(T_NEW, 100.0, "aggressive")]), T_NEW),
        _view(_result([Entry(T_NEW, 100.0, "aggressive")], paused=True), T_NEXT),  # no fresh entry: stop-out
    ])
    bot1, _ = _bot(tmp_path, day1, store=store)
    bot1.cycle(NOW)
    bot1._finish_exit(_key(), bot1._positions[_key()], 98.0, "stop", 98.0)  # the stop closed the position
    bot1.cycle(NOW + timedelta(minutes=5))  # paused, persisted
    assert bot1._parents[_key()].paused is True
    assert bot1._parents[_key()].remaining == 750

    day2 = ScriptedCycle("scripted", [
        _view(_result([Entry(T_NEW, 100.0, "aggressive")], paused=True), T_DAY2),  # still waiting
        _view(_result([Entry(T_DAY2, 100.0, "aggressive")]), T_DAY2),              # new touch
    ])
    bot2, audit2 = _bot(tmp_path, day2, store=store, restore_at=NEXT_DAY)
    assert bot2._parents[_key()].paused is True
    assert bot2._parents[_key()].remaining == 750
    bot2.cycle(NEXT_DAY)
    assert _child_qtys(audit2) == []  # paused: waits for the touch
    bot2.cycle(NEXT_DAY + timedelta(minutes=5))
    assert _child_qtys(audit2) == [500]  # the touch resumes and refills the same quantity


def test_stop_out_then_next_ltf_touch_refills_in_the_same_session(tmp_path):
    strat = ScriptedCycle("scripted", [
        _view(_result([Entry(T_NEW, 100.0, "aggressive")]), T_NEW),                  # open, 500 sent
        _view(_result([Entry(T_NEW, 100.0, "aggressive")], paused=True), T_NEXT),    # stop-out: paused
        _view(_result([Entry(T_NEXT, 100.0, "aggressive")]), T_NEXT),                # new touch: refill
    ])
    bot, audit = _bot(tmp_path, strat)
    bot.cycle(NOW)
    bot._finish_exit(_key(), bot._positions[_key()], 98.0, "stop", 98.0)
    assert _key() in bot._parents  # the cycle keeps its unfilled quantity
    bot.cycle(NOW + timedelta(minutes=5))
    assert _child_qtys(audit) == [500]  # paused
    bot.cycle(NOW + timedelta(minutes=10))
    assert _child_qtys(audit) == [500, 500]
    assert bot._parents[_key()].remaining == 250


def test_fresh_touch_at_or_below_the_htf_low_does_not_refill(tmp_path):
    strat = ScriptedCycle("scripted", [
        _view(_result([Entry(T_NEW, 100.0, "aggressive")]), T_NEW),
        _view(_result([Entry(T_NEXT, HTF_LOW, "aggressive")], paused=False), T_NEXT),  # touch at the hard stop
    ])
    bot, audit = _bot(tmp_path, strat)
    bot.cycle(NOW)
    bot._finish_exit(_key(), bot._positions[_key()], 98.0, "stop", 98.0)
    bot.cycle(NOW + timedelta(minutes=5))
    assert _child_qtys(audit) == [500]  # nothing new bought at or below the HTF low


def test_restart_after_a_cycle_ended_never_reopens_it(tmp_path):
    # Day 1: the cycle opens, then ends (HTF invalidated, parent closed). Restart on day 2 with a
    # fresh trigger inside the same HTF cycle. It must not open again.
    store = PositionStore(tmp_path / "positions.json")
    day1 = ScriptedCycle("scripted", [_view(_result([Entry(T_NEW, 100.0, "aggressive")]), T_NEW)])
    bot1, _ = _bot(tmp_path, day1, store=store)
    bot1.cycle(NOW)
    bot1._close(_key(), "HTF invalidated: unfilled quantity dropped")

    day2 = ScriptedCycle("scripted", [_view(_result([Entry(T_DAY2, 100.0, "aggressive")]), T_DAY2)])
    bot2, audit2 = _bot(tmp_path, day2, store=store, restore_at=NEXT_DAY)
    assert _key() not in bot2._parents
    bot2.cycle(NEXT_DAY)
    assert _key() not in bot2._parents
    assert _child_qtys(audit2) == []


def test_cycle_parent_is_not_dropped_by_the_max_cycles_age_limit(tmp_path):
    # max_cycles = 1 would drop an ordinary parent after its second cycle. A cycle parent keeps its
    # unfilled quantity until it is filled or its HTF cycle is invalidated.
    strat = ScriptedCycle("scripted", [_view(_result([Entry(T_NEW, 100.0, "aggressive")]), T_NEW)])
    bot, audit = _bot(tmp_path, strat, max_cycles=1)
    bot.cycle(NOW)
    bot.cycle(NOW + timedelta(minutes=5))  # no new view: keeps filling
    assert _key() in bot._parents
    assert sum(_child_qtys(audit)) == 1000


def test_target_pending_refuses_the_cycle_fail_closed(tmp_path):
    """Owner rule 11.7: an aggressive-only cycle with no N set has no exit rule, so it is never opened."""
    strat = ScriptedCycle("scripted", [_view(_result([Entry(T_NEW, 100.0, "aggressive")],
                                                     mode="target_pending", target=None), T_NEW)])
    bot, audit = _bot(tmp_path, strat)
    bot.cycle(NOW)
    assert _key() not in bot._parents
    assert _child_qtys(audit) == []
    assert any(k == "parent_skipped" and p.get("reason") == "target N not set" for k, p in audit.records)


def test_conservative_touch_switches_position_to_atr_trail(tmp_path):
    strat = ScriptedCycle("scripted", [
        _view(_result([Entry(T_NEW, 100.0, "aggressive")]), T_NEW),
        _view(_result([Entry(T_NEW, 100.0, "aggressive"), Entry(T_NEXT, 99.0, "conservative")],
                      mode="trail", target=None), T_NEXT),  # trail mode: no fixed target (rules 11.7)
    ])
    bot, _ = _bot(tmp_path, strat)
    bot.cycle(NOW)
    bot.cycle(NOW + timedelta(minutes=5))
    pos = bot._positions[_key()]
    assert pos.trail_from_start is True
    assert pos.target is None
    assert pos.breakeven_enabled is False
    assert bot._parents[_key()].trail is True
