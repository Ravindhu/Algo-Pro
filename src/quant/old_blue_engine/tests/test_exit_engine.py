"""Exit engine tests (audit 2026-10-29, P0-2: unprotected positions).

Every test drives the engine through its public ``run_cycle`` with a stub
broker that behaves like the OpenAlgo SDK: ``positionbook()`` / ``orderbook()``
/ ``placeorder()`` / ``cancelorder()`` returning ``{"status": "success", ...}``.
The safety properties under test are the ones a live account depends on:

* a filled position gets a broker-side SL-M on the opposite side;
* an unverifiable book is never reported as "flat" or "protected";
* a stop that is already marketable is not placed (it would exit instantly);
* a failed placement is CRITICAL, not a warning;
* a closed position's leftover stop is cancelled;
* a tripped kill switch cancels stops and never places new ones;
* market flattening only happens when explicitly armed.
"""
from __future__ import annotations

import pytest

from blue_engine.core.exit_engine import (
    ExitConfig,
    ExitEngine,
    protect_positions,
    protecting,
)


class StubBroker:
    def __init__(self, positions=None, orders=None, *, pos_error=False,
                 place_fails=False, place_status="open"):
        self.positions = positions if positions is not None else []
        self.orders = orders if orders is not None else []
        self.pos_error = pos_error
        self.place_fails = place_fails
        self.place_status = place_status
        self.placed = []
        self.cancelled = []
        self._next = 1

    def positionbook(self):
        if self.pos_error:
            raise RuntimeError("broker down")
        return {"status": "success", "data": self.positions}

    def orderbook(self):
        return {"status": "success", "data": self.orders}

    def placeorder(self, **kwargs):
        self.placed.append(kwargs)
        if self.place_fails:
            return {"status": "error", "message": "RMS rejected"}
        oid = f"O{self._next}"
        self._next += 1
        return {"status": "success", "order_id": oid,
                "data": {"order_status": self.place_status}}

    def cancelorder(self, *, order_id):
        self.cancelled.append(order_id)
        return {"status": "success"}


class StubRegistry:
    def __init__(self, records=None):
        self.records = records or []

    def snapshot(self):
        return list(self.records)


def _pos(symbol="INFY", exchange="NSE", qty=100, price=1480.0):
    return {"symbol": symbol, "exchange": exchange, "netqty": qty,
            "average_price": price, "ltp": price, "product": "CNC"}


def _engine(broker, *, registry=None, kill=False, **cfg):
    config = ExitConfig(**cfg)
    return ExitEngine(broker, config, registry=registry,
                      kill_check=(lambda: kill) if not callable(kill) else kill,
                      log=lambda _m: None)


# --------------------------------------------------------------------------- #
# Placement
# --------------------------------------------------------------------------- #
def test_long_position_gets_a_sell_stop_below_market():
    broker = StubBroker([_pos(qty=100, price=1480.0)])
    reg = StubRegistry([{"symbol": "INFY", "exchange": "NSE",
                         "stop_loss": 1450.0, "action": "BUY",
                         "registered_at": "2026-10-29T09:20:00"}])
    report = _engine(broker, registry=reg).run_cycle()

    assert report["critical"] is False
    assert len(broker.placed) == 1
    order = broker.placed[0]
    assert order["action"] == "SELL", "a long position exits by selling"
    assert order["price_type"] == "SL-M"
    assert order["quantity"] == 100
    assert order["trigger_price"] == pytest.approx(1450.0)
    assert order["strategy"] == "POV-EXIT"
    assert protecting(report) is True


def test_short_position_gets_a_buy_stop_above_market():
    broker = StubBroker([_pos(qty=-75, price=1480.0)])
    reg = StubRegistry([{"symbol": "INFY", "exchange": "NSE",
                         "stop_loss": 1500.0, "action": "SELL"}])
    report = _engine(broker, registry=reg).run_cycle()
    order = broker.placed[0]
    assert order["action"] == "BUY"
    assert order["quantity"] == 75
    assert order["trigger_price"] > 1480.0
    assert protecting(report) is True


def test_trigger_is_tick_aligned():
    broker = StubBroker([_pos(price=1480.0)])
    reg = StubRegistry([{"symbol": "INFY", "exchange": "NSE", "stop_loss": 1450.07}])
    _engine(broker, registry=reg, tick_size=0.05).run_cycle()
    trig = broker.placed[0]["trigger_price"]
    assert abs(trig / 0.05 - round(trig / 0.05)) < 1e-9


