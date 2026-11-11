"""Flow stage: Risk gate, Algo router, Protection (exits), Audit log, Alerts, Strategy bot runner.
Status: TESTS for the 2026-11-10 audit fixes. Real gate, real POV executor, real paper broker.
Bars advance one timestamp per cycle, so a new bar is a new bar (the older fixture reused one index).
"""
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
import yaml

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent
from quant.execution.exit_manager import ExitConfig, Position
from quant.execution.openalgo_client import PaperBroker
from quant.execution.strategy_bot import (AnalyzeSender, BotConfig, BotRefused, PaperSender,
                                          StrategyBot)
from quant.monitor.safe_mode import SafeMode
from quant.notify.alerts import Alerts
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch
from quant.strategies.signals.risk_adapter import gate_signal

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


class TimedBars:
    """One closed bar per cycle with a new timestamp. set_last changes the bar the bot reads next."""

    def __init__(self):
        self.cycle = 0
        self.missing = False
        self.last = dict(open=100.0, high=100.0, low=100.0, close=100.0, volume=1_000_000.0)

    def step(self, **kw):
        self.cycle += 1
        self.last.update(kw)

    def bars(self, symbol, now):
        if self.missing:
            return None
        rows = [dict(open=100.0, high=100.0, low=100.0, close=100.0, volume=1_000_000.0)] * 40
        rows[-2] = dict(self.last)
        index = pd.date_range("2026-11-02 09:15", periods=40, freq="5min") + pd.Timedelta(minutes=5 * self.cycle)
        return pd.DataFrame(rows, index=index)

    def daily_stats(self, symbol, now):
        return {"adv_shares": 5_000_000.0, "prev_close": 100.0}


class Signal:
    name = "fixed"

    def signal(self, df):
        return {"side": "BUY", "entry": 100.0, "sl_dist": 2.0, "tp_dist": 6.0, "time": "T1", "atr": 1.0}


class RejectExitBroker:
    """First order fills. Every later order is rejected, so an exit gets an unclear reply."""

    def __init__(self):
        self._paper = PaperBroker()
        self.calls = 0

    def place(self, intent, decision=None):
        self.calls += 1
        if self.calls == 1:
            return self._paper.place(intent, decision)
        return {"order_id": "", "status": "REJECTED", "filled_qty": 0, "avg_price": None}


def _limits(**over):
    base = dict(max_order_quantity=10_000, max_order_notional=500_000.0, max_open_exposure=2_000_000.0,
                max_daily_loss=50_000.0, max_strategy_loss=25_000.0, max_adv_pct=0.01,
                max_symbols_per_portfolio=20)
    base.update(over)
    return RiskLimits(**base)


class FakeFillSender:
    """Paper-style sender whose fill prices are set by the test, one per child."""

    bot_mode = "paper"

    def __init__(self, prices):
        self.prices = list(prices)

    def place_child(self, intent, api_key, decision, price_type="MARKET", limit_price=None):
        return {"status": "success", "orderid": "x", "avg_price": self.prices.pop(0),
                "filled_qty": intent.qty}


def _bot(tmp_path, broker, bars, *, limits=None, alerts=None, signal=None, sender=None, **cfg_over):
    screen = tmp_path / "latest.json"
    screen.write_text(json.dumps({"as_of": NOW.isoformat(), "results": [
        {"symbol": "INFY", "selected": True, "asm_gsm": False, "circuit_pct": 0.2}]}), encoding="utf-8")
    sources = tmp_path / "data_sources.yaml"
    sources.write_text(yaml.safe_dump({"sources": {"live_feed": {"live_allowed": True}}}), encoding="utf-8")
    cfg = BotConfig(mode="paper", screen_path=str(screen), data_sources_path=str(sources), **cfg_over)
    audit = Audit()
    kill = KillSwitch(tmp_path / "kill.json")
    bot = StrategyBot(
        cfg=cfg, strategies=[signal or Signal()], bars=bars,
        sender=sender or PaperSender(broker), audit=audit,
        api_key="KEY", read_funds=lambda: 1_000_000.0, limits=limits or _limits(),
        kill=kill, safe=SafeMode(), halt=HaltHandler(), state=RiskState(), alerts=alerts,
    )
    bot.run_startup_recon(None)
    return bot, audit, kill


# ---- P&L feed, daily loss breaker ------------------------------------------------------------

