"""Apply-route guards (docs/IMPLEMENTATION_PLAN_ACCOUNTS_AND_RECOMMENDATIONS.md,
Part B exit checks). FastAPI TestClient.
"""
from __future__ import annotations

import time

import pytest

fastapi = pytest.importorskip("fastapi")

from dashboard.app import create_app  # noqa: E402
from quant.core import dashboard_accounts as accounts  # noqa: E402
from quant.core import recommendations as recs  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    paths = {
        "ledger": tmp_path / "ledger.jsonl",
        "audit": tmp_path / "audit.jsonl",
        "kill": tmp_path / "kill.json",
        "limits": tmp_path / "risk_limits.yaml",
        "settings": tmp_path / "settings.yaml",
        "strategies": tmp_path / "strategies",
        "page": tmp_path / "page.html",
        "flow": tmp_path / "flow.js",
        "loop": tmp_path / "loop",
        "holidays": tmp_path / "holidays.csv",
        "mode_state": tmp_path / "mode.json",
        "live_approval": tmp_path / "approval.json",
        "schedule": tmp_path / "schedule.json",
        "users": tmp_path / "users.json",
        "recs": tmp_path / "recs.json",
    }
    paths["limits"].write_text("blocked: false\n", encoding="utf-8")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-secret")
    accounts.create_user("Owner", "owner-password-1", paths["users"], now=time.time(), role="owner")
    accounts.create_user("Peon", "viewer-password", paths["users"], now=time.time(), role="viewer")
    app = create_app(paths)
    from fastapi.testclient import TestClient
    client = TestClient(app)
    return client, paths, tmp_path


def _sign_in(client, name, password):
    res = client.post("/api/account/login", json={"name": name, "password": password})
    assert res.status_code == 200, res.text
    return res


# all recs calls go through the app's paths["recs"]


def test_viewer_and_token_are_refused(env, monkeypatch):
    client, paths, _ = env
    entry = recs.put("s", "max_daily_loss", None, 2500.0, "r", paths["recs"], now=time.time())
    _sign_in(client, "Peon", "viewer-password")
    res = client.post("/api/flow/apply", json={"id": entry["id"]})
    assert res.status_code == 403
    # the write token alone must not pass either
    res = client.post("/api/flow/apply", json={"id": entry["id"]},
                      headers={"X-Dashboard-Token": "t"})
    assert res.status_code == 403


def test_tightening_applies_without_phrase_and_audits_once(env):
    client, paths, _ = env
    _sign_in(client, "Owner", "owner-password-1")
    paths["limits"].write_text("blocked: false\nmax_daily_loss: 1000.0\n", encoding="utf-8")
    entry = recs.put("s", "max_daily_loss", 1000.0, 500.0, "tighten", paths["recs"], now=time.time())
    res = client.post("/api/flow/apply", json={"id": entry["id"]})
    assert res.status_code == 200, res.text
    assert res.json()["new"] == 500.0
    # one use: second click refused
    res2 = client.post("/api/flow/apply", json={"id": entry["id"]})
    assert res2.status_code == 409


def test_loosening_requires_the_phrase(env):
    client, paths, _ = env
    _sign_in(client, "Owner", "owner-password-1")
    paths["limits"].write_text("blocked: false\nmax_daily_loss: 1000.0\n", encoding="utf-8")
    entry = recs.put("s", "max_daily_loss", 1000.0, 2000.0, "raise", paths["recs"], now=time.time())
    res = client.post("/api/flow/apply", json={"id": entry["id"]})
    assert res.status_code == 422 and "APPLY LIMIT" in res.json()["error"]
    res = client.post("/api/flow/apply", json={"id": entry["id"], "phrase": "APPLY LIMIT"})
    assert res.status_code == 200


def test_stale_current_value_is_409(env):
    client, paths, _ = env
    _sign_in(client, "Owner", "owner-password-1")
    paths["limits"].write_text("blocked: false\nmax_daily_loss: 999.0\n", encoding="utf-8")
    entry = recs.put("s", "max_daily_loss", 1000.0, 500.0, "tighten", paths["recs"], now=time.time())
    res = client.post("/api/flow/apply", json={"id": entry["id"]})
    assert res.status_code == 409 and res.json().get("stale") is True


def test_veto_cannot_be_turned_off(env):
    client, paths, _ = env
    _sign_in(client, "Owner", "owner-password-1")
    paths["limits"].write_text("blocked: true\n", encoding="utf-8")
    entry = recs.put("s", "blocked", True, False, "unblock?", paths["recs"], now=time.time())
    res = client.post("/api/flow/apply", json={"id": entry["id"]})
    assert res.status_code == 422 and "veto" in res.json()["error"]
    # nothing written
    assert "blocked: true" in paths["limits"].read_text(encoding="utf-8")


def test_expired_recommendation_is_409(env):
    client, paths, _ = env
    _sign_in(client, "Owner", "owner-password-1")
    entry = recs.put("s", "max_daily_loss", None, 100.0, "r", paths["recs"], now=time.time(), ttl_s=10)
    later = time.time() + 20
    import quant.core.recommendations as r
    res = client.post("/api/flow/apply", json={"id": entry["id"]})
    # route uses real time; force expiry by aging the entry on disk
    data = r._read(paths["recs"])
    data[entry["id"]]["expires"] = 1
    r._write(paths["recs"], data)
    res = client.post("/api/flow/apply", json={"id": entry["id"]})
    assert res.status_code == 409


def test_recommendations_read_route_lists_live_entries(env):
    client, paths, _ = env
    recs.put("s", "max_daily_loss", None, 100.0, "r", paths["recs"], now=time.time())
    res = client.get("/api/flow/recommendations")
    assert res.status_code == 200 and len(res.json()["recommendations"]) == 1


