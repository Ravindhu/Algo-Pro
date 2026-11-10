"""Dashboard accounts, session cookies and the manual engine start decision.

Pure logic only (plan: docs/SCHEDULER_PLAN.md). No FastAPI, no engine process.
"""
import json
from pathlib import Path

import pytest

from quant.core import dashboard_accounts as acc
from quant.core import engine_control as eng

PW = "correct-horse-battery"


def test_password_is_never_stored_in_plain_text(tmp_path):
    path = tmp_path / "users.json"
    acc.create_user("Ravindhu", PW, path, now=1000)
    raw = path.read_text(encoding="utf-8")
    assert PW not in raw
    assert "salt" in raw and "hash" in raw


def test_short_password_and_bad_name_refused(tmp_path):
    path = tmp_path / "users.json"
    with pytest.raises(ValueError):
        acc.create_user("Ravindhu", "short", path)
    with pytest.raises(ValueError):
        acc.create_user("1bad name", PW, path)
    assert not path.exists()


def test_duplicate_name_refused(tmp_path):
    path = tmp_path / "users.json"
    acc.create_user("Ravindhu", PW, path)
    with pytest.raises(ValueError):
        acc.create_user("Ravindhu", PW, path)


def test_login_right_and_wrong_password(tmp_path):
    path = tmp_path / "users.json"
    acc.create_user("Ravindhu", PW, path, now=1000)
    assert acc.verify_login("Ravindhu", PW, path, now=1001) == (True, "ok")
    ok, reason = acc.verify_login("Ravindhu", "wrong-password-xx", path, now=1002)
    assert ok is False and reason == "invalid name or password"


def test_unknown_name_gives_same_message_as_wrong_password(tmp_path):
    path = tmp_path / "users.json"
    acc.create_user("Ravindhu", PW, path)
    _, unknown = acc.verify_login("nobody", PW, path)
    _, wrong = acc.verify_login("Ravindhu", "wrong-password-xx", path)
    assert unknown == wrong  # does not reveal which names exist


def test_lockout_after_five_failures_then_expires(tmp_path):
    path = tmp_path / "users.json"
    acc.create_user("Ravindhu", PW, path)
    t = 5000
    for _ in range(acc.MAX_FAILURES):
        acc.verify_login("Ravindhu", "wrong-password-xx", path, now=t)
    # locked: even the right password is refused during the window
    ok, reason = acc.verify_login("Ravindhu", PW, path, now=t + 10)
    assert ok is False and "locked" in reason
    # after the window the right password works again
    assert acc.verify_login("Ravindhu", PW, path, now=t + acc.LOCK_SECONDS + 1) == (True, "ok")


def test_unreadable_users_file_means_no_login(tmp_path):
    path = tmp_path / "users.json"
    path.write_text("{broken", encoding="utf-8")
    assert acc.verify_login("Ravindhu", PW, path)[0] is False


def test_session_round_trip_and_tamper_detection():
    secret = "s3cret-value-for-tests"
    cookie = acc.make_session("Ravindhu", secret, now=1000)
    assert acc.read_session(cookie, secret, now=1001) == "Ravindhu"
    assert acc.read_session(cookie.replace("Ravindhu", "Mallory"), secret, now=1001) is None
    assert acc.read_session(cookie, "other-secret", now=1001) is None


def test_session_expires():
    secret = "s3cret-value-for-tests"
    cookie = acc.make_session("Ravindhu", secret, now=1000)
    assert acc.read_session(cookie, secret, now=1000 + acc.SESSION_TTL + 5) is None


def test_no_secret_means_no_session():
    assert acc.read_session("Ravindhu|99999999999.abc", "") is None


# ---------------------------------------------------------- engine start
def test_manual_start_refused_when_kill_switch_on():
    assert eng.decide_start(kill_active=True, already_running=False)[0] is False


def test_manual_start_refused_when_already_running():
    ok, msg = eng.decide_start(kill_active=False, already_running=True)
    assert ok is False and "already running" in msg


def test_manual_start_allowed_when_clear():
    assert eng.decide_start(kill_active=False, already_running=False)[0] is True


def test_start_engine_refuses_on_kill_switch_and_spawns_nothing(tmp_path):
    ok, msg, pid = eng.start_engine(kill_active=True, pid_path=tmp_path / "e.pid",
                                    log_path=tmp_path / "e.log", supervisor=tmp_path / "missing.py")
    assert ok is False and pid is None
    assert not (tmp_path / "e.pid").exists()


def test_stale_pid_file_is_removed(tmp_path):
    pid_file = tmp_path / "e.pid"
    pid_file.write_text(json.dumps({"pid": 999999999}), encoding="utf-8")
    assert eng.running_pid(pid_file) is None
    assert not pid_file.exists()
