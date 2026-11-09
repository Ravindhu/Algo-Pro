"""Kill switch store used by the dashboard API (decision 0020). No FastAPI needed."""
import pytest

from quant.audit.log import AuditLog
from quant.risk.kill_switch import KillSwitch


def test_trip_sets_state_and_survives_restart(tmp_path):
    path = tmp_path / "kill.json"
    KillSwitch(path).trip("manual stop", source="dashboard-api")
    again = KillSwitch(path)
    assert again.active is True
    assert again.reason == "dashboard-api: manual stop"


def test_reset_with_audit_clears_and_writes_override(tmp_path):
    path, audit_path = tmp_path / "kill.json", tmp_path / "audit.jsonl"
    switch = KillSwitch(path)
    switch.trip("manual stop", source="dashboard-api")
    switch.reset("dashboard-api", "checked", audit=AuditLog(audit_path))
    assert KillSwitch(path).active is False
    assert "kill_switch_reset" in audit_path.read_text(encoding="utf-8")


def test_reset_without_audit_needs_actor_and_reason(tmp_path):
    switch = KillSwitch(tmp_path / "kill.json")
    switch.trip("x", source="automatic")
    with pytest.raises(ValueError):
        switch.reset("", "")
    assert switch.active is True
