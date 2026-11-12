"""FastAPI routes. Runs only where FastAPI is installed (owner machine). Read-only: no POST routes."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dashboard"))

fastapi = pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def client(tmp_path):
    from app import create_app

    paths = {
        "ledger": tmp_path / "ledger.jsonl", "audit": tmp_path / "audit.jsonl",
        "kill": tmp_path / "kill.json", "limits": tmp_path / "none.yaml",
        "settings": tmp_path / "none.yaml",
    }
    return testclient.TestClient(create_app(paths))


def test_overview_and_health(client):
    assert client.get("/api/overview").status_code == 200
    h = client.get("/api/health").json()
    assert h["feed"]["status"] == "not connected"


def test_index_is_api_only(client):
    body = client.get("/").json()
    assert "/api/overview" in body["endpoints"]
    assert "<html" not in client.get("/").text.lower()


def test_terminal_page_is_removed():
    assert not (ROOT / "dashboard" / "assets" / "terminal.html").exists()


def test_no_write_routes(client):
    for method in ("post", "put", "delete"):
        assert getattr(client, method)("/api/overview").status_code in (404, 405)


@pytest.fixture
def write_client(tmp_path, monkeypatch):
    """Client with NO session. Tests sign in as Owner or Peon (viewer) when they need to."""
    import time as clock

    from app import create_app
    from quant.core import dashboard_accounts as accounts

    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-secret")
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("blocked: false\nmax_daily_loss: null\n", encoding="utf-8")
    users = tmp_path / "users.json"
    accounts.create_user("Owner", "owner-password-1", users, now=clock.time(), role="owner")
    accounts.create_user("Peon", "viewer-password", users, now=clock.time(), role="viewer")
    paths = {"ledger": tmp_path / "l.jsonl", "audit": tmp_path / "a.jsonl",
             "kill": tmp_path / "k.json", "limits": limits, "settings": tmp_path / "none.yaml",
             "users": users, "session_secret": tmp_path / "secret.key"}
    return testclient.TestClient(create_app(paths)), limits, tmp_path / "a.jsonl"


def sign_in(client, name, password):
    res = client.post("/api/account/login", json={"name": name, "password": password})
    assert res.status_code == 200, res.text


def test_ui_page_and_flow_asset_are_served(client):
    ui = client.get("/ui")
    assert ui.status_code == 200 and "ALGO PRO" in ui.text
    assert client.get("/assets/flow_svg.js").status_code == 200


def test_limits_write_needs_owner_sign_in(write_client):
    client, limits, _ = write_client
    assert client.post("/api/limits", json={"max_daily_loss": 500}).status_code == 401
    sign_in(client, "Peon", "viewer-password")
    assert client.post("/api/limits", json={"max_daily_loss": 500}).status_code == 403
    assert "max_daily_loss: null" in limits.read_text(encoding="utf-8")


def test_api_token_header_alone_is_refused(write_client):
    client, limits, _ = write_client
    r = client.post("/api/limits", json={"max_daily_loss": 500}, headers={"X-Dashboard-Token": "anything"})
    assert r.status_code == 401
    assert "max_daily_loss: null" in limits.read_text(encoding="utf-8")


def test_limits_write_saves_and_audits(write_client):
    client, limits, audit = write_client
    sign_in(client, "Owner", "owner-password-1")
    r = client.post("/api/limits", json={"max_daily_loss": 500})
    assert r.status_code == 200 and r.json()["changed"]["new"] == {"max_daily_loss": 500}
    assert "max_daily_loss: 500" in limits.read_text(encoding="utf-8")
    records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["kind"] == "limits_changed"


def test_limits_write_bad_value_is_422(write_client):
    client, _, _ = write_client
    sign_in(client, "Owner", "owner-password-1")
    assert client.post("/api/limits", json={"max_daily_loss": -5}).status_code == 422


def test_kill_status_is_read_only_and_starts_off(write_client):
    client, _, _ = write_client
    assert client.get("/api/kill").json() == {"active": False, "reason": ""}


def test_kill_trip_needs_owner_and_reason(write_client):
    client, _, _ = write_client
    assert client.post("/api/kill/trip", json={"reason": "manual stop"}).status_code == 401
    sign_in(client, "Owner", "owner-password-1")
    assert client.post("/api/kill/trip", json={}).status_code == 422
    assert client.post("/api/kill/trip", json={"reason": "  "}).status_code == 422
    assert client.get("/api/kill").json()["active"] is False


def test_kill_trip_then_reset_is_audited(write_client):
    client, _, audit = write_client
    sign_in(client, "Owner", "owner-password-1")
    tripped = client.post("/api/kill/trip", json={"reason": "manual stop"})
    assert tripped.status_code == 200 and tripped.json()["active"] is True
    assert client.get("/api/kill").json()["active"] is True
    assert client.post("/api/kill/reset", json={}).status_code == 422
    reset = client.post("/api/kill/reset", json={"reason": "checked"})
    assert reset.status_code == 200 and reset.json()["active"] is False
    text = audit.read_text(encoding="utf-8")
    assert '"kill_switch"' in text and "kill_switch_reset" in text


def test_setup_read_lists_strategies_without_sign_in(write_client):
    client, _, _ = write_client
    assert "ocl_keylevel" in client.get("/api/setup").json()["strategies"]


def test_setup_save_needs_owner_and_validates(write_client):
    client, _, audit = write_client
    payload = {"risk_per_trade": 0.0025, "max_symbols_per_portfolio": 2, "strategy": "ocl_keylevel"}
    assert client.post("/api/setup", json=payload).status_code == 401
    sign_in(client, "Owner", "owner-password-1")
    assert client.post("/api/setup", json={"strategy": "nope"}).status_code == 422
    r = client.post("/api/setup", json=payload)
    assert r.status_code == 200 and r.json()["changed"]["new"]["max_symbols_per_portfolio"] == 2
    assert "setup_changed" in audit.read_text(encoding="utf-8")


def test_login_key_is_created_once_with_owner_only_mode(tmp_path, monkeypatch):
    import os
    import stat

    from app import load_or_create_secret

    monkeypatch.delenv("DASHBOARD_SESSION_SECRET", raising=False)
    key = tmp_path / "data" / "session_secret.key"
    first = load_or_create_secret(key)
    assert len(first) >= 40
    assert load_or_create_secret(key) == first  # reused, not regenerated
    if os.name == "posix":
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
