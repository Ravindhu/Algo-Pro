"""POST /api/screener/refresh: the screener panel's Refresh button.

Rules under test (2026-11-27):
1. The owner gets a result and the saved screen is re-scraped NOW (no staleness wait).
2. A viewer is refused and nothing is scraped.
3. A failed scrape is reported back, not raised.
Network is faked: the session login and the scraper are injected, so no screener.in call is made.
"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dashboard"))

pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import app as app_mod
    from quant.core import dashboard_accounts as accounts
    from quant.data import screen_refresh as sr
    from quant.data import screener_session as ss

    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-secret")
    monkeypatch.setattr(app_mod, "login_after_signin", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(ss, "refresh_session", lambda **_: {"ok": True, "saved": False, "reason": "reused"})
    calls = []
    state = {"result": {"ok": True, "count": 2, "reason": "2 selected"}}

    def fake_scraper(**_):
        def scrape():
            calls.append(1)
            return state["result"]
        return scrape

    monkeypatch.setattr(sr, "make_screener_scraper", fake_scraper)
    users = tmp_path / "users.json"
    accounts.create_user("Owner", "owner-password-1", users, now=time.time(), role="owner")
    accounts.create_user("Peon", "viewer-password", users, now=time.time(), role="viewer")
    holidays = tmp_path / "holidays.csv"
    holidays.write_text("date,exchange\n", encoding="utf-8")
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("blocked: false\n", encoding="utf-8")
    paths = {"ledger": tmp_path / "l.jsonl", "audit": tmp_path / "a.jsonl",
             "kill": tmp_path / "k.json", "bot_kill": tmp_path / "bk.json",
             "live_kill": tmp_path / "lk.json", "limits": limits, "settings": limits,
             "strategies": tmp_path / "strategies", "users": users,
             "session_secret": tmp_path / "secret.key", "mode_state": tmp_path / "mode.json",
             "holidays": holidays}
    return paths, calls, state


def _client(paths, name, password):
    from app import create_app

    client = testclient.TestClient(create_app(paths))
    assert client.post("/api/account/login", json={"name": name, "password": password}).status_code == 200
    return client


def test_owner_refresh_scrapes_now_and_returns_result(setup):
    paths, calls, _ = setup
    owner = _client(paths, "Owner", "owner-password-1")
    calls.clear()  # the sign-in job may have run its own scrape; count only this press
    r = owner.post("/api/screener/refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["ran"] is True and body["scrape"]["ok"] is True
    assert calls == [1]


def test_viewer_refresh_is_refused_and_scrapes_nothing(setup):
    paths, calls, _ = setup
    viewer = _client(paths, "Peon", "viewer-password")
    calls.clear()
    r = viewer.post("/api/screener/refresh")
    assert r.status_code == 403
    assert calls == []


def test_failed_scrape_is_reported_not_raised(setup):
    paths, calls, state = setup
    state["result"] = {"ok": False, "reason": "screener run failed: login required"}
    owner = _client(paths, "Owner", "owner-password-1")
    calls.clear()
    r = owner.post("/api/screener/refresh")
    assert r.status_code == 200
    assert r.json()["scrape"]["ok"] is False
    assert "login required" in r.json()["scrape"]["reason"]
