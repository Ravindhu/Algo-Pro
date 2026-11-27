"""Owner decisions of 2026-11-28, applied:

* Item 2  - a saved API key / credential is written to BOTH _restored/.env and the repo-root .env.
* Item 8  - a missing or broken source_mode.json fails closed to MANUAL symbols only.
* Item 12 - a failed scheduled broker login raises a CRITICAL alert the dashboard Notification tab shows.

Every write goes to a temp folder. No test touches the real .env or the real audit trail.
"""
from pathlib import Path
from types import SimpleNamespace

from quant.core import credential_update as cu
from quant.core import openalgo_apikey as apikey
from quant.data import source_mode as sm
from quant.engines.core import broker_auth
from quant.reporting import notifications

KEY = "new-key-123"


# ---- item 2: both .env copies -----------------------------------------------------------

def test_sync_env_updates_restored_and_creates_repo_copy(tmp_path):
    restored = tmp_path / "_restored" / ".env"
    restored.parent.mkdir()
    restored.write_text("OPENALGO_API_KEY='old'\n", encoding="utf-8")
    repo = tmp_path / ".env"
    assert apikey.sync_env(KEY, restored, repo_env=repo) == "updated"
    assert apikey.read_env_value(restored, "OPENALGO_API_KEY") == KEY
    assert apikey.read_env_value(repo, "OPENALGO_API_KEY") == KEY


def test_sync_env_second_run_is_unchanged_for_both(tmp_path):
    restored = tmp_path / "r.env"
    restored.write_text("OPENALGO_API_KEY='x'\n", encoding="utf-8")
    repo = tmp_path / ".env"
    apikey.sync_env(KEY, restored, repo_env=repo)
    assert apikey.sync_env(KEY, restored, repo_env=repo) == "unchanged"


def test_sync_env_repo_copy_is_not_created_when_not_asked(tmp_path):
    restored = tmp_path / "missing.env"
    assert apikey.sync_env(KEY, restored).startswith("skipped")
    assert not (tmp_path / ".env").exists()


def test_credential_save_writes_the_repo_copy_too(tmp_path, monkeypatch):
    restored = tmp_path / "_restored" / ".env"
    restored.parent.mkdir()
    restored.write_text("BROKER_PASSWORD='old'\n", encoding="utf-8")
    repo = tmp_path / ".env"
    monkeypatch.setattr(cu, "RESTORED_ENV", restored)
    monkeypatch.setattr(cu, "REPO_ENV", repo)
    monkeypatch.setattr(cu, "ALLOWED", {"BROKER_PASSWORD": restored})
    monkeypatch.setattr(cu, "owner_totp_ok", lambda *a, **k: True)
    ok, _ = cu.update("BROKER_PASSWORD", "fresh-pw", owner="o", totp_code="123456")
    assert ok is True
    assert "BROKER_PASSWORD='fresh-pw'" in restored.read_text(encoding="utf-8")
    assert "BROKER_PASSWORD='fresh-pw'" in repo.read_text(encoding="utf-8")


def test_write_env_values_or_create_makes_the_file_private(tmp_path):
    from quant.core.env_write import write_env_values_or_create
    target = tmp_path / ".env"
    write_env_values_or_create(target, {"A_KEY": "v1"})
    assert "A_KEY='v1'" in target.read_text(encoding="utf-8")
    write_env_values_or_create(target, {"A_KEY": "v2"})
    assert target.read_text(encoding="utf-8").count("A_KEY=") == 1


# ---- item 8: fail closed to manual symbols -----------------------------------------------

def test_missing_source_mode_file_means_manual_only(tmp_path):
    state = sm.read(tmp_path / "absent.json")
    assert sm.effective(state) == (True, False)


def test_broken_source_mode_file_means_manual_only(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{not json", encoding="utf-8")
    assert sm.effective(sm.read(f)) == (True, False)


def test_explicit_both_off_still_means_both_paths(tmp_path):
    # An owner who saved both switches OFF keeps the existing rule: both paths work.
    f = tmp_path / "source_mode.json"
    f.write_text('{"manual": false, "automation": false}', encoding="utf-8")
    assert sm.effective(sm.read(f)) == (True, True)


# ---- item 12: failed scheduled login reaches the dashboard -------------------------------

def test_failed_login_writes_a_critical_alert_the_notification_tab_shows(tmp_path, monkeypatch):
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(broker_auth, "_BOT_AUDIT_PATH", audit)
    result = SimpleNamespace(leg="broker", reason="invalid credentials")
    broker_auth._notify_login_failure(result)
    rec = notifications.recent([audit])
    assert rec["count"] == 1
    item = rec["items"][0]
    assert item["level"] == "CRITICAL"
    assert "broker auto-login failed" in item["message"] and "invalid credentials" in item["message"]


def test_failed_login_alert_never_raises(tmp_path, monkeypatch):
    # A path that cannot be written must not break the login job.
    monkeypatch.setattr(broker_auth, "_BOT_AUDIT_PATH", tmp_path)  # a directory, not a file
    broker_auth._notify_login_failure(SimpleNamespace(leg="x", reason="y"))  # no exception
