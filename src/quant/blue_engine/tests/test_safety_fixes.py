"""
Regression tests for the 2026-11-02 audit fixes (items 1-7).

* 1  DepthPricingGuard never crosses or touches the opposite quote.
* 2  A failed placeorder is reconciled with the broker before any retry.
* 3  A live run refuses to start when the kill-switch store is degraded.
* 5  Stale-LIMIT cancels touch only this run's orders and honour expiry.
* 6  CircuitGuard fails closed when the LTP is unavailable.
* 7  RiskGate requires a monetary profile for live runs by default.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from blue_engine.core import kill_switch_store as ks_store
from blue_engine.core.risk_gate import RiskGate
from blue_engine.pov.slicers.guards import (
    CircuitGuard,
    DepthPricingGuard,
    TopOfBook,
)
from blue_engine.pov.slicers.pov import POVSlicer


# ---------------------------------------------------------------- item 1
@pytest.mark.parametrize("offset_ticks", [0, 1, 2])
def test_depth_pricing_never_crosses_or_touches_opposite_quote(offset_ticks):
    guard = DepthPricingGuard()
    tick = 0.05
    for bid_steps in range(0, 200):
        for spread_ticks in (1, 2, 3, 4, 5):
            bid = round(100 + bid_steps * tick, 2)
            ask = round(bid + spread_ticks * tick, 2)
            top = TopOfBook(best_bid=bid, best_ask=ask, tick_size=tick)
            buy = guard.evaluate(top, action="BUY",
                                 cfg={"depth_offset_ticks": offset_ticks}).new_price
            sell = guard.evaluate(top, action="SELL",
                                  cfg={"depth_offset_ticks": offset_ticks}).new_price
            assert bid <= buy < ask, (bid, ask, buy)
            assert bid < sell <= ask, (bid, ask, sell)


# ---------------------------------------------------------------- item 6
def test_circuit_guard_fails_closed_without_ltp():
    decision = CircuitGuard().evaluate(
        TopOfBook(best_bid=99.0, best_ask=99.1, upper_circuit=110.0,
                  lower_circuit=90.0),
        action="BUY", cfg={})
    assert decision.pause_reason == "MARKET_DATA_UNAVAILABLE"


def test_circuit_guard_fail_open_is_explicit_opt_in():
    decision = CircuitGuard().evaluate(
        TopOfBook(best_bid=99.0, best_ask=99.1), action="BUY",
        cfg={"circuit_fail_closed": False})
    assert decision.pause_reason is None


# ---------------------------------------------------------------- item 7
def test_live_ack_is_required_by_default():
    assert RiskGate({}).require_live_ack is True


def test_live_ack_can_still_be_disabled_explicitly():
    assert RiskGate({"require_live_ack": False}).require_live_ack is False


# ---------------------------------------------------------------- item 3
def test_require_durable_raises_when_store_degraded(monkeypatch):
    monkeypatch.setattr(ks_store, "_DEGRADED", True)
    with pytest.raises(RuntimeError, match="would not survive a restart"):
        ks_store.require_durable()


def test_require_durable_passes_on_healthy_store():
    ks_store.require_durable()  # conftest points the store at a temp DB


# ---------------------------------------------------------------- item 2
class _FakeClient:
    def __init__(self, rows=None, raise_on_orderbook=False):
        self._rows = rows or []
        self._raise = raise_on_orderbook

    def orderbook(self):
        if self._raise:
            raise TimeoutError("orderbook down")
        return {"status": "success", "data": {"orders": self._rows}}


def _slicer():
    s = POVSlicer.__new__(POVSlicer)
    s.log = lambda *_a, **_k: None
    return s


_ROW = {"orderid": "B1", "symbol": "INFY", "exchange": "NSE",
        "strategy": "POV-INFY", "action": "BUY", "quantity": 50}


def _reconcile(slicer, client, known=frozenset()):
    return slicer._reconcile_failed_placement(
        client=client, symbol="INFY", exchange="NSE", strategy_label="POV-INFY",
        action="BUY", quantity=50, known_oids=set(known))


def test_reconcile_finds_order_the_broker_accepted():
    assert _reconcile(_slicer(), _FakeClient([_ROW])) == ("placed", "B1")


def test_reconcile_ignores_orders_this_run_already_recorded():
    assert _reconcile(_slicer(), _FakeClient([_ROW]), known={"B1"}) == ("absent", None)


def test_reconcile_reports_absent_when_no_match():
    assert _reconcile(_slicer(), _FakeClient([])) == ("absent", None)


def test_reconcile_halts_when_broker_state_unreadable():
    assert _reconcile(_slicer(), _FakeClient(raise_on_orderbook=True)) == ("unknown", None)


# ---------------------------------------------------------------- item 5
class _CancelClient:
    def __init__(self, rows):
        self._rows = rows
        self.cancelled = []

    def orderbook(self):
        return {"status": "success", "data": {"orders": self._rows}}

    def cancelorder(self, *, order_id, strategy):
        self.cancelled.append(order_id)
        return {"status": "success"}


def _open_limit(oid):
    return {"orderid": oid, "symbol": "INFY", "exchange": "NSE",
            "strategy": "POV-INFY", "pricetype": "LIMIT",
            "order_status": "open"}


def test_cancel_touches_only_orders_this_run_placed():
    client = _CancelClient([_open_limit("MINE"), _open_limit("OTHER")])
    _slicer()._cancel_stale_limits(
        client=client, symbol="INFY", exchange="NSE", strategy_label="POV-INFY",
        limit_expiry_pct=80.0, allowed_strategy_labels={"POV-INFY"},
        own_oids={"MINE": datetime.now() - timedelta(seconds=60)}, min_age_sec=0.0)
    assert client.cancelled == ["MINE"]


def test_cancel_is_a_no_op_without_own_orders():
    client = _CancelClient([_open_limit("OTHER")])
    _slicer()._cancel_stale_limits(
        client=client, symbol="INFY", exchange="NSE", strategy_label="POV-INFY",
        limit_expiry_pct=80.0, allowed_strategy_labels={"POV-INFY"},
        own_oids=None)
    assert client.cancelled == []


def test_cancel_honours_minimum_resting_age():
    client = _CancelClient([_open_limit("FRESH")])
    _slicer()._cancel_stale_limits(
        client=client, symbol="INFY", exchange="NSE", strategy_label="POV-INFY",
        limit_expiry_pct=80.0, allowed_strategy_labels={"POV-INFY"},
        own_oids={"FRESH": datetime.now()}, min_age_sec=30.0)
    assert client.cancelled == []
