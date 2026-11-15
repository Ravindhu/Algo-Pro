"""Dashboard wiring (2026-11-15): one STOP reaches every engine, the setup panel saves the live
cap and IP, and the live readiness list is owner-only.

Runs only where FastAPI is installed (the owner machine and this repo's test environment).
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dashboard"))

fastapi = pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def owner_client(tmp_path, monkeypatch):
    import time as clock
    from app import create_app
    from quant.core import dashboard_accounts as accounts

    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-secret")
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("blocked: false\nmax_daily_loss: null\n", encoding="utf-8")
    settings = tmp_path / "settings.yaml"
    settings.write_text("sizing:\n  risk_per_trade: 0.005\n", encoding="utf-8")
    holidays = tmp_path / "holidays.csv"
    holidays.write_text("date,exchange\n", encoding="utf-8")
    users = tmp_path / "users.json"
    accounts.create_user("Owner", "owner-password-1", users, now=clock.time(), role="owner")
    accounts.create_user("Peon", "viewer-password", users, now=clock.time(), role="viewer")
    paths = {"ledger": tmp_path / "l.jsonl", "audit": tmp_path / "a.jsonl",
             "kill": tmp_path / "k.json", "bot_kill": tmp_path / "bk.json", "live_kill": tmp_path / "lk.json",
             "limits": limits, "settings": settings, "strategies": tmp_path / "strategies",
             "users": users, "session_secret": tmp_path / "secret.key",
             "mode_state": tmp_path / "mode.json", "live_approval": tmp_path / "approval.json",
             "holidays": holidays}
    client = testclient.TestClient(create_app(paths))
    res = client.post("/api/account/login", json={"name": "Owner", "password": "owner-password-1"})
    assert res.status_code == 200, res.text
    return client, paths


def test_one_stop_trips_all_three_engines_and_one_reset_clears_them(owner_client):
    client, paths = owner_client
    r = client.post("/api/kill/trip", json={"reason": "manual stop"})
    assert r.status_code == 200 and r.json()["active"] is True
    for key in ("kill", "bot_kill", "live_kill"):
        assert json.loads(paths[key].read_text(encoding="utf-8"))["active"] is True, key
    assert client.post("/api/kill/reset", json={"reason": "checked"}).status_code == 200
    for key in ("kill", "bot_kill", "live_kill"):
        assert json.loads(paths[key].read_text(encoding="utf-8"))["active"] is False, key


def test_status_is_on_when_only_the_live_bot_file_is_on(owner_client):
    client, paths = owner_client
    paths["live_kill"].write_text(json.dumps({"active": True, "reason": "live only"}), encoding="utf-8")
    body = client.get("/api/kill").json()
    assert body["active"] is True and "live only" in body["reason"]


def test_setup_saves_live_cap_and_ip_to_settings(owner_client):
    client, paths = owner_client
    r = client.post("/api/setup", json={"live_max_capital_inr": 250000, "live_allowed_egress_ip": "203.0.113.10"})
    assert r.status_code == 200, r.text
    text = paths["settings"].read_text(encoding="utf-8")
    assert "max_capital_inr: 250000" in text and "allowed_egress_ip: 203.0.113.10" in text
    read = client.get("/api/setup").json()
    assert read["live_max_capital_inr"] == 250000 and read["live_allowed_egress_ip"] == "203.0.113.10"
    assert "risk_per_trade: 0.005" in text   # other keys are kept


@pytest.mark.parametrize("body", [
    {"live_max_capital_inr": 0}, {"live_max_capital_inr": -5}, {"live_max_capital_inr": True},
    {"live_allowed_egress_ip": "not-an-ip"}, {"live_allowed_egress_ip": "300.1.1.1"},
])
def test_setup_refuses_bad_live_values(owner_client, body):
    client, _ = owner_client
    assert client.post("/api/setup", json=body).status_code == 422


def test_live_status_is_owner_only_and_lists_what_is_missing(owner_client):
    client, _ = owner_client
    assert client.get("/api/live/status").status_code == 200       # signed in as Owner
    body = client.get("/api/live/status").json()
    assert body["ready"] is False
    text = " | ".join(body["reasons"])
    assert "allowed_egress_ip" in text and "max_capital_inr" in text and "book-level" in text


def test_live_status_refuses_a_viewer(tmp_path, monkeypatch):
    import time as clock
    from app import create_app
    from quant.core import dashboard_accounts as accounts

    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-secret")
    users = tmp_path / "users.json"
    accounts.create_user("Peon", "viewer-password", users, now=clock.time(), role="viewer")
    paths = {"users": users, "session_secret": tmp_path / "s.key", "audit": tmp_path / "a.jsonl",
             "kill": tmp_path / "k.json", "bot_kill": tmp_path / "bk.json", "live_kill": tmp_path / "lk.json"}
    client = testclient.TestClient(create_app(paths))
    client.post("/api/account/login", json={"name": "Peon", "password": "viewer-password"})
    assert client.get("/api/live/status").status_code in (401, 403)
