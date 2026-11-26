"""POST /api/screener/session: the START ENGINE button's Screener.in sign-in step.

Status: IMPLEMENTED 2026-11-26

Rules under test:
1. The owner gets a JSON result and the route never echoes the cookie value.
2. A viewer is refused (a .env write is an owner action).
3. The audit row records the KEY name, the outcome, and never the value.
4. A refused login is a 409 with a reason, not a 500.
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dashboard"))

pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")

COOKIE = "route-test-cookie-9c1"


class _Reply:
    def __init__(self, ok=True, error=None, data=None):
        self.ok, self.error, self.data = ok, error, data


@pytest.fixture
def clients(tmp_path, monkeypatch):
    """(owner_client, viewer_client, paths) — FastAPI app with a temp .env and users file."""
    from app import create_app
    from quant.core import dashboard_accounts as accounts
    from quant.data import screener_session as ss

    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-secret")
    users = tmp_path / "users.json"
    accounts.create_user("Owner", "owner-password-1", users, now=time.time(), role="owner")
    accounts.create_user("Peon", "viewer-password", users, now=time.time(), role="viewer")

    env_file = tmp_path / ".env"
    env_file.write_text("Screenerdotin_sessionid=expired\nKEEP=1\n", encoding="utf-8")
    # The route calls the default .env; point it at the temp file for this test.
    monkeypatch.setattr(ss, "DEFAULT_ENV_FILE", env_file)

    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("blocked: false\n", encoding="utf-8")
    holidays = tmp_path / "holidays.csv"
    holidays.write_text("date,exchange\n", encoding="utf-8")
    paths = {"ledger": tmp_path / "l.jsonl", "audit": tmp_path / "a.jsonl",
             "kill": tmp_path / "k.json", "bot_kill": tmp_path / "bk.json",
             "live_kill": tmp_path / "lk.json", "limits": limits, "settings": limits,
             "strategies": tmp_path / "strategies", "users": users,
             "session_secret": tmp_path / "secret.key", "mode_state": tmp_path / "mode.json",
             "holidays": holidays}

    owner = testclient.TestClient(create_app(paths))
    assert owner.post("/api/account/login",
                      json={"name": "Owner", "password": "owner-password-1"}).status_code == 200
    viewer = testclient.TestClient(create_app(paths))
    assert viewer.post("/api/account/login",
                       json={"name": "Peon", "password": "viewer-password"}).status_code == 200
    return owner, viewer, paths, env_file


def _ok_client(monkeypatch, reply):
    """Replace the network client the route would build with an injected fake."""
    from quant.data import screener_session as ss

    class _Client:
        base_url = "https://www.screener.in"

        def login(self, username, password):
            return reply

    monkeypatch.setenv("SCREENER_USERNAME", "me@example.com")
    monkeypatch.setenv("SCREENER_PASSWORD", "pw")
    monkeypatch.setattr(ss, "_client", lambda base_url: _Client())


def test_owner_saves_the_session_and_never_sees_the_value(clients, monkeypatch):
    owner, _viewer, paths, env_file = clients
    _ok_client(monkeypatch, _Reply(data={"sessionid": COOKIE}))

    res = owner.post("/api/screener/session")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["saved"] is True and body["ok"] is True
    assert COOKIE not in res.text
    assert f"Screenerdotin_sessionid='{COOKIE}'" in env_file.read_text(encoding="utf-8")

    rows = [json.loads(ln) for ln in paths["audit"].read_text(encoding="utf-8").splitlines() if ln]
    entry = [r for r in rows if r.get("kind") == "screener_session_refreshed"][-1]
    assert COOKIE not in json.dumps(entry)
    assert entry["payload"]["saved"] is True
    assert entry["payload"]["key"] == "Screenerdotin_sessionid"


def test_a_viewer_cannot_write_the_env(clients, monkeypatch):
    _owner, viewer, _paths, env_file = clients
    _ok_client(monkeypatch, _Reply(data={"sessionid": COOKIE}))
    assert viewer.post("/api/screener/session").status_code in (401, 403)
    assert "expired" in env_file.read_text(encoding="utf-8")


def test_a_refused_login_is_409_not_500(clients, monkeypatch):
    owner, _viewer, _paths, env_file = clients
    _ok_client(monkeypatch, _Reply(ok=False, error="login was not accepted"))
    res = owner.post("/api/screener/session")
    assert res.status_code == 409
    assert res.json()["saved"] is False
    assert COOKIE not in res.text
    assert "expired" in env_file.read_text(encoding="utf-8")
