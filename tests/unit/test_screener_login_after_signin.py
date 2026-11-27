"""Owner sign-in starts the Screener.in session refresh; a viewer does not.

Rules under test (2026-11-27):
1. The refresh runs after an OWNER dashboard sign-in, in a background thread (never blocks login).
2. A viewer sign-in does not start it.
3. A failure is contained and audited; it never raises or breaks the login.
4. The audit row records the outcome and the actor, never a cookie value.
"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dashboard"))

pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")

from quant.data import screener_session as ss  # noqa: E402


def _last_payload(audit):
    return json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])["payload"]


def test_success_is_audited_with_actor(tmp_path):
    audit = tmp_path / "a.jsonl"
    res = ss.screener_login_after_signin(
        "Owner", audit, runner=lambda: {"ok": True, "saved": True, "reason": "session saved to .env"})
    assert res["saved"] is True
    row = _last_payload(audit)
    assert row["ok"] is True and row["saved"] is True and row["actor"] == "Owner"


def test_exception_is_contained_and_audited(tmp_path):
    audit = tmp_path / "a.jsonl"

    def boom():
        raise RuntimeError("network down")

    res = ss.screener_login_after_signin("Owner", audit, runner=boom)
    assert res["ok"] is False
    assert _last_payload(audit)["reason"] == "RuntimeError"


@pytest.fixture
def app_paths(tmp_path, monkeypatch):
    import app as app_mod  # noqa: F401  (imported for the monkeypatch below)
    from quant.core import dashboard_accounts as accounts

    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-secret")
    # The broker sign-in thread is not under test here: keep it offline.
    monkeypatch.setattr(sys.modules["app"], "login_after_signin", lambda *a, **k: {"ok": True})
    users = tmp_path / "users.json"
    accounts.create_user("Owner", "owner-password-1", users, now=time.time(), role="owner")
    accounts.create_user("Peon", "viewer-password", users, now=time.time(), role="viewer")
    holidays = tmp_path / "holidays.csv"
    holidays.write_text("date,exchange\n", encoding="utf-8")
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("blocked: false\n", encoding="utf-8")
    return {"ledger": tmp_path / "l.jsonl", "audit": tmp_path / "a.jsonl",
            "kill": tmp_path / "k.json", "bot_kill": tmp_path / "bk.json",
            "live_kill": tmp_path / "lk.json", "limits": limits, "settings": limits,
            "strategies": tmp_path / "strategies", "users": users,
            "session_secret": tmp_path / "secret.key", "mode_state": tmp_path / "mode.json",
            "holidays": holidays}


def test_owner_sign_in_starts_screener_refresh(app_paths, monkeypatch):
    from app import create_app
    import quant.data.screen_refresh as sr

    started = threading.Event()
    seen = []

    def fake(actor, audit_path, source, **kwargs):
        seen.append(actor)
        started.set()
        return {"ran": False}

    monkeypatch.setattr(sr, "owner_signin_job", fake)
    client = testclient.TestClient(create_app(app_paths))
    r = client.post("/api/account/login", json={"name": "Owner", "password": "owner-password-1"})
    assert r.status_code == 200
    assert started.wait(5) and seen == ["Owner"]


def test_viewer_sign_in_does_not_start_screener_refresh(app_paths, monkeypatch):
    from app import create_app

    import quant.data.screen_refresh as sr

    started = threading.Event()
    monkeypatch.setattr(sr, "owner_signin_job",
                        lambda *a, **k: (started.set(), {"ran": False})[1])
    client = testclient.TestClient(create_app(app_paths))
    r = client.post("/api/account/login", json={"name": "Peon", "password": "viewer-password"})
    assert r.status_code == 200
    assert not started.wait(0.5)
