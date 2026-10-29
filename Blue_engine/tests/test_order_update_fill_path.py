"""Regression tests for the live fill path (audit 2026-10-29, P0-4).

The POV slicer stops slicing when the durable ledger says the parent is
complete, so ``filled_quantity`` must advance from the BROKER's order-update
events. Before this test the handler read ``filled_quantity``/``status`` from a
feed that sends ``order_status`` = "open"|"complete"|"rejected"|"cancelled"|
"trigger pending" (OpenAlgo ``events/order_events.py`` and the broker adapters),
and ``normalize_status`` mapped both "Opened" and "rejected by broker" to
"unknown" -> "open". A live parent therefore never registered a fill and the
loop kept placing children until its duration expired.

Every payload below is a real OpenAlgo shape:
  * the event-bus relay ``subscribers/wsproxy_subscriber.py::on_order_update``
  * the ``OrderUpdateEvent`` dataclass
  * the SDK WebSocket ``order_update`` message
  * the ``postback`` blueprint (broker webhook)
"""
from __future__ import annotations

import pytest

from blue_engine.core import ledger, order_updates
from blue_engine.core.order_updates import normalize_status, parse_order_update


# --------------------------------------------------------------------------- #
# Status vocabulary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", ["Opened", "open", "OPEN", "open_pending",
                                 "open pending", "trigger_pending",
                                 "TRIGGER PENDING", "put order req received"])
def test_working_statuses_are_open(raw):
    assert normalize_status(raw) == "open"


@pytest.mark.parametrize("raw", ["COMPLETE", "complete", "Completed", "filled",
                                 "executed", "traded"])
def test_terminal_fills_are_filled(raw):
    assert normalize_status(raw) == "filled"


@pytest.mark.parametrize("raw", ["rejected by broker", "Rejected By Broker",
                                 "REJECTED", "rejected_by_rms", "error"])
def test_rejections_are_rejected(raw):
    assert normalize_status(raw) == "rejected"


@pytest.mark.parametrize("raw", ["Cancelled", "CANCELLED", "canceled"])
def test_cancels_are_cancelled(raw):
    assert normalize_status(raw) == "cancelled"


def test_unknown_status_fails_closed_to_open():
    # Never release a reservation on a state we cannot classify.
    assert normalize_status("something-new") == "open"
    assert normalize_status(None) == "unknown"
    assert normalize_status("") == "unknown"


# --------------------------------------------------------------------------- #
# Payload extraction
# --------------------------------------------------------------------------- #
def test_relay_no_orderid_is_ignored():
    assert parse_order_update({"type": "ping"}) is None
    assert parse_order_update({"type": "order_update", "orderid": ""}) is None


def test_relay_open_ack_carries_no_zero_price():
    parsed = parse_order_update({
        "type": "order_update", "user_id": "u1", "mode": "live",
        "broker": "zerodha", "orderid": "OA-1", "symbol": "INFY",
        "exchange": "NSE", "action": "BUY", "quantity": 100, "price": 1480.0,
        "trigger_price": 0.0, "pricetype": "LIMIT", "product": "CNC",
        "order_status": "open", "filled_quantity": 0, "pending_quantity": 100,
        "average_price": 0, "rejection_reason": "",
    })
    assert parsed == ("OA-1", "open", 0, None, "BUY")


def test_relay_partial_fill():
    parsed = parse_order_update({
        "type": "order_update", "orderid": "OA-1", "order_status": "open",
        "quantity": 100, "filled_quantity": 40, "pending_quantity": 60,
        "average_price": 1481.5, "action": "BUY",
    })
    assert parsed == ("OA-1", "open", 40, 1481.5, "BUY")


def test_relay_complete_fill():
    parsed = parse_order_update({
        "type": "order_update", "orderid": "OA-1", "order_status": "complete",
        "quantity": 100, "filled_quantity": 100, "pending_quantity": 0,
        "average_price": 1482.05, "action": "SELL",
    })
    assert parsed == ("OA-1", "filled", 100, 1482.05, "SELL")