def test_existing_working_stop_is_reused_not_duplicated():
    broker = StubBroker(
        [_pos()],
        orders=[{"symbol": "INFY", "exchange": "NSE", "strategy": "POV-EXIT",
                 "orderid": "EXISTING", "order_status": "trigger pending"}])
    reg = StubRegistry([{"symbol": "INFY", "exchange": "NSE", "stop_loss": 1450.0}])
    report = _engine(broker, registry=reg).run_cycle()
    assert broker.placed == []
    assert report["protected"][0]["order_id"] == "EXISTING"
    assert protecting(report) is True


def test_another_strategys_stop_does_not_count():
    # A sibling strategy's order must not be mistaken for our protection.
    broker = StubBroker(
        [_pos()],
        orders=[{"symbol": "INFY", "exchange": "NSE", "strategy": "SOMETHING-ELSE",
                 "orderid": "X1", "order_status": "trigger pending"}])
    reg = StubRegistry([{"symbol": "INFY", "exchange": "NSE", "stop_loss": 1450.0}])
    _engine(broker, registry=reg).run_cycle()
    assert len(broker.placed) == 1


# --------------------------------------------------------------------------- #
# Failure modes must fail CLOSED
# --------------------------------------------------------------------------- #
def test_unverifiable_positionbook_is_critical_and_never_flat():
    broker = StubBroker(pos_error=True)
    report = _engine(broker, registry=StubRegistry()).run_cycle()
    assert report["critical"] is True
    assert report["checked"] == 0
    assert report["errors"]
    assert protecting(report) is False, "unknown must never read as protected"


def test_marketable_stop_is_not_placed():
    # A "stop" above market for a long would fire instantly: refuse it.
    broker = StubBroker([_pos(price=1480.0)])
    reg = StubRegistry([{"symbol": "INFY", "exchange": "NSE", "stop_loss": 1500.0}])
    report = _engine(broker, registry=reg).run_cycle()
    assert broker.placed == []
    assert report["critical"] is True
    assert any("above market" in w for w in report["warnings"])


def test_failed_placement_is_critical():
    broker = StubBroker([_pos()], place_fails=True)
    reg = StubRegistry([{"symbol": "INFY", "exchange": "NSE", "stop_loss": 1450.0}])
    report = _engine(broker, registry=reg).run_cycle()
    assert report["critical"] is True
    assert report["errors"]
    assert protecting(report) is False


def test_immediately_dead_exit_order_is_critical():
    broker = StubBroker([_pos()], place_status="rejected")
    reg = StubRegistry([{"symbol": "INFY", "exchange": "NSE", "stop_loss": 1450.0}])
    report = _engine(broker, registry=reg).run_cycle()
    assert report["critical"] is True
    assert protecting(report) is False


def test_no_level_and_no_default_is_unprotected_and_critical():
    broker = StubBroker([_pos()])
    report = _engine(broker, registry=StubRegistry(),
                     default_stop_pct=None).run_cycle()
    assert broker.placed == []
    assert report["unprotected"]
    assert report["critical"] is True
    assert protecting(report) is False


def test_default_percent_stop_is_used_and_labelled():
    broker = StubBroker([_pos(price=1000.0)])
    report = _engine(broker, registry=StubRegistry(),
                     default_stop_pct=5.0).run_cycle()
    assert broker.placed[0]["trigger_price"] == pytest.approx(950.0)
    assert "default" in report["placed"][0]["source"]


def test_no_flatten_without_explicit_opt_in():
    broker = StubBroker([_pos()])
    report = _engine(broker, registry=StubRegistry(), default_stop_pct=None,
                     flatten_unprotected=True).run_cycle()
    assert broker.placed == []
    assert "allow_market_exit is false" in " ".join(report["warnings"])


