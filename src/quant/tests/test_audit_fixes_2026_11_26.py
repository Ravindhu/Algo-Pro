"""Live-path audit fixes 1, 2, 3 and 7 (2026-11-26). Offline: fakes only, no broker calls.

1. A stop move is a MODIFY of the live broker stop. The old stop is never cancelled first.
2. MIS square-off does not need bars: a position with no bar is still flattened.
3. Repeated cycle failures alert CRITICAL and block new entries (through the kill switch).
7. The live start is refused when the funds reply has no numeric availablecash.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from quant.core.types import TradeIntent
from quant.execution.position_store import PositionStore
from quant.execution.strategy_bot import BotConfig, BotRefused, StrategyBot, _Parent
from quant.core.config import RiskLimits
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState
from quant.risk.kill_switch import KillSwitch

ROOT = Path(__file__).resolve().parents[3]
LIMITS = ROOT / "config" / "risk_limits.yaml"
NOW = datetime(2026, 11, 16, 10, 0, 0)


class FakeAudit:
    def __init__(self):
        self.rows = []

    def append(self, kind, payload):
        self.rows.append((kind, payload))
        return kind

    def alerts(self, level):
        return [p for k, p in self.rows if k == "alert" and p.get("level") == level]


class ModifyStops:
    """Broker stop fake that supports modify. Records every call in order."""

    def __init__(self):
        self.calls = []
        self.place_reply = {"status": "success", "orderid": "S1"}
        self.modify_reply = {"status": "success"}
        self.cancel_reply = {"status": "success"}
        self.status_reply = {"order_status": "open", "avg_price": None}

    def place_stop(self, symbol, action, qty, trigger_price, strategy, product="MIS", exchange="NSE"):
        self.calls.append(("place", qty, trigger_price))
        return dict(self.place_reply)

    def modify_stop(self, order_id, symbol, action, qty, trigger_price, strategy, product="MIS", exchange="NSE"):
        self.calls.append(("modify", order_id, qty, trigger_price))
        return dict(self.modify_reply)

    def cancel_stop(self, order_id, strategy):
        self.calls.append(("cancel", order_id))
        return dict(self.cancel_reply)

    def stop_status(self, order_id, strategy):
        self.calls.append(("status", order_id))
        return dict(self.status_reply)


class Sender:
    bot_mode = "paper"

    def __init__(self):
        self.sent = []

    def place_child(self, intent, api_key, decision, price_type="MARKET", limit_price=None):
        self.sent.append((intent.side, intent.qty))
        return {"status": "success", "orderid": f"O{len(self.sent)}", "avg_price": intent.ref_price,
                "filled_qty": intent.qty}


class NoBars:
    """Feed that never returns a bar: the flatten must not depend on bars."""

    def bars(self, sym, now, exchange=None):
        return None

    def daily_stats(self, sym, now):
        return None


def _bot(tmp_path, *, stops=None, sender=None):
    cfg = BotConfig(mode="paper")
    return StrategyBot(
        cfg=cfg, strategies=[], bars=NoBars(), sender=sender or Sender(), audit=FakeAudit(),
        api_key="KEY", read_funds=lambda: 1_000_000.0, limits=RiskLimits.from_yaml(LIMITS),
        kill=KillSwitch(tmp_path / "kill.json"), safe=SafeMode(), halt=HaltHandler(),
        state=RiskState(), stops=stops if stops is not None else ModifyStops(),
        position_store=PositionStore(tmp_path / "positions.json"),
    )


def _open_position(bot, qty=10, px=100.0):
    key = "INFY|s|BUY|t1"
    intent = TradeIntent(intent_id=key, strategy_id="s", symbol="INFY", side="BUY", qty=qty,
                         ref_price=px, kind="entry", trade_day=NOW.date())
    p = _Parent(key=key, intent=intent, remaining=qty, circuit_pct=0.1, stop=98.0, target=105.0,
                atr=1.0, strategy="s")
    bot._parents[key] = p
    bot._record_fill(p, qty, {"avg_price": px})
    return key


# ---- fix 1: stop moves are modifies, never cancel-first -----------------------------------

def test_trail_move_modifies_the_live_stop_and_never_cancels_it(tmp_path):
    stops = ModifyStops()
    bot = _bot(tmp_path, stops=stops)
    key = _open_position(bot)
    stops.calls.clear()
    pos = bot._positions[key]
    pos.stop = 101.5                       # the trail moved the bot's stop up
    bot._sync_broker_stop(key)
    assert stops.calls == [("modify", "S1", 10, 101.5)]
    assert pos.broker_stop_id == "S1" and pos.broker_stop_trigger == 101.5
    assert ("stop", key) not in bot._faults


def test_refused_modify_keeps_the_old_stop_and_blocks_entries(tmp_path):
    stops = ModifyStops()
    stops.modify_reply = {"status": "error", "reason": "rejected"}
    bot = _bot(tmp_path, stops=stops)
    key = _open_position(bot)
    stops.calls.clear()
    pos = bot._positions[key]
    pos.stop = 101.5
    bot._sync_broker_stop(key)
    assert all(c[0] != "cancel" and c[0] != "place" for c in stops.calls)  # the old stop is never cancelled
    assert pos.broker_stop_id == "S1" and pos.broker_stop_trigger == 98.0    # still the old stop on record
    assert ("stop", key) in bot._faults
    assert any("could not be moved" in a["message"] for a in bot._audit.alerts("CRITICAL"))


def test_refused_modify_is_retried_and_clears_when_it_succeeds(tmp_path):
    stops = ModifyStops()
    stops.modify_reply = {"status": "error", "reason": "timeout"}
    bot = _bot(tmp_path, stops=stops)
    key = _open_position(bot)
    pos = bot._positions[key]
    pos.stop = 101.5
    bot._sync_broker_stop(key)
    assert ("stop", key) in bot._faults
    stops.modify_reply = {"status": "success"}
    bot._resync_stops()
    assert ("stop", key) not in bot._faults
    assert pos.broker_stop_trigger == 101.5


def test_position_without_a_broker_stop_is_still_placed_fresh(tmp_path):
    stops = ModifyStops()
    stops.place_reply = {"status": "error", "reason": "rejected"}
    bot = _bot(tmp_path, stops=stops)
    key = _open_position(bot)
    pos = bot._positions[key]
    assert pos.broker_stop_id == ""           # placement was refused: no stop to modify
    assert ("stop", key) in bot._faults
    stops.place_reply = {"status": "success", "orderid": "S9"}
    bot._resync_stops()
    assert pos.broker_stop_id == "S9" and ("stop", key) not in bot._faults


# ---- fix 2: MIS square-off works without bars ---------------------------------------------

def test_flatten_sends_an_exit_even_when_no_bar_was_ever_seen(tmp_path):
    sender = Sender()
    bot = _bot(tmp_path, sender=sender)
    key = _open_position(bot, qty=10, px=100.0)
    bot._policy = {"block_entries": True, "flatten": True}
    report = {"skipped": []}
    bot._manage_exits({}, NOW, report)
    assert sender.sent == [("SELL", 10)]
    assert key not in bot._positions
    assert any(k == "flatten_no_bars" for k, _ in bot._audit.rows)


def test_flatten_is_not_held_by_a_stale_feed(tmp_path):
    sender = Sender()
    bot = _bot(tmp_path, sender=sender)
    key = _open_position(bot, qty=10, px=100.0)
    bot._last_bar["INFY"] = {"open": 99.0, "high": 100.0, "low": 98.0, "close": 99.5,
                             "time": "x", "cycle": -100}          # far too old
    bot._policy = {"block_entries": True, "flatten": True}
    bot._manage_exits({}, NOW, {"skipped": []})
    assert sender.sent == [("SELL", 10)]


def test_no_flatten_outside_square_off_still_holds_on_no_bars(tmp_path):
    sender = Sender()
    bot = _bot(tmp_path, sender=sender)
    key = _open_position(bot)
    bot._policy = {"block_entries": False, "flatten": False}
    bot._manage_exits({}, NOW, {"skipped": []})
    assert sender.sent == []
    assert key in bot._positions


# ---- fix 3: repeated cycle failures escalate ------------------------------------------------

class _Failing:
    def __init__(self):
        self.alerts = []
        self.cfg = SimpleNamespace(vwap_poll_seconds=15, interval_minutes=5)
        self._kill = None

    def cycle(self, now):
        raise RuntimeError("boom")

    def _alert(self, level, message):
        self.alerts.append((level, message))


def test_three_cycle_failures_alert_and_trip_the_kill_switch(tmp_path):
    from scripts.run import run_strategy_bot as runner
    bot = _Failing()
    bot._kill = KillSwitch(tmp_path / "kill.json")
    audit = FakeAudit()
    runner.run(bot, audit, state_dir=tmp_path, sleep=lambda s: None, max_cycles=3,
               clock=lambda: NOW)
    assert any(lvl == "CRITICAL" for lvl, _ in bot.alerts)
    assert bot._kill.active is True


def test_a_good_cycle_resets_the_failure_count(tmp_path):
    from scripts.run import run_strategy_bot as runner

    class Flaky:
        def __init__(self):
            self.n = 0
            self.alerts = []
            self.cfg = SimpleNamespace(vwap_poll_seconds=15, interval_minutes=5)
            self._kill = KillSwitch(tmp_path / "kill.json")

        def cycle(self, now):
            self.n += 1
            if self.n % 3 != 0:
                raise RuntimeError("boom")

        def _alert(self, level, message):
            self.alerts.append(level)

    bot = Flaky()
    runner.run(bot, FakeAudit(), state_dir=tmp_path, sleep=lambda s: None, max_cycles=8, clock=lambda: NOW)
    assert bot._kill.active is False and bot.alerts == []


# ---- fix 7: funds reply must carry availablecash ------------------------------------------

def _live_runner():
    spec = importlib.util.spec_from_file_location("run_strategy_bot_live_t", ROOT / "scripts/run/run_strategy_bot_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_funds_reply_check_accepts_numbers_and_numeric_strings_only():
    live = _live_runner()
    ok = live.funds_reply_has_cash
    assert ok({"status": "success", "data": {"availablecash": 1000.5}})
    assert ok({"status": "success", "data": {"availablecash": "1000.50"}})
    assert not ok({"status": "success", "data": {"cash": 1000}})
    assert not ok({"status": "success", "data": {"availablecash": "n/a"}})
    assert not ok({"status": "success", "data": {"availablecash": True}})
    assert not ok({"status": "error", "message": "bad key"})
    assert not ok(None)
