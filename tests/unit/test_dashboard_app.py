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
    from app import create_app

    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("blocked: false\nmax_daily_loss: null\n", encoding="utf-8")
    paths = {"ledger": tmp_path / "l.jsonl", "audit": tmp_path / "a.jsonl",
             "kill": tmp_path / "k.json", "limits": limits, "settings": tmp_path / "none.yaml"}
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "test-token-123")
    return testclient.TestClient(create_app(paths)), limits, tmp_path / "a.jsonl"


def test_limits_write_refused_without_server_token(write_client, monkeypatch):
    client, limits, _ = write_client
    monkeypatch.delenv("DASHBOARD_API_TOKEN", raising=False)
    r = client.post("/api/limits", json={"max_daily_loss": 500}, headers={"X-Dashboard-Token": "x"})
    assert r.status_code == 503
    assert "max_daily_loss: null" in limits.read_text(encoding="utf-8")


def test_limits_write_needs_the_right_token(write_client):
    client, limits, _ = write_client
    assert client.post("/api/limits", json={"max_daily_loss": 500}).status_code == 401
    bad = client.post("/api/limits", json={"max_daily_loss": 500}, headers={"X-Dashboard-Token": "nope"})
    assert bad.status_code == 401
    assert "max_daily_loss: null" in limits.read_text(encoding="utf-8")


def test_limits_write_saves_and_audits(write_client):
    client, limits, audit = write_client
    r = client.post("/api/limits", json={"max_daily_loss": 500},
                    headers={"X-Dashboard-Token": "test-token-123"})
    assert r.status_code == 200 and r.json()["changed"]["new"] == {"max_daily_loss": 500}
    assert "max_daily_loss: 500" in limits.read_text(encoding="utf-8")
    records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["kind"] == "limits_changed"


def test_ui_page_and_flow_asset_are_served(client):
    ui = client.get("/ui")
    assert ui.status_code == 200 and "ALGO PRO" in ui.text
    assert client.get("/assets/flow_svg.js").status_code == 200


def test_limits_write_bad_value_is_422(write_client):
    client, _, _ = write_client
    r = client.post("/api/limits", json={"max_daily_loss": -5},
                    headers={"X-Dashboard-Token": "test-token-123"})
    assert r.status_code == 422


def test_kill_status_is_read_only_and_starts_off(write_client):
    client, _, _ = write_client
    assert client.get("/api/kill").json() == {"active": False, "reason": ""}


def test_kill_trip_needs_token_and_reason(write_client):
    client, _, audit = write_client
    body = {"reason": "manual stop"}
    assert client.post("/api/kill/trip", json=body).status_code == 401
    good = {"X-Dashboard-Token": "test-token-123"}
    assert client.post("/api/kill/trip", json={}, headers=good).status_code == 422
    assert client.post("/api/kill/trip", json={"reason": "  "}, headers=good).status_code == 422
    assert client.get("/api/kill").json()["active"] is False


def test_kill_trip_then_reset_is_audited(write_client):
    client, _, audit = write_client
    good = {"X-Dashboard-Token": "test-token-123"}
    tripped = client.post("/api/kill/trip", json={"reason": "manual stop"}, headers=good)
    assert tripped.status_code == 200 and tripped.json()["active"] is True
    assert client.get("/api/kill").json()["active"] is True
    assert client.post("/api/kill/reset", json={}, headers=good).status_code == 422
    reset = client.post("/api/kill/reset", json={"reason": "checked"}, headers=good)
    assert reset.status_code == 200 and reset.json()["active"] is False
    text = audit.read_text(encoding="utf-8")
    assert '"kill_switch"' in text and "kill_switch_reset" in text
