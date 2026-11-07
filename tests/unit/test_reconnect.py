"""Backoff reconnect (flow item 4). Sleep and connect are fakes."""
from quant.data.reconnect import backoff_delays, reconnect
from quant.notify.alerts import Alerts


def test_delays_double_and_cap():
    assert backoff_delays(base=1, factor=2, cap=5, attempts=5) == [1, 2, 4, 5, 5]


def test_succeeds_after_two_failures_and_sleeps_between():
    calls, sleeps = [], []

    def connect():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("down")

    ok = reconnect(connect, sleeps.append, delays=[1.0, 2.0, 4.0])
    assert ok is True
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_gives_up_and_raises_error_alert():
    alerts = Alerts()

    def connect():
        raise ConnectionError("down")

    ok = reconnect(connect, lambda s: None, delays=[1.0, 1.0], alerts=alerts)
    assert ok is False
    assert alerts.items[-1]["level"] == "ERROR"
    assert sum(1 for i in alerts.items if i["level"] == "WARNING") == 2


def test_first_try_success_does_not_sleep():
    sleeps = []
    assert reconnect(lambda: None, sleeps.append, delays=[1.0]) is True
    assert sleeps == []