def test_stop_out_books_realized_loss_and_trips_kill_switch(tmp_path):
    bars = TimedBars()
    bot, audit, kill = _bot(tmp_path, PaperBroker(), bars, limits=_limits(max_daily_loss=100.0))
    bot.cycle(NOW)                                   # entry fills in full at ~100.05
    assert len(bot._positions) == 1
    bars.step(low=97.0)                              # stop at 98 is hit
    bot.cycle(NOW)
    assert not bot._positions
    assert bot.state.daily_pnl < -100.0              # realized loss is now in the bot's counters
    assert "pnl_realized" in audit.kinds()
    assert kill.active                               # daily loss limit trips the kill switch
    assert "daily loss" in kill.reason


def test_daily_pnl_resets_on_a_new_day(tmp_path):
    bars = TimedBars()
    bot, audit, _ = _bot(tmp_path, PaperBroker(), bars)
    bot.state.daily_pnl = -500.0
    bot.state.strategy_pnl = {"engulfing": -500.0}
    bot.cycle(NOW)
    from datetime import timedelta
    bot.cycle(NOW + timedelta(days=1))
    assert bot.state.daily_pnl == 0.0
    assert bot.state.strategy_pnl == {}
    assert "pnl_day_reset" in audit.kinds()


# ---- parent order and exposure after an exit -------------------------------------------------

def test_stop_out_closes_the_parent_so_it_does_not_buy_again(tmp_path):
    bars = TimedBars()
    bot, audit, _ = _bot(tmp_path, PaperBroker(), bars)
    bars.last["volume"] = 5_000.0                    # small bars: child = 500 shares, 750 left in parent
    bars.step(volume=5_000.0)
    bot.cycle(NOW)
    assert list(bot._positions.values())[0].qty == 500
    bars.step(low=97.0, volume=5_000.0)              # stop hit while the parent still has 750 to slice
    bot.cycle(NOW)
    assert not bot._positions
    sent_before = len(audit.get("child_sent"))
    bars.step(volume=5_000.0)                        # flat bar afterwards: nothing may be bought
    bot.cycle(NOW)
    assert not bot._positions
    assert len(audit.get("child_sent")) == sent_before


def test_exit_releases_exposure_booked_at_entry(tmp_path):
    bars = TimedBars()
    bot, _, _ = _bot(tmp_path, PaperBroker(), bars)
    bot.cycle(NOW)
    assert bot.state.open_notional == pytest.approx(1250 * 100.0)
    bars.step(low=97.0)
    bot.cycle(NOW)
    assert bot.state.open_notional == pytest.approx(0.0, abs=1e-6)


def test_entry_is_volume_weighted_average_of_child_fills(tmp_path):
    bars = TimedBars()
    bot, audit, _ = _bot(tmp_path, None, bars, sender=FakeFillSender([100.0, 110.0]))
    bars.last["volume"] = 5_000.0
    bars.step(volume=5_000.0)
    bot.cycle(NOW)                                   # 500 filled at 100
    bars.step(volume=5_000.0)
    bot.cycle(NOW)                                   # 500 more filled at 110
    pos = list(bot._positions.values())[0]
    assert pos.qty == 1000
    assert pos.entry == pytest.approx(105.0)         # average, not the first fill


# ---- outage: last known bar age limit --------------------------------------------------------

def test_stale_last_bar_holds_exit_and_raises_critical_alert(tmp_path):
    alerts = Alerts()
    bars = TimedBars()
    bot, audit, _ = _bot(tmp_path, PaperBroker(), bars, alerts=alerts, max_outage_cycles=3)
    key = "INFY|x|BUY|T1"
    bot._positions[key] = Position(key=key, symbol="INFY", side="BUY", qty=100, entry=100.0,
                                   stop=98.0, target=110.0, atr=1.0, strategy="x")
    bot._cycle_no = 10
    bot._last_bar["INFY"] = {"open": 100.0, "high": 100.0, "low": 90.0, "time": "old", "cycle": 2}
    bars.missing = True
    bot._manage_exits({}, NOW, {"skipped": []})
    assert key in bot._positions                     # stop was NOT acted on: the bar is 8 cycles old
    assert "exit_held_stale_bar" in audit.kinds()
    assert any(i["level"] == "CRITICAL" for i in alerts.items)