def test_flatten_happens_only_when_armed():
    broker = StubBroker([_pos()])
    report = _engine(broker, registry=StubRegistry(), default_stop_pct=None,
                     flatten_unprotected=True,
                     allow_market_exit=True).run_cycle()
    assert broker.placed and broker.placed[0]["price_type"] == "MARKET"
    assert report["actions"]


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def test_closed_position_cancels_its_stop():
    broker = StubBroker([_pos()])
    reg = StubRegistry([{"symbol": "INFY", "exchange": "NSE", "stop_loss": 1450.0}])
    engine = _engine(broker, registry=reg)
    engine.run_cycle()
    assert len(broker.placed) == 1

    # Position closed elsewhere; the orphaned stop must be cancelled.
    broker.positions = []
    broker.orders = []
    report = engine.run_cycle()
    assert broker.cancelled == ["O1"]
    assert report["cancelled"]


def test_kill_switch_cancels_and_never_places():
    broker = StubBroker([_pos()])
    reg = StubRegistry([{"symbol": "INFY", "exchange": "NSE", "stop_loss": 1450.0}])
    engine = _engine(broker, registry=reg)
    engine.run_cycle()
    broker.placed.clear()

    broker_kill = _engine(broker, registry=reg, kill=True)
    broker_kill._stops = dict(engine._stops)   # simulate a restart with memo
    report = broker_kill.run_cycle()
    assert report["kill_tripped"] is True
    assert broker.placed == []
    assert broker.cancelled == ["O1"]


def test_broken_kill_probe_is_critical_not_a_pass():
    broker = StubBroker([_pos()])
    reg = StubRegistry([{"symbol": "INFY", "exchange": "NSE", "stop_loss": 1450.0}])

    def _boom():
        raise RuntimeError("probe failed")

    engine = ExitEngine(broker, ExitConfig(), registry=reg,
                        kill_check=_boom, log=lambda _m: None)
    report = engine.run_cycle()
    assert report["critical"] is True


def test_unverifiable_orderbook_trusts_the_local_memo():
    # The orderbook is down: we must not stack a second stop for a position
    # we already protected this session.
    broker = StubBroker([_pos()])
    reg = StubRegistry([{"symbol": "INFY", "exchange": "NSE", "stop_loss": 1450.0}])
    engine = _engine(broker, registry=reg)
    engine.run_cycle()
    assert len(broker.placed) == 1

    broker.orderbook = lambda: (_ for _ in ()).throw(RuntimeError("down"))
    report = engine.run_cycle()
    assert len(broker.placed) == 1, "must not duplicate the stop"
    assert report["protected"][0]["source"] == "memo"


def test_multi_symbol_cycle_covers_every_open_position():
    broker = StubBroker([_pos("INFY"), _pos("TCS", qty=20, price=3500.0)])
    reg = StubRegistry([{"symbol": "INFY", "exchange": "NSE", "stop_loss": 1450.0},
                        {"symbol": "TCS", "exchange": "NSE", "stop_loss": 3400.0}])
    report = _engine(broker, registry=reg).run_cycle()
    assert report["checked"] == 2
    assert {p["symbol"] for p in report["protected"]} == {"INFY", "TCS"}
    assert protecting(report) is True


def test_zero_quantity_rows_are_ignored():
    broker = StubBroker([_pos(qty=0)])
    report = _engine(broker, registry=StubRegistry()).run_cycle()
    assert report["checked"] == 0
    assert broker.placed == []


def test_run_forever_stops_after_max_cycles():
    broker = StubBroker([])
    engine = _engine(broker, registry=StubRegistry(), check_interval_seconds=1.0)
    seen = []
    report = engine.run_forever(on_report=seen.append, max_cycles=2)
    assert isinstance(report, dict)
    assert len(seen) == 2


def test_disabled_engine_does_not_trade():
    broker = StubBroker([_pos()])
    report = _engine(broker, registry=StubRegistry(), enabled=False).run_cycle()
    assert broker.placed == []
    assert "disabled" in " ".join(report["warnings"])


def test_protect_positions_wrapper():
    broker = StubBroker([_pos()])
    reg = StubRegistry([{"symbol": "INFY", "exchange": "NSE", "stop_loss": 1450.0}])
    report = protect_positions(broker, config={"default_stop_pct": 3.0},
                               registry=reg, log=lambda _m: None)
    assert report["placed"]
    assert report["critical"] is False


