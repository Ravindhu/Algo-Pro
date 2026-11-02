from __future__ import annotations

import pytest

from blue_engine.backtest.loops import LiveMonitor
from blue_engine.core.live_monitor import LiveMonitorGuard
from blue_engine.portfolio import optim_policy


class PositionClient:
    def __init__(self, response=None, error=None):
        self.response = response or {
            "status": "success",
            "data": [{"netqty": 0, "ltp": 0}],
        }
        self.error = error

    def positionbook(self):
        if self.error is not None:
            raise self.error
        return self.response


def _telemetry(*, drawdown=False):
    equity = [100.0, 90.0] if drawdown else [100.0, 100.0]
    return {
        "live_equity": equity,
        "live_trade_pnls": [],
        "live_daily_returns": [0.0] * 10,
        "historical_daily_returns": [0.0] * 11,
    }


def _guard(client=None, provider=None):
    return LiveMonitorGuard(
        client=client or PositionClient(),
        monitor=LiveMonitor(
            {"max_dd": 1.0, "max_losing_streak": 3},
            baseline_source="verified_live"),
        provider=provider or (lambda **kwargs: _telemetry()),
        monitor_config={},
    )


def test_verified_live_inputs_authorize_child_order():
    guard = _guard()

    assert guard.authorize_order(
        phase="accumulating", quantity=25, action="BUY") == 25
    assert not optim_policy.kill_switch_state()["tripped"]


def test_monitor_trip_pauses_entries_but_allows_verified_exit():
    guard = _guard(provider=lambda **kwargs: _telemetry(drawdown=True))

    assert guard.authorize_order(
        phase="accumulating", quantity=25, action="BUY") == 0
    assert optim_policy.kill_switch_state()["tripped"]
    assert guard.authorize_order(
        phase="exiting", quantity=10, action="SELL") == 10


def test_observation_detects_threshold_breach_without_pending_child_order():
    guard = _guard(provider=lambda **kwargs: _telemetry(drawdown=True))

    assert guard.observe()
    assert optim_policy.kill_switch_state()["tripped"]
    assert guard.authorize_order(
        phase="accumulating", quantity=25, action="BUY") == 0


@pytest.mark.parametrize(
    "client",
    [
        PositionClient(error=RuntimeError("broker unavailable")),
        PositionClient(response={"status": "error", "data": []}),
        PositionClient(response={
            "status": "success", "data": [{"symbol": "INFY", "ltp": 100}]}),
        PositionClient(response={
            "status": "success",
            "data": [{"symbol": "INFY", "netqty": 10, "ltp": "bad"}]}),
    ],
)
def test_broker_or_position_verification_failure_latches_and_blocks(client):
    guard = _guard(client=client)

    assert guard.authorize_order(
        phase="accumulating", quantity=25, action="BUY") is None
    state = optim_policy.kill_switch_state()
    assert state["tripped"]
    assert "verification_failure" in state["reason"]


def test_live_telemetry_failure_latches_and_blocks_orders():
    guard = _guard(provider=lambda **kwargs: {"live_equity": []})

    assert guard.authorize_order(
        phase="accumulating", quantity=25, action="BUY") is None
    state = optim_policy.kill_switch_state()
    assert state["tripped"]
    assert "verification_failure" in state["reason"]


def test_empty_successful_positionbook_is_a_verified_flat_book():
    from blue_engine.core.risk_context import parse_positionbook

    result = parse_positionbook({"status": "success", "data": []})
    malformed = parse_positionbook({"status": "success", "data": [None]})

    assert result["ok"]
    assert result["open_exposure"] == 0.0
    assert result["n_positions"] == 0
    assert not malformed["ok"]


def test_persisted_kill_switch_blocks_new_entries():
    optim_policy.trip_kill_switch("operator safety halt")
    guard = _guard()

    assert guard.authorize_order(
        phase="accumulating", quantity=25, action="BUY") is None


def test_persisted_kill_switch_still_allows_a_verified_exit():
    # Halt policy (audit 2026-11-03): a halt stops new risk, not the exit
    # that reduces an open position. Verification still runs first.
    optim_policy.trip_kill_switch("operator safety halt")
    guard = _guard()

    assert guard.authorize_order(
        phase="exiting", quantity=10, action="SELL") == 10


