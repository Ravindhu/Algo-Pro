"""Clock-skew check (flow item 17). Uses fake server times; no network."""
import pytest

from quant.audit.log import AuditLog
from quant.monitor import clock
from quant.notify.alerts import Alerts


def _fake_server(offset_seconds):
    return lambda: clock.time.time() - offset_seconds


def test_small_skew_is_ok_and_logged(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    alerts = Alerts()
    result = clock.check_clock(_fake_server(0.2), audit=audit, alerts=alerts)
    assert result["ok"] is True and result["status"] == "ok"
    assert audit.read_all()[-1]["kind"] == "clock_check"
    assert alerts.items[-1]["level"] == "INFO"


def test_large_skew_blocks_and_raises_error(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    alerts = Alerts()
    result = clock.check_clock(_fake_server(5.0), audit=audit, alerts=alerts)
    assert result["ok"] is False and result["status"] == "skewed"
    assert alerts.items[-1]["level"] == "ERROR"
    assert audit.read_all()[-1]["payload"]["ok"] is False


def test_unreachable_server_fails_closed(tmp_path):
    def broken():
        raise OSError("no route")

    alerts = Alerts()
    result = clock.check_clock(broken, alerts=alerts)
    assert result["ok"] is False and result["status"] == "unavailable"
    assert alerts.items[-1]["level"] == "WARNING"


def test_skew_inside_limit_is_ok_with_timing_noise():
    result = clock.check_clock(_fake_server(0.9), max_skew_seconds=1.0)
    assert result["ok"] is True
