"""Flow stage: Protection, Algo router, Risk gate (exit path of the strategy bot).
Status: TESTS for PHASE 3 IMPLEMENTED exits in the bot. Real gate, real POV executor,
real paper broker. Bars are scripted per cycle; the last closed bar is row -2.
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

NOW = datetime(2026, 11, 10, 10, 15)


class Audit:
    def __init__(self):
        self.records = []

    def append(self, kind, payload):
        self.records.append((kind, payload))
        return "h"

    def kinds(self):
        return [k for k, _ in self.records]

    def get(self, kind):
        return [p for k, p in self.records if k == kind]


class ScriptedBars:
    """Every bar in the window is flat at 100 except the last closed bar (row -2), set per cycle."""

    def __init__(self):
        self.last = dict(open=100.0, high=100.0, low=100.0, close=100.0, volume=1_000_000.0)
        self.missing = False

    def set_last(self, **kw):
        self.last.update(kw)

    def bars(self, symbol, now):
        if self.missing:
            return None
        rows = [dict(open=100.0, high=100.0, low=100.0, close=100.0, volume=1_000_000.0)] * 40
        rows[-2] = dict(self.last)
        return pd.DataFrame(rows)

    def daily_stats(self, symbol, now):
        return {"adv_shares": 5_000_000.0, "prev_close": 100.0}


class Signal:
    name = "fixed"

    def signal(self, df):
        return {"side": "BUY", "entry": 100.0, "sl_dist": 2.0, "tp_dist": 6.0, "time": "T1", "atr": 1.0}


class FlakyExitBroker:
    """First order fills (entry). Every later order is rejected (exit)."""

    def __init__(self):
        self._paper = PaperBroker()
        self.calls = 0

    def place(self, intent, decision=None):
        self.calls += 1
        if self.calls == 1:
            return self._paper.place(intent, decision)
        return {"order_id": "", "status": "REJECTED", "filled_qty": 0, "avg_price": None}


def _limits():
    return RiskLimits(max_order_quantity=10_000, max_order_notional=500_000.0,
                      max_open_exposure=2_000_000.0, max_daily_loss=50_000.0,
                      max_strategy_loss=25_000.0, max_adv_pct=0.01, max_symbols_per_portfolio=20)


def _bot(tmp_path, broker, bars, signal=None):
    screen = tmp_path / "latest.json"
    screen.write_text(json.dumps({"as_of": NOW.isoformat(), "results": [
        {"symbol": "INFY", "selected": True, "asm_gsm": False, "circuit_pct": 0.2}]}), encoding="utf-8")
    sources = tmp_path / "data_sources.yaml"
    sources.write_text(yaml.safe_dump({"sources": {"live_feed": {"live_allowed": True}}}), encoding="utf-8")
    cfg = BotConfig(mode="paper", screen_path=str(screen), data_sources_path=str(sources))
    audit = Audit()
    bot = StrategyBot(
        cfg=cfg, strategies=[signal or Signal()], bars=bars, sender=PaperSender(broker), audit=audit,
        api_key="KEY", read_funds=lambda: 1_000_000.0, limits=_limits(),
        kill=KillSwitch(tmp_path / "kill.json"), safe=SafeMode(), halt=HaltHandler(),
        state=RiskState(),
    )
    bot.run_startup_recon(None)  # paper-style startup: no broker to compare
    return bot, audit


def _open_position(bot, bars):
    """Cycle 1: entry fills in full (1250 shares at paper price ~100.05). Flat bars, no exit."""
    bot.cycle(NOW)
    assert bars.bars("INFY", NOW) is not None
    assert len(bot._positions) == 1


def test_position_opens_with_stop_and_target_from_signal(tmp_path):
    bars = ScriptedBars()
    bot, audit = _bot(tmp_path, PaperBroker(), bars)
    _open_position(bot, bars)
    pos = list(bot._positions.values())[0]
    assert pos.qty == 1250
    assert pos.stop == 98.0 and pos.target == 106.0
    assert pos.atr == 1.0
    assert audit.get("position_open")


def test_stop_hit_sends_exit_and_closes_position(tmp_path):
    bars = ScriptedBars()
    bot, audit = _bot(tmp_path, PaperBroker(), bars)
    _open_position(bot, bars)
    bars.set_last(open=99.0, high=99.5, low=97.0, close=97.5)
    bot.cycle(NOW + timedelta(minutes=5))
    exits = audit.get("exit_sent")
    assert len(exits) == 1 and exits[0]["reason"] == "stop"
    assert exits[0]["qty"] == 1250
    assert bot._positions == {}


def test_target_hit_sends_exit_at_target(tmp_path):
    bars = ScriptedBars()
    bot, audit = _bot(tmp_path, PaperBroker(), bars)
    _open_position(bot, bars)
    bars.set_last(open=101.0, high=107.0, low=100.5, close=106.5)
    bot.cycle(NOW + timedelta(minutes=5))
    exits = audit.get("exit_sent")
    assert len(exits) == 1 and exits[0]["reason"] == "target"
    assert exits[0]["price"] == 106.0


def test_breakeven_move_is_logged_and_no_exit(tmp_path):
    bars = ScriptedBars()
    bot, audit = _bot(tmp_path, PaperBroker(), bars)
    _open_position(bot, bars)
    bars.set_last(open=101.0, high=103.0, low=100.5, close=102.5)
    bot.cycle(NOW + timedelta(minutes=5))
    assert audit.get("exit_sent") == []
    assert audit.get("stop_moved")
    pos = list(bot._positions.values())[0]
    assert pos.be_done is True and pos.stop > 100.0


def test_missing_bars_keep_position_open_and_say_so(tmp_path):
    bars = ScriptedBars()
    bot, audit = _bot(tmp_path, PaperBroker(), bars)
    _open_position(bot, bars)
    bars.missing = True
    bot.cycle(NOW + timedelta(minutes=5))
    assert len(bot._positions) == 1
    assert audit.get("exit_no_bars")


def test_unclear_exit_reply_halts_position_without_resend(tmp_path):
    bars = ScriptedBars()
    broker = FlakyExitBroker()
    bot, audit = _bot(tmp_path, broker, bars)
    _open_position(bot, bars)
    bars.set_last(open=99.0, high=99.5, low=97.0, close=97.5)
    bot.cycle(NOW + timedelta(minutes=5))
    assert broker.calls == 2  # entry + one exit attempt
    halted = [p for p in audit.get("exit_halted")]
    assert halted and "not resent" in halted[0]["note"]
    bot.cycle(NOW + timedelta(minutes=10))
    assert broker.calls == 2  # never resent automatically
    assert list(bot._positions.values())[0].exit_halted is True


def test_signal_without_atr_opens_no_position(tmp_path):
    class NoAtr(Signal):
        def signal(self, df):
            sig = super().signal(df)
            sig.pop("atr")
            return sig

    bars = ScriptedBars()
    bot, audit = _bot(tmp_path, PaperBroker(), bars, signal=NoAtr())
    bot.cycle(NOW)
    assert bot._positions == {}
    assert audit.get("parent_skipped")
    assert audit.get("child_sent") == []


def test_exit_reduces_open_exposure(tmp_path):
    bars = ScriptedBars()
    bot, _ = _bot(tmp_path, PaperBroker(), bars)
    _open_position(bot, bars)
    before = bot.state.open_notional
    assert before > 0
    bars.set_last(open=99.0, high=99.5, low=97.0, close=97.5)
    bot.cycle(NOW + timedelta(minutes=5))
    assert bot.state.open_notional < before