def test_persisted_kill_switch_exit_still_needs_verified_inputs():
    optim_policy.trip_kill_switch("operator safety halt")
    guard = _guard(provider=lambda **kwargs: (_ for _ in ()).throw(
        RuntimeError("telemetry down")))

    assert guard.authorize_order(
        phase="exiting", quantity=10, action="SELL") is None


def test_guard_refuses_to_start_without_durable_kill_switch(monkeypatch):
    from blue_engine.core import kill_switch_store

    monkeypatch.setattr(kill_switch_store, "store_available", lambda: False)

    with pytest.raises(RuntimeError, match="durable kill-switch storage"):
        _guard()


def test_runner_installs_guard_on_the_live_slicer(monkeypatch):
    from blue_engine.core import runner
    from blue_engine.core import volume_cache

    client = PositionClient()
    authorized = []

    class Slicer:
        pre_order_check = None

        def execute(self, intent):
            assert self.monitor_observer()
            authorized.append(self.pre_order_check(
                phase="accumulating", quantity=intent["quantity"],
                action=intent["action"]))
            return {"child_orders": 0}

    slicer = Slicer()
    monkeypatch.setattr(runner, "init_client", lambda **kwargs: client)
    monkeypatch.setattr(runner, "read_mode", lambda _client: "analyze")
    monkeypatch.setattr(runner, "subscribe_to_order_updates",
                        lambda **kwargs: True)
    monkeypatch.setattr(volume_cache, "subscribe",
                        lambda **kwargs: True)
    monkeypatch.setattr(runner, "_build_slicer",
                        lambda name, params, _client: slicer)
    monkeypatch.setattr(
        runner, "_annotate_sme_flag",
        lambda _client, _symbol, _exchange, _cfg: False)

    raw = {
        "symbol": "INFY",
        "exchange": "NSE",
        "strategies": {"pov": True},
        "pov": {"action": "BUY", "quantity": 5},
        "live_monitor": {
            "enabled": True,
            "baseline_source": "verified_live",
            "historical_envelope": {
                "max_dd": 5.0, "max_losing_streak": 4},
        },
    }
    rc = runner.run_with_config(
        raw, monitor_inputs_provider=lambda **kwargs: _telemetry())

    assert rc == 0
    assert authorized == [5]


def test_runner_honors_persisted_trip_when_monitor_is_disabled(monkeypatch):
    from blue_engine.core import runner

    optim_policy.trip_kill_switch("previous monitor trip")
    monkeypatch.setattr(
        runner, "init_client",
        lambda **kwargs: pytest.fail("runner initialized while halted"))

    rc = runner.run_with_config({
        "symbol": "INFY",
        "exchange": "NSE",
        "strategies": {"pov": True},
        "pov": {"action": "BUY", "quantity": 5},
    })

    assert rc == 4


def test_runner_surfaces_monitor_trip_as_nonzero_status(monkeypatch):
    from blue_engine.core import runner
    from blue_engine.core import volume_cache

    class Slicer:
        def execute(self, intent):
            assert self.monitor_observer()
            assert self.pre_order_check(
                phase="accumulating", quantity=intent["quantity"],
                action=intent["action"]) == 0
            return {"child_orders": 0}

    monkeypatch.setattr(runner, "init_client",
                        lambda **kwargs: PositionClient())
    monkeypatch.setattr(runner, "read_mode", lambda _client: "analyze")
    monkeypatch.setattr(runner, "subscribe_to_order_updates",
                        lambda **kwargs: True)
    monkeypatch.setattr(volume_cache, "subscribe",
                        lambda **kwargs: True)
    monkeypatch.setattr(runner, "_build_slicer",
                        lambda name, params, _client: Slicer())
    monkeypatch.setattr(
        runner, "_annotate_sme_flag",
        lambda _client, _symbol, _exchange, _cfg: False)
    raw = {
        "symbol": "INFY",
        "exchange": "NSE",
        "strategies": {"pov": True},
        "pov": {"action": "BUY", "quantity": 5},
        "live_monitor": {
            "enabled": True,
            "baseline_source": "verified_live",
            "historical_envelope": {
                "max_dd": 5.0, "max_losing_streak": 4},
        },
    }

    rc = runner.run_with_config(
        raw, monitor_inputs_provider=lambda **kwargs: _telemetry(drawdown=True))

    assert rc == 4