def test_relay_rejection():
    parsed = parse_order_update({
        "type": "order_update", "orderid": "OA-1", "order_status": "rejected",
        "quantity": 100, "filled_quantity": 0, "average_price": 0,
        "rejection_reason": "RMS:insufficient funds", "action": "BUY",
    })
    assert parsed == ("OA-1", "rejected", 0, None, "BUY")


def test_sdk_message_without_filled_quantity_derives_it():
    # Some SDK payloads publish only quantity/pending_quantity.
    parsed = parse_order_update({
        "type": "order_update", "orderid": "OA-2", "order_status": "open",
        "quantity": 100, "pending_quantity": 70, "action": "BUY",
    })
    assert parsed[2] == 30


def test_pending_greater_than_quantity_cannot_prove_a_fill():
    parsed = parse_order_update({
        "orderid": "OA-3", "order_status": "open",
        "quantity": 10, "pending_quantity": 50,
    })
    assert parsed[2] == 0


def test_order_update_event_object():
    # Mirrors openalgo/events/order_events.py::OrderUpdateEvent's field names.
    class OrderUpdateEvent:
        mode = "live"
        orderid = "OA-4"
        symbol = "INFY"
        exchange = "NSE"
        action = "BUY"
        quantity = 50
        order_status = "complete"
        filled_quantity = 50
        pending_quantity = 0
        average_price = 1490.25
        request_data = {"user_id": "u1"}

    assert parse_order_update(OrderUpdateEvent()) == (
        "OA-4", "filled", 50, 1490.25, "BUY")


def test_camel_case_postback_fields():
    parsed = parse_order_update({
        "orderId": "OA-5", "orderStatus": "Complete", "filledQty": 7,
        "average_price": 99.5, "side": "sell",
    })
    assert parsed == ("OA-5", "filled", 7, 99.5, "SELL")


def test_missing_price_never_parses_to_zero():
    # float(0) must not leak into the slippage window as a real price.
    parsed = parse_order_update({
        "orderid": "OA-6", "order_status": "complete", "filled_quantity": 5,
        "average_price": 0,
    })
    assert parsed[3] is None


# --------------------------------------------------------------------------- #
# End-to-end: event -> ledger -> what the slicer reads
# --------------------------------------------------------------------------- #
def _fresh_memory_ledger():
    ledger._degrade_db(RuntimeError("test: in-memory ledger"))
    ledger._IN_MEMORY_EXECUTIONS.clear()


def test_complete_event_advances_the_filled_quantity_the_slicer_reads():
    _fresh_memory_ledger()
    ledger.create_execution(
        execution_id="POV-INFY-NSE-abc123", strategy="POV-INFY-NSE",
        symbol="INFY", exchange="NSE", action="BUY", total_quantity=100,
        mode="live", phase="accumulating")
    ledger.record_child(execution_id="POV-INFY-NSE-abc123", order_id="OA-7",
                        quantity=100, ltp=1480.0, status="open")

    # The broker's order ACK alone must NOT advance the fill.
    assert ledger.recover_filled_quantity("POV-INFY-NSE-abc123") == 0

    order_updates._on_update({
        "type": "order_update", "orderid": "OA-7", "order_status": "open",
        "quantity": 100, "filled_quantity": 60, "average_price": 1480.5,
        "action": "BUY"})
    assert ledger.recover_filled_quantity("POV-INFY-NSE-abc123") == 60

    # A replayed older event must not roll the fill backwards.
    order_updates._on_update({
        "type": "order_update", "orderid": "OA-7", "order_status": "open",
        "quantity": 100, "filled_quantity": 20, "average_price": 1480.5,
        "action": "BUY"})
    assert ledger.recover_filled_quantity("POV-INFY-NSE-abc123") == 60

    order_updates._on_update({
        "type": "order_update", "orderid": "OA-7", "order_status": "complete",
        "quantity": 100, "filled_quantity": 100, "pending_quantity": 0,
        "average_price": 1481.0, "action": "BUY"})
    assert ledger.recover_filled_quantity("POV-INFY-NSE-abc123") == 100


def test_event_for_an_unknown_order_does_not_raise():
    _fresh_memory_ledger()
    order_updates._on_update({
        "type": "order_update", "orderid": "NOT-OURS", "order_status": "complete",
        "filled_quantity": 10, "average_price": 100.0, "action": "BUY"})