# --------------------------------------------------------------------------- #
# Wiring: runner._run_exit_engine and ScreenerBridge._protect_after_fill
# --------------------------------------------------------------------------- #
def test_runner_skips_when_exit_engine_is_absent(capsys):
    import blue_engine.core.runner as runner
    broker = StubBroker([_pos()])
    assert runner._run_exit_engine(broker, {}, filled=100) is False
    assert broker.placed == []
    assert "UNPROTECTED" in capsys.readouterr().out


def test_runner_does_nothing_when_the_entry_did_not_fill():
    import blue_engine.core.runner as runner
    broker = StubBroker([_pos()])
    status = runner._run_exit_engine(
        broker, {"exit_engine": {"enabled": True}}, filled=0)
    assert status is False
    assert broker.placed == []


def test_runner_critical_report_trips_the_kill_switch(monkeypatch):
    import blue_engine.core.runner as runner
    import blue_engine.portfolio.optim_policy as policy

    tripped = {}
    monkeypatch.setattr(policy, "peek_kill_switch", lambda: {"tripped": False})
    monkeypatch.setattr(policy, "trip_kill_switch",
                        lambda reason, **k: tripped.setdefault("reason", reason))

    # The broker cannot be read -> the engine must fail closed.
    broker = StubBroker(pos_error=True)
    status = runner._run_exit_engine(
        broker, {"exit_engine": {"enabled": True, "supervise_seconds": 0}},
        filled=100)
    assert status is True
    assert "reason" in tripped


def test_runner_happy_path_returns_clean(monkeypatch):
    import blue_engine.core.runner as runner
    import blue_engine.core.protective_orders as registry
    import blue_engine.portfolio.optim_policy as policy

    monkeypatch.setattr(policy, "peek_kill_switch", lambda: {"tripped": False})
    registry._LEVELS.clear()
    registry.register("EX1", symbol="INFY", exchange="NSE", action="BUY",
                      stop_loss=1450.0, target=1600.0, persist=False)
    broker = StubBroker([_pos()])
    status = runner._run_exit_engine(
        broker, {"exit_engine": {"enabled": True, "supervise_seconds": 0,
                                 "default_stop_pct": 3.0}}, filled=100)
    registry._LEVELS.clear()
    assert status is False
    assert len(broker.placed) == 1


def test_runner_rejects_a_non_object_exit_block():
    import blue_engine.core.runner as runner
    broker = StubBroker([_pos()])
    assert runner._run_exit_engine(broker, {"exit_engine": "yes"}, filled=10) is True
    assert broker.placed == []


def test_bridge_warns_loudly_when_no_exit_config(monkeypatch):
    import blue_engine.core.screener_bridge as module
    monkeypatch.setattr(module, "init_client", lambda **kw: StubBroker())
    messages = []
    bridge = module.ScreenerBridge(symbols=["INFY"], log=messages.append)
    bridge._protect_after_fill("INFY", "NSE")
    assert any("UNPROTECTED" in m for m in messages)


def test_bridge_protects_after_fill(monkeypatch):
    import blue_engine.core.protective_orders as registry
    import blue_engine.core.screener_bridge as module
    broker = StubBroker([_pos()])
    monkeypatch.setattr(module, "init_client", lambda **kw: broker)
    monkeypatch.setattr(module.ScreenerBridge, "kill_tripped", lambda self: False)
    registry._LEVELS.clear()
    registry.register("EX2", symbol="INFY", exchange="NSE", action="BUY",
                      stop_loss=1450.0, target=None, persist=False)
    bridge = module.ScreenerBridge(
        symbols=["INFY"], exit_config={"default_stop_pct": 3.0},
        log=lambda _m: None)
    bridge._protect_after_fill("INFY", "NSE")
    registry._LEVELS.clear()
    assert len(broker.placed) == 1
    assert broker.placed[0]["action"] == "SELL"
    assert bridge._protection_failed is False


def test_bridge_marks_protection_failure(monkeypatch):
    import blue_engine.core.screener_bridge as module
    broker = StubBroker(pos_error=True)
    monkeypatch.setattr(module, "init_client", lambda **kw: broker)
    monkeypatch.setattr(module.ScreenerBridge, "kill_tripped", lambda self: False)
    bridge = module.ScreenerBridge(
        symbols=["INFY"], exit_config={"default_stop_pct": 3.0},
        log=lambda _m: None)
    bridge._protect_after_fill("INFY", "NSE")
    assert bridge._protection_failed is True
