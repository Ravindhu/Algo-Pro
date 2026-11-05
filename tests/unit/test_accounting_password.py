"""Local password gate for the accounting view (Priority B, step B3)."""
import pytest

from quant.reporting.accounting import (
    DEFAULT_PASSWORD_FILE,
    MIN_PASSWORD_LEN,
    authorise_accounting,
    hash_password,
    password_file,
    set_password,
    verify_password,
)

GOOD = "correct horse battery"   # 21 chars


def test_hash_is_salted_and_never_contains_plain_password():
    a = hash_password(GOOD)
    b = hash_password(GOOD)
    assert a != b                       # random salt each time
    assert GOOD not in a
    assert a.startswith("scrypt$")


def test_verify_accepts_right_password_and_rejects_wrong():
    stored = hash_password(GOOD)
    assert verify_password(GOOD, stored)
    assert not verify_password(GOOD + "x", stored)
    assert not verify_password("", stored)


def test_short_password_rejected_at_hash_time():
    with pytest.raises(ValueError):
        hash_password("x" * (MIN_PASSWORD_LEN - 1))


@pytest.mark.parametrize("bad", ["", "not-a-hash", "md5$1$2$3$aa$bb", "scrypt$x$y$z$zz$zz"])
def test_malformed_stored_hash_fails_closed(bad):
    assert verify_password(GOOD, bad) is False


def test_set_password_writes_hash_not_plain_text(tmp_path):
    target = tmp_path / "pw.hash"
    set_password(GOOD, target)
    text = target.read_text(encoding="utf-8")
    assert GOOD not in text
    assert verify_password(GOOD, text.strip())


def test_authorise_requires_role_then_password(tmp_path):
    target = tmp_path / "pw.hash"
    set_password(GOOD, target)
    authorise_accounting("accountant", GOOD, target)  # passes
    with pytest.raises(PermissionError):
        authorise_accounting("trader", GOOD, target)   # wrong role
    with pytest.raises(PermissionError):
        authorise_accounting("accountant", "wrong password!!", target)


def test_authorise_fails_when_no_password_set(tmp_path):
    with pytest.raises(PermissionError):
        authorise_accounting("owner", GOOD, tmp_path / "missing.hash")


def test_error_message_never_echoes_password(tmp_path):
    target = tmp_path / "pw.hash"
    set_password(GOOD, target)
    with pytest.raises(PermissionError) as err:
        authorise_accounting("owner", "SECRET-GUESS-123", target)
    assert "SECRET-GUESS-123" not in str(err.value)


def test_password_file_resolution_order(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.hash"
    assert password_file(explicit) == explicit
    monkeypatch.setenv("ACCOUNTING_PASSWORD_FILE", str(tmp_path / "env.hash"))
    assert password_file() == tmp_path / "env.hash"
    monkeypatch.delenv("ACCOUNTING_PASSWORD_FILE")
    assert password_file() == DEFAULT_PASSWORD_FILE
    assert DEFAULT_PASSWORD_FILE.parts[-2] == "logs"   # git-ignored location


# --- lockout after repeated wrong passwords ------------------------------------

from quant.reporting.accounting import (  # noqa: E402
    LOCKOUT_SECONDS,
    MAX_FAILURES,
    clear_lockout,
)


def _fail_n(target, n, now):
    for _ in range(n):
        with pytest.raises(PermissionError):
            authorise_accounting("owner", "wrong password!!", target, now=now)


def test_lockout_after_max_failures_blocks_even_correct_password(tmp_path):
    target = tmp_path / "pw.hash"
    set_password(GOOD, target)
    _fail_n(target, MAX_FAILURES, now=1000.0)
    with pytest.raises(PermissionError) as err:
        authorise_accounting("owner", GOOD, target, now=1001.0)
    assert "locked" in str(err.value)
    assert GOOD not in str(err.value)


def test_lockout_expires_after_window(tmp_path):
    target = tmp_path / "pw.hash"
    set_password(GOOD, target)
    _fail_n(target, MAX_FAILURES, now=1000.0)
    authorise_accounting("owner", GOOD, target, now=1000.0 + LOCKOUT_SECONDS + 1)


def test_success_resets_failure_counter(tmp_path):
    target = tmp_path / "pw.hash"
    set_password(GOOD, target)
    _fail_n(target, MAX_FAILURES - 1, now=1000.0)
    authorise_accounting("owner", GOOD, target, now=1001.0)   # resets
    _fail_n(target, MAX_FAILURES - 1, now=1002.0)             # still under the limit
    authorise_accounting("owner", GOOD, target, now=1003.0)   # not locked


def test_corrupt_lockout_file_fails_closed(tmp_path):
    target = tmp_path / "pw.hash"
    set_password(GOOD, target)
    (tmp_path / "pw.hash.lockout.json").write_text("not json", encoding="utf-8")
    with pytest.raises(PermissionError):
        authorise_accounting("owner", GOOD, target, now=1000.0)


def test_clear_lockout_removes_the_lock(tmp_path):
    target = tmp_path / "pw.hash"
    set_password(GOOD, target)
    _fail_n(target, MAX_FAILURES, now=1000.0)
    clear_lockout(target)
    authorise_accounting("owner", GOOD, target, now=1001.0)


def test_wrong_role_does_not_count_as_password_failure(tmp_path):
    target = tmp_path / "pw.hash"
    set_password(GOOD, target)
    for _ in range(MAX_FAILURES + 2):
        with pytest.raises(PermissionError):
            authorise_accounting("trader", GOOD, target, now=1000.0)
    authorise_accounting("owner", GOOD, target, now=1000.0)   # role check ran first


# --- setup script ----------------------------------------------------------------

def _load_script():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "set_accounting_password.py"
    spec = importlib.util.spec_from_file_location("set_accounting_password", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_sets_password_when_both_entries_match(tmp_path, monkeypatch, capsys):
    script = _load_script()
    answers = iter([GOOD, GOOD])
    monkeypatch.setattr(script.getpass, "getpass", lambda prompt="": next(answers))
    target = tmp_path / "pw.hash"
    assert script.main(["--path", str(target)]) == 0
    assert verify_password(GOOD, target.read_text(encoding="utf-8").strip())
    assert GOOD not in capsys.readouterr().out


def test_script_writes_nothing_when_entries_differ(tmp_path, monkeypatch):
    script = _load_script()
    answers = iter([GOOD, GOOD + "x"])
    monkeypatch.setattr(script.getpass, "getpass", lambda prompt="": next(answers))
    target = tmp_path / "pw.hash"
    assert script.main(["--path", str(target)]) == 1
    assert not target.exists()


def test_script_clear_lockout_flag(tmp_path):
    script = _load_script()
    target = tmp_path / "pw.hash"
    set_password(GOOD, target)
    _fail_n(target, MAX_FAILURES, now=1000.0)
    assert script.main(["--path", str(target), "--clear-lockout"]) == 0
    authorise_accounting("owner", GOOD, target, now=1001.0)
