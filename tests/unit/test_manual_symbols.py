"""Manual symbols in the Screener stocks panel (2026-11-27).

Rules under test:
1. A symbol is validated and stored once, upper-cased; bad input is refused; the list is capped.
2. Each refresh adds manual symbols to the candidates, de-duplicated against the screened names.
3. Only the owner can add or remove; a viewer is refused and nothing is written.
4. Every add or remove is audited with the actor.
No network: the quote path is not touched; refresh_once is replaced by a recorder in the merge test.
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "dashboard"))

from quant.data import manual_symbols as ms  # noqa: E402

pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")


def test_normalise_and_validate():
    assert ms.normalise(" infy ") == "INFY"
    assert ms.normalise("M&M") == "M&M"
    assert ms.normalise("500325") == "500325"          # 6-digit BSE code is allowed
    assert ms.normalise("") is None
    assert ms.normalise("INFY; rm -rf") is None
    assert ms.normalise("A" * 21) is None


def test_add_remove_dedupe_and_cap(tmp_path):
    f = tmp_path / "manual.json"
    assert ms.read(f) == []
    ok, _, now = ms.add(f, "infy")
    assert ok and now == ["INFY"]
    ok, reason, now = ms.add(f, "INFY")
    assert ok and "already" in reason and now == ["INFY"]
    ok, reason, _ = ms.add(f, "bad symbol!")
    assert not ok and "valid" in reason
    ok, _, now = ms.remove(f, "infy")
    assert ok and now == []
    assert ms.remove(f, "NOPE")[0] is False
    for i in range(ms.MAX_SYMBOLS):
        ms.add(f, f"S{i}")
    ok, reason, _ = ms.add(f, "ONEMORE")
    assert not ok and "full" in reason


def test_broken_file_reads_as_empty(tmp_path):
    f = tmp_path / "manual.json"
    f.write_text("{not json", encoding="utf-8")
    assert ms.read(f) == []


def test_refresh_merges_manual_symbols_without_duplicates(tmp_path, monkeypatch):
    from quant.data import screen_refresh as sr

    source = tmp_path / "last_screen.json"
    source.write_text(json.dumps({"results": [
        {"symbol": "RELIANCE", "selected": True}, {"symbol": "TCS", "selected": False}]}), encoding="utf-8")
    manual = tmp_path / "manual.json"
    ms.add(manual, "reliance")   # already screened: must not appear twice
    ms.add(manual, "INFY")       # manual only

    seen = {}

    def recorder(candidates, quote_fn, target, **kwargs):
        seen["candidates"] = candidates
        return {"exit_code": 0, "wrote": 1}

    monkeypatch.setattr(sr, "refresh_once", recorder)
    refresh = sr.make_screen_refresher(object(), "key", source=source, target=tmp_path / "latest.json",
                                       manual_path=manual)
    refresh(datetime(2026, 11, 27, 10, 0))
    symbols = [r["symbol"] for r in seen["candidates"]]
    assert symbols == ["RELIANCE", "INFY"]
    assert seen["candidates"][1].get("manual") is True


def test_refresh_without_manual_path_is_unchanged(tmp_path, monkeypatch):
    from quant.data import screen_refresh as sr

    source = tmp_path / "last_screen.json"
    source.write_text(json.dumps({"results": [{"symbol": "HDFC", "selected": True}]}), encoding="utf-8")
    seen = {}
    def recorder(candidates, *a, **k):
        seen["c"] = candidates
        return {"exit_code": 0}

    monkeypatch.setattr(sr, "refresh_once", recorder)
    sr.make_screen_refresher(object(), "key", source=source, target=tmp_path / "t.json")(datetime(2026, 11, 27, 10, 0))
    assert [r["symbol"] for r in seen["c"]] == ["HDFC"]


@pytest.fixture
def client_setup(tmp_path, monkeypatch):
    import app as app_mod
    from quant.core import dashboard_accounts as accounts
    from quant.data import screener_session as ss

    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "test-secret")
    monkeypatch.setattr(app_mod, "login_after_signin", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(ss, "refresh_session", lambda **_: {"ok": True, "saved": False, "reason": "reused"})
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
             "holidays": holidays, "manual_symbols": tmp_path / "manual.json"}
    return paths


def _client(paths, name=None, password=None):
    from app import create_app

    client = testclient.TestClient(create_app(paths))
    if name:
        assert client.post("/api/account/login", json={"name": name, "password": password}).status_code == 200
    return client


def test_owner_adds_and_removes_and_it_is_audited(client_setup):
    paths = client_setup
    owner = _client(paths, "Owner", "owner-password-1")
    r = owner.post("/api/screener/manual", json={"action": "add", "symbol": "infy"})
    assert r.status_code == 200 and r.json()["symbols"] == ["INFY"]
    assert ms.read(paths["manual_symbols"]) == ["INFY"]
    assert owner.get("/api/overview").json()["manual_symbols"] == ["INFY"]
    bad = owner.post("/api/screener/manual", json={"action": "add", "symbol": "x y"})
    assert bad.status_code == 422
    assert owner.post("/api/screener/manual", json={"action": "remove", "symbol": "INFY"}).status_code == 200
    assert ms.read(paths["manual_symbols"]) == []
    audit = paths["audit"].read_text(encoding="utf-8")
    assert "screener_manual_symbol" in audit and '"actor": "Owner"' in audit


def test_viewer_and_anonymous_are_refused_and_nothing_is_written(client_setup):
    paths = client_setup
    viewer = _client(paths, "Peon", "viewer-password")
    assert viewer.post("/api/screener/manual", json={"action": "add", "symbol": "TCS"}).status_code == 403
    anon = _client(paths)
    assert anon.post("/api/screener/manual", json={"action": "add", "symbol": "TCS"}).status_code == 401
    assert not paths["manual_symbols"].exists()
