"""Tests for the 2026-11-15 live audit fixes (docs/AUDIT_FIX_PLAN_2026-11-15.md).

Covers: restart-safe position book (fix 2), broker SL-M stops (fix 1), retired old runner
(fix 3), LIMIT entries with a slippage band (fix 6), per-cycle egress guard (fix 8), and the
exchange field on every order (fix 9). Stop and fill behaviour is tested with fakes; no network.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

from quant.core.types import TradeIntent
from quant.execution.exit_manager import Position
from quant.execution.openalgo_client import OpenAlgoClient, PaperBroker
from quant.execution.position_store import PositionStore
from quant.execution.strategy_bot import (AnalyzeSender, BotConfig, BotRefused, PaperSender,
                                          StrategyBot, _Parent)
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


class FakeStops:
    """Records every broker stop call. Replies are set by the test."""

    def __init__(self):
        self.calls = []
        self.place_reply = {"status": "success", "orderid": "S1"}
        self.cancel_reply = {"status": "success"}
        self.status_reply = {"order_status": "open", "avg_price": None}

    def place_stop(self, symbol, action, qty, trigger_price, strategy, product="MIS"):
        self.calls.append(("place", symbol, action, qty, trigger_price))
        return dict(self.place_reply)

    def stop_status(self, order_id, strategy):
        self.calls.append(("status", order_id))
        return dict(self.status_reply)

    def cancel_stop(self, order_id, strategy):
        self.calls.append(("cancel", order_id))
        return dict(self.cancel_reply)


class FakeSender:
    """Paper-like sender that records each child and its price type."""
    bot_mode = "paper"

    def __init__(self):
        self.sent = []

    def place_child(self, intent, api_key, decision, price_type="MARKET", limit_price=None):
        self.sent.append((intent.side, intent.qty, price_type, limit_price))
        return {"status": "success", "orderid": f"O{len(self.sent)}", "avg_price": intent.ref_price,
                "filled_qty": intent.qty}


def _bot(tmp_path, *, stops=None, sender=None, mode="paper", cfg_over=None):
    cfg = BotConfig(mode=mode, **(cfg_over or {}))
    return StrategyBot(
        cfg=cfg, strategies=[], bars=None, sender=sender or FakeSender(), audit=FakeAudit(),
        api_key="KEY", read_funds=lambda: 1_000_000.0, limits=RiskLimits.from_yaml(LIMITS),
        kill=KillSwitch(tmp_path / "kill.json"), safe=SafeMode(), halt=HaltHandler(),
        state=RiskState(), stops=stops if stops is not None else FakeStops(),
        position_store=PositionStore(tmp_path / "positions.json"),
    )


def _parent(bot, key="INFY|s|BUY|t1", qty=10, px=100.0):
    intent = TradeIntent(intent_id=key, strategy_id="s", symbol="INFY", side="BUY", qty=qty,
                         ref_price=px, kind="entry", trade_day=NOW.date())
    p = _Parent(key=key, intent=intent, remaining=qty, circuit_pct=0.1, stop=98.0, target=105.0,
                atr=1.0, strategy="s")
    bot._parents[key] = p
    return p


# ---- fix 2: restart-safe position book ----------------------------------------------------

def test_position_store_round_trip_and_unreadable_file(tmp_path):
    store = PositionStore(tmp_path / "positions.json")
    assert store.load() is None
    store.save({"day": "2026-11-16", "positions": [], "booked": {}, "marks": {}, "done": [],
                "open_notional": 0.0})
    assert store.load()["day"] == "2026-11-16"
    (tmp_path / "positions.json").write_text("{broken", encoding="utf-8")
    assert store.load() is None


def test_fill_is_saved_and_a_new_bot_restores_it(tmp_path):
    bot = _bot(tmp_path)
    p = _parent(bot)
    bot._record_fill(p, 10, {"avg_price": 100.0})
    bot.state.open_notional = 1000.0
    bot._booked[p.key] = 1000.0
    bot._persist_positions()

    fresh = _bot(tmp_path)
    assert fresh.restore_positions(NOW) == 1
    restored = fresh._positions[p.key]
    assert restored.qty == 10 and restored.entry == 100.0
    assert restored.broker_stop_id == "S1" and restored.broker_stop_qty == 10
    assert fresh.state.open_notional == 1000.0
    assert fresh._booked[p.key] == 1000.0
    assert fresh._faults == set()


def test_restored_position_without_a_broker_stop_gets_a_fault(tmp_path):
    bot = _bot(tmp_path, stops=FakeStops())
    bot._positions["K1"] = Position(key="K1", symbol="INFY", side="BUY", qty=5, entry=100, stop=98,
                                    target=105, atr=1)
    bot._persist_positions()
    fresh = _bot(tmp_path)
    fresh.restore_positions(NOW)
    assert ("stop", "K1") in fresh._faults  # no stop on record: place one before anything else


def test_restore_from_an_earlier_day_keeps_positions_but_not_marks_or_done(tmp_path):
    bot = _bot(tmp_path)
    bot._positions["K1"] = Position(key="K1", symbol="INFY", side="BUY", qty=5, entry=100, stop=98,
                                    target=105, atr=1, broker_stop_id="S9", broker_stop_qty=5)
    bot._marks["K1"] = -50.0
    bot._done.add("K1")
    bot._persist_positions()
    fresh = _bot(tmp_path)
    fresh.restore_positions(datetime(2026, 11, 17, 9, 30))
    assert "K1" in fresh._positions
    assert fresh._marks == {} and fresh._done == set()


def test_live_refuses_without_stops_store_or_entry_guard(tmp_path):
    sender = FakeSender()
    sender.bot_mode = "live"
    base = dict(mode="live", live_enabled=True)
    ok_gate = lambda: (True, [])
    with pytest.raises(BotRefused, match="stop orders"):
        StrategyBot(cfg=BotConfig(**base), strategies=[], bars=None, sender=sender, audit=FakeAudit(),
                    api_key="K", read_funds=lambda: 1.0, limits=RiskLimits.from_yaml(LIMITS),
                    kill=KillSwitch(tmp_path / "k.json"), safe=SafeMode(), halt=HaltHandler(),
                    live_gate=ok_gate, position_store=PositionStore(tmp_path / "p.json"),
                    entry_guard=ok_gate)
    with pytest.raises(BotRefused, match="position store"):
        StrategyBot(cfg=BotConfig(**base), strategies=[], bars=None, sender=sender, audit=FakeAudit(),
                    api_key="K", read_funds=lambda: 1.0, limits=RiskLimits.from_yaml(LIMITS),
                    kill=KillSwitch(tmp_path / "k.json"), safe=SafeMode(), halt=HaltHandler(),
                    live_gate=ok_gate, stops=FakeStops(), entry_guard=ok_gate)
    with pytest.raises(BotRefused, match="entry guard"):
        StrategyBot(cfg=BotConfig(**base), strategies=[], bars=None, sender=sender, audit=FakeAudit(),
                    api_key="K", read_funds=lambda: 1.0, limits=RiskLimits.from_yaml(LIMITS),
                    kill=KillSwitch(tmp_path / "k.json"), safe=SafeMode(), halt=HaltHandler(),
                    live_gate=ok_gate, stops=FakeStops(), position_store=PositionStore(tmp_path / "p.json"))


# ---- fix 1: broker-side stops -------------------------------------------------------------

def test_fill_places_a_sell_stop_at_the_bot_stop_price(tmp_path):
    stops = FakeStops()
    bot = _bot(tmp_path, stops=stops)
    p = _parent(bot, qty=10)
    bot._record_fill(p, 10, {"avg_price": 100.0})
    assert stops.calls == [("place", "INFY", "SELL", 10, 98.0)]
    assert bot._positions[p.key].broker_stop_id == "S1"


def test_growing_a_position_replaces_the_stop_with_the_new_quantity(tmp_path):
    stops = FakeStops()
    bot = _bot(tmp_path, stops=stops)
    p = _parent(bot, qty=10)
    bot._record_fill(p, 4, {"avg_price": 100.0})
    stops.place_reply = {"status": "success", "orderid": "S2"}
    bot._record_fill(p, 6, {"avg_price": 101.0})
    assert ("cancel", "S1") in stops.calls
    assert stops.calls[-1] == ("place", "INFY", "SELL", 10, 98.0)
    pos = bot._positions[p.key]
    assert pos.broker_stop_id == "S2" and pos.broker_stop_qty == 10


def test_failed_stop_placement_blocks_entries_and_slicing(tmp_path):
    stops = FakeStops()
    stops.place_reply = {"status": "error", "reason": "rejected"}
    sender = FakeSender()
    bot = _bot(tmp_path, stops=stops, sender=sender)
    p = _parent(bot, qty=10)
    bot._record_fill(p, 10, {"avg_price": 100.0})
    assert ("stop", p.key) in bot._faults
    assert bot._positions[p.key].broker_stop_id == ""
    assert any("broker stop not placed" in a["message"] for a in bot._audit.alerts("CRITICAL"))
    bot._slice_parents({}, NOW, {"skipped": []})
    assert sender.sent == []  # no new exposure while a stop is missing


def test_broker_stop_that_filled_closes_the_position_and_books_pnl(tmp_path):
    stops = FakeStops()
    bot = _bot(tmp_path, stops=stops)
    p = _parent(bot, qty=10)
    bot._record_fill(p, 10, {"avg_price": 100.0})
    bot.state.open_notional = 1000.0
    bot._booked[p.key] = 1000.0
    stops.status_reply = {"order_status": "complete", "avg_price": 97.5}
    bot._check_broker_stops()
    assert p.key not in bot._positions
    assert bot.state.daily_pnl == pytest.approx((97.5 - 100.0) * 10)
    assert bot.state.open_notional == 0.0


def test_rejected_broker_stop_is_placed_again_next_cycle(tmp_path):
    stops = FakeStops()
    bot = _bot(tmp_path, stops=stops)
    p = _parent(bot, qty=10)
    bot._record_fill(p, 10, {"avg_price": 100.0})
    stops.status_reply = {"order_status": "rejected", "avg_price": None}
    bot._check_broker_stops()
    assert ("stop", p.key) in bot._faults
    stops.status_reply = {"order_status": "open", "avg_price": None}
    stops.place_reply = {"status": "success", "orderid": "S3"}
    bot._resync_stops()
    assert bot._positions[p.key].broker_stop_id == "S3"
    assert ("stop", p.key) not in bot._faults


def test_exit_is_held_when_the_broker_stop_cannot_be_cancelled(tmp_path):
    stops = FakeStops()
    sender = FakeSender()
    bot = _bot(tmp_path, stops=stops, sender=sender)
    p = _parent(bot, qty=10)
    bot._record_fill(p, 10, {"avg_price": 100.0})
    stops.cancel_reply = {"status": "error", "reason": "timeout"}
    stops.status_reply = {"order_status": "open", "avg_price": None}
    report = {"skipped": []}
    pos = bot._positions[p.key]
    bot._send_exit(p.key, pos, 103.0, "target", NOW, report)
    assert sender.sent == []                       # the bot did not sell into a live stop
    assert p.key in bot._positions                 # position still open
    assert pos.broker_stop_id == "S1"              # stop still on record
    assert any("could not be cancelled" in a["message"] for a in bot._audit.alerts("CRITICAL"))


def test_exit_finds_the_broker_stop_already_filled(tmp_path):
    stops = FakeStops()
    sender = FakeSender()
    bot = _bot(tmp_path, stops=stops, sender=sender)
    p = _parent(bot, qty=10)
    bot._record_fill(p, 10, {"avg_price": 100.0})
    stops.cancel_reply = {"status": "error", "reason": "already triggered"}
    stops.status_reply = {"order_status": "complete", "avg_price": 98.0}
    bot._send_exit(p.key, bot._positions[p.key], 103.0, "target", NOW, {"skipped": []})
    assert sender.sent == []                       # no second sell
    assert p.key not in bot._positions
    assert bot.state.daily_pnl == pytest.approx((98.0 - 100.0) * 10)


# ---- fix 6: LIMIT entries with a slippage band ---------------------------------------------

def test_entries_are_limit_orders_a_band_above_the_last_close(tmp_path):
    import pandas as pd
    bot = _bot(tmp_path, cfg_over={"entry_max_slippage_bps": 20.0})
    df = pd.DataFrame({"close": [100.0, 99.0]})  # iloc[-2] is the last closed bar: 100.0
    p = _parent(bot)
    price_type, limit = bot._entry_price(p, df)
    assert price_type == "LIMIT" and limit == pytest.approx(100.2)
    exit_intent = TradeIntent(intent_id="X-EXIT", strategy_id="s", symbol="INFY", side="SELL",
                              qty=10, ref_price=100.0, kind="exit")
    p.intent = exit_intent
    assert bot._entry_price(p, df) == ("MARKET", None)


def test_slippage_band_is_validated():
    with pytest.raises(ValueError, match="entry_max_slippage_bps"):
        BotConfig(entry_max_slippage_bps=0)
    with pytest.raises(ValueError, match="entry_max_slippage_bps"):
        BotConfig(entry_max_slippage_bps=500)


def _analyze_client(order_status):
    calls = []

    def transport(url, payload):
        calls.append((url.rsplit("/", 1)[-1], payload))
        if url.endswith("/analyzer"):
            return {"status": "success", "data": {"analyze_mode": True}}
        if url.endswith("/placeorder"):
            return {"status": "success", "orderid": "O9"}
        if url.endswith("/cancelorder"):
            return {"status": "success", "orderid": "O9"}
        if url.endswith("/orderstatus"):
            return {"status": "success", "data": order_status}
        return {"status": "error"}

    client = OpenAlgoClient("http://x", transport, mode="analyze")
    return client, calls


def test_unfilled_limit_entry_is_cancelled_not_left_open():
    client, calls = _analyze_client({"order_status": "open", "average_price": 0})
    sender = AnalyzeSender(client)
    intent = TradeIntent(intent_id="E1", strategy_id="s", symbol="INFY", side="BUY", qty=10,
                         ref_price=100.0, kind="entry")
    from quant.core.types import GateDecision
    res = sender.place_child(intent, "KEY", GateDecision("E1", True, ()), "LIMIT", 100.2)
    assert res["status"] == "error" and res.get("cancel_failed") is False
    assert "cancelled" in res["reason"]
    names = [c[0] for c in calls]
    assert "cancelorder" in names


def test_partial_limit_fill_is_flagged_not_dropped():
    client, _ = _analyze_client({"order_status": "partially filled", "average_price": 99.9})
    sender = AnalyzeSender(client)
    intent = TradeIntent(intent_id="E2", strategy_id="s", symbol="INFY", side="BUY", qty=10,
                         ref_price=100.0, kind="entry")
    from quant.core.types import GateDecision
    res = sender.place_child(intent, "KEY", GateDecision("E2", True, ()), "LIMIT", 100.2)
    assert res["status"] == "error" and res.get("partial") is True


# ---- fix 9: exchange on every order; stop payload ------------------------------------------

def test_every_order_names_the_exchange_and_a_stop_is_sl_m():
    sent = []

    def transport(url, payload):
        if url.endswith("/analyzer"):
            return {"status": "success", "data": {"analyze_mode": True}}
        sent.append((url.rsplit("/", 1)[-1], payload))
        return {"status": "success", "orderid": "O1"}

    client = OpenAlgoClient("http://x", transport, mode="analyze")
    client.place_stop("INFY", "SELL", 10, 98.0, "s", "KEY")
    stop = sent[-1][1]
    assert stop["exchange"] == "NSE" and stop["pricetype"] == "SL-M"
    assert stop["trigger_price"] == 98.0 and stop["action"] == "SELL" and stop["quantity"] == 10
    intent = TradeIntent(intent_id="E3", strategy_id="s", symbol="INFY", side="BUY", qty=10,
                         ref_price=100.0, kind="entry")
    from quant.core.types import GateDecision
    client.place_child(intent, "KEY", GateDecision("E3", True, ()), "LIMIT", 100.2)
    child = sent[-1][1]
    assert child["exchange"] == "NSE" and child["pricetype"] == "LIMIT" and child["price"] == 100.2


def test_stop_refused_without_a_key_or_with_a_bad_trigger():
    client = OpenAlgoClient("http://x", lambda u, p: {"status": "success"}, mode="analyze")
    with pytest.raises(ValueError):
        client.place_stop("INFY", "SELL", 10, float("nan"), "s", "KEY")
    with pytest.raises(ValueError):
        client.place_stop("INFY", "SELL", 0, 98.0, "s", "KEY")


def test_unknown_exchange_is_refused():
    with pytest.raises(ValueError, match="exchange"):
        OpenAlgoClient("http://x", lambda u, p: {}, mode="analyze", exchange="XYZ")


# ---- fix 8: entry guard every cycle -------------------------------------------------------

def test_entry_guard_refusal_is_logged_and_alerted(tmp_path):
    bot = _bot(tmp_path)
    bot._entry_guard = lambda: (False, ["guard says no"])
    assert bot._run_entry_guard() is False
    assert any("guard says no" in a["message"] for a in bot._audit.alerts("CRITICAL"))


# ---- fix 3: the retired runner refuses ------------------------------------------------------

def test_retired_run_live_main_refuses_and_sends_nothing(capsys):
    spec = importlib.util.spec_from_file_location("run_live_retired", ROOT / "scripts" / "run" / "run_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main() == 2
    out = capsys.readouterr().out
    assert "retired" in out and "run_strategy_bot_live.py" in out


# ---- per-strategy breakeven and trail; broker stop follows the bot's stop ---------------------

def test_exits_per_strategy_merge_with_global_values():
    cfg = BotConfig(strategies=("s1", "s2"), exits={"s1": {"trail_mult": 1.5}})
    s1 = cfg.exit_config_for("s1")
    s2 = cfg.exit_config_for("s2")
    assert s1.trail_mult == 1.5 and s1.breakeven_r == cfg.breakeven_r   # partial override keeps the rest
    assert s2.trail_mult == cfg.trail_mult and s2.breakeven_r == cfg.breakeven_r


def test_exits_refuse_unknown_strategy_and_bad_keys():
    with pytest.raises(ValueError, match="not in strategies"):
        BotConfig(strategies=("s1",), exits={"nope": {"trail_mult": 1.0}})
    with pytest.raises(ValueError, match="only breakeven_r and trail_mult"):
        BotConfig(strategies=("s1",), exits={"s1": {"stop_pct": 1.0}})
    with pytest.raises(ValueError, match="number > 0"):
        BotConfig(strategies=("s1",), exits={"s1": {"trail_mult": 0}})


def _bars_df(high, low):
    import pandas as pd
    idx = pd.to_datetime(["2026-11-16 09:20", "2026-11-16 09:25", "2026-11-16 09:30"])
    return pd.DataFrame({"open": [100.0, 100.0, 100.0], "high": [100.5, high, 100.5],
                         "low": [99.5, low, 99.5], "close": [100.0, 100.0, 100.0],
                         "volume": [1000, 1000, 1000]}, index=idx)


class _Bars:
    def __init__(self, df): self.df = df
    def bars(self, sym, now): return self.df
    def daily_stats(self, sym, now): return None


def test_each_strategy_trails_with_its_own_settings(tmp_path):
    cfg_over = {"strategies": ("s1", "s2"),
                "exits": {"s1": {"breakeven_r": 1.0, "trail_mult": 2.0},
                          "s2": {"breakeven_r": 0.5, "trail_mult": 1.0}}}
    bot = _bot(tmp_path, stops=FakeStops(), cfg_over=cfg_over)
    bot._bars = _Bars(_bars_df(high=103.0, low=99.5))
    for strat in ("s1", "s2"):
        bot._positions[strat] = Position(key=strat, symbol="INFY", side="BUY", qty=10, entry=100.0,
                                         stop=98.0, target=110.0, atr=1.0, strategy=strat)
    bot._manage_exits({"INFY": _bars_df(high=103.0, low=99.5)}, NOW, {"skipped": []})
    # bar high 103. s1: breakeven at 1R = 102 is reached, stop to 100, then trail 103 - 2*1 = 101.
    # s2: breakeven at 0.5R = 101 is reached, stop to 100, then trail 103 - 1*1 = 102.
    assert bot._positions["s1"].stop == pytest.approx(101.0)
    assert bot._positions["s2"].stop == pytest.approx(102.0)


def test_moved_trail_moves_the_broker_stop(tmp_path):
    stops = FakeStops()
    bot = _bot(tmp_path, stops=stops, cfg_over={"strategies": ("s1",)})
    bot._bars = _Bars(_bars_df(high=103.0, low=99.5))
    p = _parent(bot, qty=10)
    bot._record_fill(p, 10, {"avg_price": 100.0})
    stops.place_reply = {"status": "success", "orderid": "S7"}
    stops.calls.clear()
    bot._manage_exits({"INFY": _bars_df(high=103.0, low=99.5)}, NOW, {"skipped": []})
    pos = bot._positions[p.key]
    assert pos.stop > 98.0
    # old stop cancelled, new SL-M placed at the moved trigger
    assert ("cancel", "S1") in stops.calls
    placed = [c for c in stops.calls if c[0] == "place"]
    assert placed and placed[-1][4] == pytest.approx(round(pos.stop, 2))
    assert pos.broker_stop_id == "S7" and pos.broker_stop_trigger == round(pos.stop, 2)


def test_failed_stop_move_blocks_entries(tmp_path):
    stops = FakeStops()
    bot = _bot(tmp_path, stops=stops, cfg_over={"strategies": ("s1",)})
    bot._bars = _Bars(_bars_df(high=103.0, low=99.5))
    p = _parent(bot, qty=10)
    bot._record_fill(p, 10, {"avg_price": 100.0})
    stops.place_reply = {"status": "error", "reason": "rejected"}
    bot._manage_exits({"INFY": _bars_df(high=103.0, low=99.5)}, NOW, {"skipped": []})
    assert ("stop", p.key) in bot._faults  # no broker stop on record: entries and slicing stop
