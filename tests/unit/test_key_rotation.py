"""Automatic login-key rotation (src/quant/core/key_rotation.py)."""
import os
import stat

import pytest

from quant.core import key_rotation as kr

DAY = 86400.0


def _key(tmp_path, text="old-key-value", age_s=0.0):
    path = tmp_path / "data" / "session_secret.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    stamp = 1_000_000_000.0
    os.utime(path, (stamp, stamp))
    return path, stamp + age_s  # "now" for the check


def test_key_not_yet_due_is_left_alone(tmp_path):
    path, now = _key(tmp_path, age_s=2 * DAY)
    rotated, reason = kr.rotate_if_due(path, 30 * DAY, now=now)
    assert (rotated, reason) == (False, "not due")
    assert path.read_text(encoding="utf-8").strip() == "old-key-value"


def test_due_key_is_replaced_with_new_owner_only_value(tmp_path):
    path, now = _key(tmp_path, age_s=31 * DAY)
    rotated, reason = kr.rotate_if_due(path, 30 * DAY, now=now)
    assert rotated and reason == "rotated"
    new = path.read_text(encoding="utf-8").strip()
    assert new != "old-key-value" and len(new) >= 40
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # The new key is fresh, so it is not due again straight away.
    assert kr.rotate_if_due(path, 30 * DAY, now=now)[0] is False


def test_env_pin_is_never_rotated(tmp_path):
    path, now = _key(tmp_path, age_s=400 * DAY)
    rotated, reason = kr.rotate_if_due(path, 30 * DAY, now=now, env_pinned=True)
    assert rotated is False and "pinned" in reason
    assert path.read_text(encoding="utf-8").strip() == "old-key-value"


def test_zero_max_age_turns_rotation_off(tmp_path):
    path, now = _key(tmp_path, age_s=400 * DAY)
    assert kr.rotate_if_due(path, 0, now=now)[0] is False


def test_missing_key_file_is_not_created_here(tmp_path):
    path = tmp_path / "data" / "session_secret.key"
    assert kr.rotate_if_due(path, 30 * DAY) == (False, "no key file yet")
    assert not path.exists()


def test_failed_write_keeps_old_key(tmp_path, monkeypatch):
    path, now = _key(tmp_path, age_s=31 * DAY)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(kr, "_write_private", boom)
    rotated, reason = kr.rotate_if_due(path, 30 * DAY, now=now)
    assert rotated is False and "old key kept" in reason
    assert path.read_text(encoding="utf-8").strip() == "old-key-value"


@pytest.mark.parametrize("raw, days", [
    ("", 30), ("abc", 30), ("-5", 30), ("0", 0), ("7", 7), ("2.5", 2.5),
])
def test_max_age_env_values(raw, days):
    assert kr.max_age_seconds(raw) == days * DAY


def test_max_age_reads_environment_when_no_argument(monkeypatch):
    monkeypatch.setenv("DASHBOARD_KEY_MAX_AGE_DAYS", "14")
    assert kr.max_age_seconds() == 14 * DAY
    monkeypatch.delenv("DASHBOARD_KEY_MAX_AGE_DAYS")
    assert kr.max_age_seconds() == 30 * DAY
