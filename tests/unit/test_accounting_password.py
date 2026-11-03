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
