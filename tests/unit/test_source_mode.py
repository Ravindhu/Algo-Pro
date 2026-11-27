"""M (manual) and A (automation) source switches, and manual-first ordering (2026-11-27).

Rules under test:
1. M on alone -> manual only. A on alone -> Screener.in only. Both on or both off -> both paths.
2. The refresh obeys the switches: the Screener.in scrape and selection are skipped when A is not in use.
3. Each written row is tagged source manual|screener; the panel lists manual rows first.
4. Only the owner can flip a switch; each flip is audited.
No network: the scrape and quote functions are replaced.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "dashboard"))

from quant.data import source_mode as sm  # noqa: E402
from quant.data import screen_refresh as sr  # noqa: E402
from quant.data import manual_symbols as ms  # noqa: E402
from quant.reporting import home_view  # noqa: E402

NOW = datetime(2026, 11, 27, 10, 0)


@pytest.mark.parametrize("state, expected", [
    ({"manual": True, "automation": False}, (True, False)),
    ({"manual": False, "automation": True}, (False, True)),
    ({"manual": True, "automation": True}, (True, True)),
    ({"manual": False, "automation": False}, (True, True)),
])
def test_switch_rule(state, expected):
    assert sm.effective(state) == expected


def test_read_set_and_broken_file(tmp_path):
    f = tmp_path / "source_mode.json"
    # 2026-11-28 (item 8): a missing file fails closed to manual symbols only.
    assert sm.read(f) == {"manual": True, "automation": False}
    assert sm.set_flag(f, "manual", True) == {"manual": True, "automation": False}
    assert sm.read(f) == {"manual": True, "automation": False}
    f.write_text("[oops", encoding="utf-8")
    assert sm.read(f) == {"manual": True, "automation": False}
    with pytest.raises(ValueError):
        sm.set_flag(f, "bogus", True)


def _source(tmp_path):
    src = tmp_path / "last_screen.json"
    src.write_text(json.dumps({"results": [{"symbol": "RELIANCE", "selected": True}]}), encoding="utf-8")
    manual = tmp_path / "manual.json"
    ms.add(manual, "INFY")
    return src, manual


def _recorder(monkeypatch):
    seen = {}

    def fake_refresh_once(candidates, *a, **k):
        seen["symbols"] = [c["symbol"] for c in candidates]
        return {"exit_code": 0}

    monkeypatch.setattr(sr, "refresh_once", fake_refresh_once)
    return seen


def _run(tmp_path, monkeypatch, state, scrape=None):
    src, manual = _source(tmp_path)
    import os
    old_time = NOW.timestamp() - 3 * 3600      # 3 h before the refresh clock: older than 60 min = stale
    os.utime(src, (old_time, old_time))
    mode = tmp_path / "source_mode.json"
    mode.write_text(json.dumps(state), encoding="utf-8")
    seen = _recorder(monkeypatch)
    refresh = sr.make_screen_refresher(object(), "k", source=src, target=tmp_path / "latest.json",
                                       scrape=scrape, manual_path=manual, mode_path=mode)
    refresh(NOW)
    return seen


def test_manual_only_drops_screener_names(tmp_path, monkeypatch):
    seen = _run(tmp_path, monkeypatch, {"manual": True, "automation": False})
    assert seen["symbols"] == ["INFY"]


def test_automation_only_drops_manual_names_and_scrapes(tmp_path, monkeypatch):
    calls = []
    seen = _run(tmp_path, monkeypatch, {"manual": False, "automation": True},
                scrape=lambda: calls.append(1) or {"ok": True})
    assert seen["symbols"] == ["RELIANCE"]
    assert calls == [1]


def test_both_off_or_both_on_uses_both(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, {"manual": False, "automation": False})["symbols"] == ["RELIANCE", "INFY"]
    assert _run(tmp_path, monkeypatch, {"manual": True, "automation": True})["symbols"] == ["RELIANCE", "INFY"]


def test_manual_only_does_not_scrape(tmp_path, monkeypatch):
    calls = []
    _run(tmp_path, monkeypatch, {"manual": True, "automation": False},
         scrape=lambda: calls.append(1) or {"ok": True})
    assert calls == []


def test_rows_tagged_by_source_and_manual_listed_first(tmp_path):
    rows = [{"symbol": "RELIANCE", "selected": True, "source": "screener", "metrics": {"current_price": 10}},
            {"symbol": "INFY", "selected": True, "source": "manual"}]
    sr._attach_source(rows, [{"symbol": "INFY", "manual": True}, {"symbol": "RELIANCE"}])
    assert [r["source"] for r in rows] == ["screener", "manual"]
    f = tmp_path / "latest.json"
    f.write_text(json.dumps({"as_of": "2026-11-27T10:00:00", "results": rows}), encoding="utf-8")
    out = home_view.read_screener(f, ticker_fn=lambda s: s)
    assert [r["symbol"] for r in out["rows"]] == ["INFY", "RELIANCE"]
    assert out["rows"][0]["status"] == "manual"


def test_owner_flips_switch_audited_and_viewer_refused(tmp_path, monkeypatch):
    import time
    pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
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
             "holidays": holidays, "manual_symbols": tmp_path / "manual.json",
             "source_mode": tmp_path / "source_mode.json"}

    def client(name=None, password=None):
        c = testclient.TestClient(app_mod.create_app(paths))
        if name:
            assert c.post("/api/account/login", json={"name": name, "password": password}).status_code == 200
        return c

    owner = client("Owner", "owner-password-1")
    r = owner.post("/api/screener/source", json={"key": "manual", "on": True})
    assert r.status_code == 200
    assert r.json()["use_manual"] is True and r.json()["use_screener"] is False
    state = owner.get("/api/overview").json()["screener_source"]
    assert state["manual"] is True and state["use_screener"] is False
    assert owner.post("/api/screener/source", json={"key": "bogus", "on": True}).status_code == 422
    audit = paths["audit"].read_text(encoding="utf-8")
    assert "screener_source_mode" in audit and '"actor": "Owner"' in audit

    viewer = client("Peon", "viewer-password")
    assert viewer.post("/api/screener/source", json={"key": "automation", "on": True}).status_code == 403
    assert sm.read(paths["source_mode"])["automation"] is False