def test_recent_last_bar_still_manages_exit(tmp_path):
    bars = TimedBars()
    bot, audit, _ = _bot(tmp_path, PaperBroker(), bars, max_outage_cycles=3)
    key = "INFY|x|BUY|T1"
    bot._positions[key] = Position(key=key, symbol="INFY", side="BUY", qty=100, entry=100.0,
                                   stop=98.0, target=110.0, atr=1.0, strategy="x")
    bot._cycle_no = 10
    bot._last_bar["INFY"] = {"open": 100.0, "high": 100.0, "low": 90.0, "time": "recent", "cycle": 9}
    bars.missing = True
    bot._manage_exits({}, NOW, {"skipped": []})
    assert key not in bot._positions                 # 1 cycle old: stop acts
    assert "exit_used_last_bar" in audit.kinds()


# ---- alerts on halted positions -------------------------------------------------------------

def test_unclear_exit_reply_halts_position_and_alerts(tmp_path):
    alerts = Alerts()
    bars = TimedBars()
    bot, audit, _ = _bot(tmp_path, RejectExitBroker(), bars, alerts=alerts)
    bot.cycle(NOW)
    bars.step(low=97.0)
    bot.cycle(NOW)
    pos = list(bot._positions.values())[0]
    assert pos.exit_halted
    assert any(i["level"] == "CRITICAL" and "exit reply unclear" in i["message"] for i in alerts.items)


def test_positions_unmanaged_alert_when_source_not_approved(tmp_path):
    alerts = Alerts()
    bars = TimedBars()
    bot, audit, _ = _bot(tmp_path, PaperBroker(), bars, alerts=alerts)
    bot.cycle(NOW)
    assert bot._positions
    (tmp_path / "data_sources.yaml").write_text(yaml.safe_dump(
        {"sources": {"live_feed": {"live_allowed": False}}}), encoding="utf-8")
    bot.cycle(NOW)
    assert "positions_unmanaged" in audit.kinds()
    assert any(i["level"] == "CRITICAL" and "unmanaged" in i["message"] for i in alerts.items)


# ---- risk gate: held quantity for SELL exits ------------------------------------------------

def _exit_intent(qty):
    return TradeIntent(intent_id=f"x{qty}", strategy_id="s", symbol="INFY", side="SELL", qty=qty,
                       ref_price=100.0, kind="exit")


def test_gate_refuses_sell_exit_larger_than_held(tmp_path):
    kill = KillSwitch(tmp_path / "k.json")
    d = check_intent(_exit_intent(10), RiskState(), _limits(), kill, SafeMode(), held_qty=5)
    assert not d.approved
    assert any("exceeds held qty" in r for r in d.reasons)


def test_gate_allows_sell_exit_within_held(tmp_path):
    kill = KillSwitch(tmp_path / "k.json")
    d = check_intent(_exit_intent(5), RiskState(), _limits(), kill, SafeMode(), held_qty=5)
    assert not any("exceeds held qty" in r for r in d.reasons)


def test_gate_without_held_qty_keeps_old_behaviour(tmp_path):
    kill = KillSwitch(tmp_path / "k.json")
    d = check_intent(_exit_intent(10), RiskState(), _limits(), kill, SafeMode(), held_qty=None)
    assert not any("exceeds held qty" in r for r in d.reasons)


# ---- sizing clamp to caps ---------------------------------------------------------------------

def _sig():
    return {"side": "BUY", "entry": 100.0, "sl_dist": 2.0, "tp_dist": 6.0, "time": "T1", "atr": 1.0}


def _gate(tmp_path, limits, **over):
    kw = dict(symbol="INFY", strategy_id="s", equity=1_000_000.0, risk_fraction=0.0025,
              state=RiskState(), limits=limits, kill=KillSwitch(tmp_path / "k.json"), safe=SafeMode(),
              halt=HaltHandler(), available_funds=1_000_000.0, adv_shares=5_000_000.0,
              prev_close=100.0, circuit_pct=0.2, trade_day=NOW.date())
    kw.update(over)
    return gate_signal(_sig(), **kw)


def test_oversized_signal_is_clamped_to_order_cap_and_approved(tmp_path):
    lim = _limits(max_order_quantity=10, max_order_notional=10_000.0,
                  max_open_exposure=20_000.0, max_daily_loss=1000.0, max_strategy_loss=500.0)
    res = _gate(tmp_path, lim)                       # signal wants 1250 shares
    assert res.ok, res.reason
    assert res.intent.qty == 10


def test_clamp_to_zero_is_refused_with_reason(tmp_path):
    lim = _limits(max_order_notional=50.0)           # 50 / 100 = 0 shares allowed
    res = _gate(tmp_path, lim)
    assert not res.ok
    assert "allows 0 shares" in res.reason


# ---- analyze mode: fill confirmation ----------------------------------------------------------

