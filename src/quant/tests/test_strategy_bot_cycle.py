"""Flow stage: Strategy bot cycle, Risk gate, Approved order queue (cycle rules, owner 2026-11-19).
Status: TESTS for the cycle wiring in strategy_bot.py (paper). Real gate, real POV executor and the
PaperBroker; only the bar feed and the cycle strategy are scripted fakes.

Rules checked here:
- one parent per HTF cycle; the first fresh trigger sizes it through the risk gate;
- no entry from an older bar (freshness: newest closed LTF bar only);
- an LTF stop-out pauses slicing; the next LTF touch resumes it (same quantity);
- cycle positions: no breakeven; trail from entry only after a conservative touch; target None until N is set.
"""
import json
from datetime import datetime, timedelta

import pandas as pd
import yaml

from quant.core.config import RiskLimits
from quant.execution.openalgo_client import PaperBroker
from quant.execution.strategy_bot import BotConfig, PaperSender, StrategyBot
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState
from quant.risk.kill_switch import KillSwitch
from quant.strategies.cycle import CycleResult, Entry
from quant.strategies.engulf_sequence_strategy import CycleView

NOW = datetime(2026, 11, 17, 10, 15)
T_OLD = pd.Timestamp("2026-11-17 10:00")
T_NEW = pd.Timestamp("2026-11-17 10:05")
T_NEXT = pd.Timestamp("2026-11-17 10:10")
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
    """A cycle strategy that returns one scripted CycleView per bot cycle."""
    is_cycle = True

    def __init__(self, name, views):
        self.name = name
        self.views = list(views)

    def cycle_for(self, symbol, now):
        return self.views.pop(0) if self.views else None


def _result(entries=(), paused=False, mode="target_pending", target=None, status="open"):
    return CycleResult(kind="egpattern_open", status=status, htf_low=HTF_LOW, entries=list(entries),
                       avg_entry=None, mode=mode, target=target, htf_key_time=HTF_KEY, ltf_paused=paused)


def _view(result, newest):
    return CycleView(results=[result], newest_ltf_time=newest, atr=1.0)


def _limits():
    return RiskLimits(max_order_quantity=10_000, max_order_notional=500_000.0,
                      max_open_exposure=2_000_000.0, max_daily_loss=50_000.0,
                      max_strategy_loss=25_000.0, max_adv_pct=0.01, max_symbols_per_portfolio=20)


def _bot(tmp_path, strategy, store=None, restore_at=None):
    screen = tmp_path / "latest.json"
    screen.write_text(json.dumps({"as_of": NOW.isoformat(), "results": [
        {"symbol": "INFY", "selected": True, "asm_gsm": False, "circuit_pct": 0.2}]}), encoding="utf-8")
    sources = tmp_path / "data_sources.yaml"
    sources.write_text(yaml.safe_dump({"sources": {"live_feed": {"live_allowed": True}}}), encoding="utf-8")
    cfg = BotConfig(mode="paper", screen_path=str(screen), data_sources_path=str(sources))
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
    assert pos.target is None  # target_pending: no fixed target until N is set
    assert _child_qtys(audit)[0] == 500  # POV 10% of 5000 bar volume, first child


def test_entry_on_older_bar_does_not_open_a_cycle(tmp_path):
    strat = ScriptedCycle("scripted", [_view(_result([Entry(T_OLD, 100.0, "aggressive")]), T_NEW)])
    bot, audit = _bot(tmp_path, strat)
    bot.cycle(NOW)
    assert _key() not in bot._parents
    assert _child_qtys(audit) == []


def test_ltf_stop_out_pauses_and_next_ltf_touch_resumes_same_quantity(tmp_path):
    strat = ScriptedCycle("scripted", [
        _view(_result([Entry(T_NEW, 100.0, "aggressive")]), T_NEW),                 # cycle 1: open, 500 sent
        _view(_result([Entry(T_NEW, 100.0, "aggressive")], paused=True), T_NEXT),   # cycle 2: stop-out, paused
        _view(_result([Entry(T_NEXT, 100.0, "aggressive")]), T_NEXT),               # cycle 3: new touch, resume
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


def test_restart_on_a_later_day_never_reopens_the_same_htf_cycle(tmp_path):
    # Day 1: the cycle opens (500 sent). The bot restarts the next day and a fresh trigger appears
    # inside the same HTF cycle. The old cycle must not get a second full-size quantity.
    from quant.execution.position_store import PositionStore

    store = PositionStore(tmp_path / "positions.json")
    day1 = ScriptedCycle("scripted", [_view(_result([Entry(T_NEW, 100.0, "aggressive")]), T_NEW)])
    bot1, _ = _bot(tmp_path, day1, store=store)
    bot1.cycle(NOW)
    assert _key() in bot1._positions

    next_day = NOW + timedelta(days=1)
    t_next_day = pd.Timestamp("2026-11-18 10:05")
    day2 = ScriptedCycle("scripted", [_view(_result([Entry(t_next_day, 100.0, "aggressive")]), t_next_day)])
    bot2, audit2 = _bot(tmp_path, day2, store=store, restore_at=next_day)
    assert _key() in bot2._positions  # the open position came back
    bot2.cycle(next_day)
    assert _key() not in bot2._parents
    assert _child_qtys(audit2) == []


def test_restart_after_the_cycle_position_exited_still_does_not_reopen_it(tmp_path):
    # The position was stopped out on day 1 (gone from the book), then the bot restarted on day 2.
    # Only the persisted cycle-done set stops a new full-size parent for that HTF cycle.
    from quant.execution.position_store import PositionStore

    store = PositionStore(tmp_path / "positions.json")
    day1 = ScriptedCycle("scripted", [_view(_result([Entry(T_NEW, 100.0, "aggressive")]), T_NEW)])
    bot1, _ = _bot(tmp_path, day1, store=store)
    bot1.cycle(NOW)
    bot1._positions.pop(_key())  # exited
    bot1._persist_positions()

    next_day = NOW + timedelta(days=1)
    t_next_day = pd.Timestamp("2026-11-18 10:05")
    day2 = ScriptedCycle("scripted", [_view(_result([Entry(t_next_day, 100.0, "aggressive")]), t_next_day)])
    bot2, audit2 = _bot(tmp_path, day2, store=store, restore_at=next_day)
    assert _key() not in bot2._positions
    bot2.cycle(next_day)
    assert _key() not in bot2._parents
    assert _child_qtys(audit2) == []


def test_conservative_touch_switches_position_to_atr_trail(tmp_path):
    strat = ScriptedCycle("scripted", [
        _view(_result([Entry(T_NEW, 100.0, "aggressive")]), T_NEW),
        _view(_result([Entry(T_NEW, 100.0, "aggressive"), Entry(T_NEXT, 99.0, "conservative")],
                      mode="trail"), T_NEXT),
    ])
    bot, _ = _bot(tmp_path, strat)
    bot.cycle(NOW)
    bot.cycle(NOW + timedelta(minutes=5))
    pos = bot._positions[_key()]
    assert pos.trail_from_start is True
    assert pos.target is None
    assert pos.breakeven_enabled is False
    assert bot._parents[_key()].trail is True
