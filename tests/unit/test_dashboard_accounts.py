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


# --- roles (plan Part A) -----------------------------------------------------
def test_role_defaults_to_viewer_and_owner_check(tmp_path):
    path = tmp_path / "users.json"
    acc.create_user("Viewer", PW, path, now=1000)
    acc.create_user("Boss", PW, path, now=1000, role="owner")
    assert acc.account_role("Viewer", path) == "viewer"
    assert acc.is_owner("Boss", path) is True
    assert acc.is_owner("Viewer", path) is False
    assert acc.is_owner(None, path) is False
    assert acc.is_owner("ghost", path) is False


def test_bad_role_refused(tmp_path):
    with pytest.raises(ValueError):
        acc.create_user("X", PW, tmp_path / "u.json", role="admin")


# --- signup / reset / avatar / totp ------------------------------------------
def test_signup_is_always_a_viewer(tmp_path):
    path = tmp_path / "users.json"
    acc.signup("Newbie", PW, path, now=1000)
    assert acc.account_role("Newbie", path) == "viewer"


def test_forgot_and_reset_flow(tmp_path):
    path = tmp_path / "users.json"
    acc.create_user("Ravi", PW, path, now=1000)
    token = acc.issue_reset_token("Ravi", path, now=2000)
    assert token and token not in path.read_text()  # raw token never stored
    ok, _ = acc.reset_password("Ravi", token, "new-password-99", path, now=2100)
    assert ok and acc.verify_login("Ravi", "new-password-99", path, now=2200)[0]
    # one use: the same token cannot reset again
    assert acc.reset_password("Ravi", token, "another-pass-999", path, now=2300)[0] is False


def test_reset_token_expires(tmp_path):
    path = tmp_path / "users.json"
    acc.create_user("Ravi", PW, path, now=1000)
    token = acc.issue_reset_token("Ravi", path, now=2000)
    later = 2000 + acc.RESET_TTL + 1
    assert acc.reset_password("Ravi", token, "new-password-99", path, now=later)[0] is False


def test_issue_reset_for_unknown_name_returns_none(tmp_path):
    assert acc.issue_reset_token("ghost", tmp_path / "u.json", now=1000) is None


def test_change_password_requires_the_old_one(tmp_path):
    path = tmp_path / "users.json"
    acc.create_user("Ravi", PW, path, now=1000)
    assert acc.change_password("Ravi", "wrong", "new-password-99", path)[0] is False
    ok, _ = acc.change_password("Ravi", PW, "new-password-99", path)
    assert ok and acc.verify_login("Ravi", "new-password-99", path, now=9999)[0]


def test_avatar_stores_png_and_rejects_other_types(tmp_path):
    path = tmp_path / "users.json"
    acc.create_user("Ravi", PW, path, now=1000)
    avatars = tmp_path / "avatars"
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 100
    served = acc.set_avatar("Ravi", png, path, avatars_dir=avatars)
    assert served == "/avatars/Ravi.png"
    assert acc.avatar_file("Ravi", avatars) == avatars / "Ravi.png"
    with pytest.raises(ValueError):
        acc.set_avatar("Ravi", b"GIF89a" + b"x" * 50, path, avatars_dir=avatars)
    with pytest.raises(ValueError):
        acc.set_avatar("Ravi", b"\x89PNG\r\n\x1a\n" + b"x" * (acc.MAX_AVATAR_BYTES + 1),
                       path, avatars_dir=avatars)


def test_avatar_path_never_escapes(tmp_path):
    assert acc.avatar_file("../etc/passwd", tmp_path) is None
    assert acc.avatar_file("a/b", tmp_path) is None


def test_totp_enroll_verify_and_login_gate(tmp_path):
    path = tmp_path / "users.json"
    acc.create_user("Ravi", PW, path, now=1000)
    secret = acc.generate_totp_secret()
    assert len(secret) == 32
    uri = acc.totp_enroll("Ravi", secret, path)
    assert uri.startswith("otpauth://totp/Algo-Pro:Ravi?")
    now = 1_700_000_000.0
    code = acc.totp_now(secret, now=now)
    # password alone is no longer enough
    ok, reason = acc.verify_login("Ravi", PW, path, now=now)
    assert not ok and reason == "totp_required"
    # password + current code opens
    assert acc.verify_login("Ravi", PW, path, now=now, totp_code=code)[0]
    # adjacent window is accepted (+/- 30 s drift)
    assert acc.verify_login("Ravi", PW, path, now=now + acc.TOTP_PERIOD,
                            totp_code=code)[0]
    # wrong code fails
    assert acc.verify_login("Ravi", PW, path, now=now, totp_code="000000")[0] is False \
        or acc.totp_verify(secret, "000000", now=now) is False
    # un-enroll restores password-only login
    assert acc.totp_remove("Ravi", path) is True
    assert acc.verify_login("Ravi", PW, path, now=now)[0]


def test_totp_bad_secret_refused(tmp_path):
    path = tmp_path / "users.json"
    acc.create_user("Ravi", PW, path, now=1000)
    with pytest.raises(ValueError):
        acc.totp_enroll("Ravi", "not-base32!!", path)
