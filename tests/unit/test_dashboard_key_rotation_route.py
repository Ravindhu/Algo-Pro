"""Login-key rotation as seen by the dashboard (dashboard/app.py). Needs FastAPI."""
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dashboard"))

pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")

DAY = 86400


def _paths(tmp_path):
    from quant.core import dashboard_accounts as accounts

    users = tmp_path / "users.json"
    accounts.create_user("Owner", "owner-password-1", users, now=time.time(), role="owner")
    return {"ledger": tmp_path / "l.jsonl", "audit": tmp_path / "a.jsonl",
            "kill": tmp_path / "k.json", "limits": tmp_path / "risk_limits.yaml",
            "settings": tmp_path / "none.yaml", "users": users,
            "session_secret": tmp_path / "data" / "session_secret.key",
            "bot_kill": tmp_path / "bk.json", "live_kill": tmp_path / "lk.json",
            "mode_state": tmp_path / "mode.json", "live_approval": tmp_path / "approval.json",
            "holidays": tmp_path / "holidays.csv"}


def _backdate(path, days):
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))


def test_old_key_is_rotated_at_start_and_audited(tmp_path, monkeypatch):
    from app import create_app

    monkeypatch.delenv("DASHBOARD_SESSION_SECRET", raising=False)
    monkeypatch.delenv("DASHBOARD_KEY_MAX_AGE_DAYS", raising=False)
    paths = _paths(tmp_path)
    paths["session_secret"].parent.mkdir(parents=True)
    paths["session_secret"].write_text("ancient-key\n", encoding="utf-8")
    _backdate(paths["session_secret"], 90)

    create_app(paths)

    assert paths["session_secret"].read_text(encoding="utf-8").strip() != "ancient-key"
    audit = paths["audit"].read_text(encoding="utf-8")
    assert "session_key_rotated" in audit and "ancient-key" not in audit


def test_cookie_from_before_a_rotation_stops_working(tmp_path, monkeypatch):
    from app import create_app

    monkeypatch.delenv("DASHBOARD_SESSION_SECRET", raising=False)
    monkeypatch.delenv("DASHBOARD_KEY_MAX_AGE_DAYS", raising=False)
    paths = _paths(tmp_path)

    first = testclient.TestClient(create_app(paths))
    res = first.post("/api/account/login", json={"name": "Owner", "password": "owner-password-1"})
    assert res.status_code == 200
    cookie = res.cookies.get("algo_session")
    assert first.get("/api/account").json()["signed_in"] == "Owner"

    _backdate(paths["session_secret"], 90)  # key is now due
    later = testclient.TestClient(create_app(paths))  # start-up check rotates it
    later.cookies.set("algo_session", cookie)
    assert later.get("/api/account").json()["signed_in"] is None


def test_env_pin_keeps_the_key_and_skips_rotation(tmp_path, monkeypatch):
    from app import create_app

    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "pinned-by-owner")
    paths = _paths(tmp_path)
    paths["session_secret"].parent.mkdir(parents=True)
    paths["session_secret"].write_text("file-key\n", encoding="utf-8")
    _backdate(paths["session_secret"], 90)

    client = testclient.TestClient(create_app(paths))
    assert paths["session_secret"].read_text(encoding="utf-8").strip() == "file-key"
    infra = {i["name"]: i for i in client.get("/api/infrastructure").json()["items"]}
    assert infra["Login key rotation"]["ok"] is False
    assert "pinned" in infra["Login key rotation"]["detail"]
