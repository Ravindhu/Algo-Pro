"""Exit retry (flow item 6). Retry only when broker shows no open order AND the position is still open."""
from quant.core.types import TradeIntent
from quant.execution.retry import retry_exit
from quant.notify.alerts import Alerts


def exit_intent():
    return TradeIntent("X1", "S1", "INFY", "SELL", 100, 1500.0, kind="exit")


def unknown_no_id():
    return {"order_id": "", "status": "UNKNOWN", "filled_qty": 0, "avg_price": None, "reason": "timeout"}


def filled():
    return {"order_id": "99", "status": "FILLED", "filled_qty": 100, "avg_price": 1500.0, "reason": ""}


def test_definite_rejection_is_not_retried():
    calls = []
    rejected = {"order_id": "", "status": "REJECTED", "filled_qty": 0, "avg_price": None, "reason": "no"}

    def send(i, d):
        calls.append(1)
        return rejected

    out = retry_exit(send, exit_intent(), None, order_not_placed=lambda i: True,
                     position_still_open=lambda i: True, sleep=lambda s: None)
    assert out["status"] == "REJECTED" and len(calls) == 1


def test_order_that_exists_is_never_resent():
    calls = []
    existing = {"order_id": "77", "status": "UNKNOWN", "filled_qty": 0, "avg_price": None, "reason": "open"}

    def send(i, d):
        calls.append(1)
        return existing

    alerts = Alerts()
    out = retry_exit(send, exit_intent(), None, order_not_placed=lambda i: True,
                     position_still_open=lambda i: True, sleep=lambda s: None, alerts=alerts)
    assert out["order_id"] == "77" and len(calls) == 1
    assert alerts.items == []  # the order exists: no retry, no CRITICAL needed


def test_no_readers_means_no_retry_and_critical_alert():
    calls = []

    def send(i, d):
        calls.append(1)
        return unknown_no_id()

    alerts = Alerts()
    out = retry_exit(send, exit_intent(), None, sleep=lambda s: None, alerts=alerts)
    assert out["status"] == "UNKNOWN" and len(calls) == 1
    assert alerts.items[-1]["level"] == "CRITICAL"


def test_retry_succeeds_when_broker_confirms_safe():
    seq = [unknown_no_id(), filled()]
    calls, sleeps = [], []

    def send(i, d):
        calls.append(1)
        return seq.pop(0)

    out = retry_exit(send, exit_intent(), None, order_not_placed=lambda i: True,
                     position_still_open=lambda i: True, sleep=sleeps.append)
    assert out["status"] == "FILLED"
    assert len(calls) == 2
    assert sleeps == [2]  # first retry waits 2 seconds


def test_position_already_closed_stops_retry():
    calls = []

    def send(i, d):
        calls.append(1)
        return unknown_no_id()

    alerts = Alerts()
    out = retry_exit(send, exit_intent(), None, order_not_placed=lambda i: True,
                     position_still_open=lambda i: False, sleep=lambda s: None, alerts=alerts)
    assert len(calls) == 1 and out["status"] == "UNKNOWN"
    assert alerts.items[-1]["level"] == "CRITICAL"


def test_all_attempts_fail_raise_critical_after_three_sends():
    calls = []

    def send(i, d):
        calls.append(1)
        return unknown_no_id()

    alerts = Alerts()
    out = retry_exit(send, exit_intent(), None, order_not_placed=lambda i: True,
                     position_still_open=lambda i: True, sleep=lambda s: None, attempts=3, alerts=alerts)
    assert out["status"] == "UNKNOWN"
    assert len(calls) == 3
    assert alerts.items[-1]["level"] == "CRITICAL"
