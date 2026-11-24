"""Dashboard .env editor: only allowed keys, values never returned, atomic 0600 write, no value in errors."""
import os
import stat

import pytest

from quant.core import env_editor as ee


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ee.EDITABLE_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_status_reports_set_without_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text("BROKER_PASSWORD=supersecret123\nOTHER=1\n", encoding="utf-8")
    st = ee.status(path)
    assert st["BROKER_PASSWORD"] is True
    assert st["BROKER_TOTP_SECRET"] is False
    assert "supersecret123" not in repr(st)


def test_save_writes_value_and_keeps_other_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# my notes\nDASHBOARD_SESSION_SECRET=keepme\n", encoding="utf-8")
    changed = ee.apply_updates({"BROKER_PASSWORD": "pw#1 two"}, path)
    text = path.read_text(encoding="utf-8")
    assert changed == ["BROKER_PASSWORD"]
    assert "# my notes" in text and "DASHBOARD_SESSION_SECRET=keepme" in text
    assert 'BROKER_PASSWORD="pw#1 two"' in text


def test_empty_value_keeps_saved_value(tmp_path):
    path = tmp_path / ".env"
    path.write_text("BROKER_PASSWORD=old\n", encoding="utf-8")
    assert ee.apply_updates({"BROKER_PASSWORD": ""}, path) == []
    assert path.read_text(encoding="utf-8") == "BROKER_PASSWORD=old\n"


def test_session_keys_are_not_editable(tmp_path):
    with pytest.raises(ee.EnvEditError) as err:
        ee.apply_updates({"DASHBOARD_SESSION_SECRET": "x"}, tmp_path / ".env")
    assert "not editable" in str(err.value)


def test_bad_value_refused_and_error_never_shows_value(tmp_path):
    path = tmp_path / ".env"
    with pytest.raises(ee.EnvEditError) as err:
        ee.apply_updates({"BROKER_PASSWORD": "line1\nline2"}, path)
    assert "line2" not in str(err.value) and "line1" not in str(err.value)
    assert not path.exists()


def test_whole_save_refused_if_any_key_is_bad(tmp_path):
    path = tmp_path / ".env"
    with pytest.raises(ee.EnvEditError):
        ee.apply_updates({"BROKER_PASSWORD": "ok", "NOT_ALLOWED": "x"}, path)
    assert not path.exists()


def test_file_is_owner_only_and_env_updated(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKER_TOTP_SECRET", "")  # restore the real value after the test
    path = tmp_path / ".env"
    ee.apply_updates({"BROKER_TOTP_SECRET": "ABCDEF234567"}, path)
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert os.environ["BROKER_TOTP_SECRET"] == "ABCDEF234567"
    assert ee.status(path)["BROKER_TOTP_SECRET"] is True