class FakeAnalyzeClient:
    def __init__(self, state, avg=100.5):
        self.state, self.avg = state, avg

    def place_child(self, intent, api_key, decision, price_type="MARKET", limit_price=None):
        return {"status": "success", "orderid": "42"}

    def get_order_status(self, api_key, order_id, strategy):
        return {"status": "success", "data": {"order_status": self.state, "average_price": self.avg}}


def _intent():
    return TradeIntent(intent_id="i", strategy_id="s", symbol="INFY", side="BUY", qty=10, ref_price=100.0)


def test_analyze_sender_counts_complete_order_as_filled():
    out = AnalyzeSender(FakeAnalyzeClient("complete")).place_child(_intent(), "KEY", None)
    assert out["status"] == "success" and out["avg_price"] == 100.5 and out["filled_qty"] == 10


def test_analyze_sender_does_not_count_open_order_as_filled():
    out = AnalyzeSender(FakeAnalyzeClient("open")).place_child(_intent(), "KEY", None)
    assert out["status"] == "error"
    assert "fill not confirmed" in out["reason"]


# ---- runner -------------------------------------------------------------------------------------

def _write_cfg(tmp_path, **over):
    screen = tmp_path / "latest.json"
    screen.write_text(json.dumps({"as_of": NOW.isoformat(), "results": []}), encoding="utf-8")
    sources = tmp_path / "data_sources.yaml"
    sources.write_text(yaml.safe_dump({"sources": {"live_feed": {"live_allowed": False}}}), encoding="utf-8")
    cfg = dict(mode="paper", live_enabled=False, screen_path=str(screen),
               data_sources_path=str(sources), strategies=["engulfing"])
    cfg.update(over)
    bot_yaml = tmp_path / "bot.yaml"
    bot_yaml.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text(yaml.safe_dump(dict(max_order_quantity=10, max_order_notional=10000,
                                          max_open_exposure=20000, max_daily_loss=1000,
                                          max_strategy_loss=500, max_adv_pct=0.01,
                                          max_symbols_per_portfolio=8)), encoding="utf-8")
    return bot_yaml, limits


def _runner():
    import importlib.util
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("run_strategy_bot", root / "scripts" / "run" / "run_strategy_bot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _NoReader:
    def history(self, *a, **k):
        raise AssertionError("no history call expected when the source is not approved")


def test_runner_refuses_analyze_mode(tmp_path):
    run = _runner()
    bot_yaml, limits = _write_cfg(tmp_path, mode="analyze")
    with pytest.raises(BotRefused):
        run.build_bot(bot_cfg_path=bot_yaml, limits_path=limits, state_dir=tmp_path / "s",
                      reader=_NoReader(), api_key="KEY", paper_capital=100000)


def test_runner_refuses_live_flag(tmp_path):
    run = _runner()
    bot_yaml, limits = _write_cfg(tmp_path, live_enabled=True)
    with pytest.raises(BotRefused):
        run.build_bot(bot_cfg_path=bot_yaml, limits_path=limits, state_dir=tmp_path / "s",
                      reader=_NoReader(), api_key="KEY", paper_capital=100000)


def test_runner_runs_cycles_and_places_no_orders_when_source_unapproved(tmp_path):
    run = _runner()
    bot_yaml, limits = _write_cfg(tmp_path)
    state = tmp_path / "s"
    bot, audit = run.build_bot(bot_cfg_path=bot_yaml, limits_path=limits, state_dir=state,
                               reader=_NoReader(), api_key="KEY", paper_capital=100000)
    done = run.run(bot, audit, state_dir=state, clock=lambda: NOW, sleep=lambda s: None, max_cycles=2)
    assert done == 2
    kinds = [r["kind"] for r in audit.read_all()]
    assert "cycle_skipped" in kinds
    assert not bot._positions


def test_runner_stops_on_stop_flag(tmp_path):
    run = _runner()
    bot_yaml, limits = _write_cfg(tmp_path)
    state = tmp_path / "s"
    bot, audit = run.build_bot(bot_cfg_path=bot_yaml, limits_path=limits, state_dir=state,
                               reader=_NoReader(), api_key="KEY", paper_capital=100000)
    (state / run.STOP_FLAG).write_text("stop", encoding="utf-8")
    done = run.run(bot, audit, state_dir=state, clock=lambda: NOW, sleep=lambda s: None)
    assert done == 0
    assert "runner_stopped" in [r["kind"] for r in audit.read_all()]
