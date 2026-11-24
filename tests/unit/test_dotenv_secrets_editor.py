"""Dashboard secrets editor: encrypted at rest, only allowed keys, values never returned or logged."""
import json
import os
import stat

import pytest

from quant.core import env_editor as ee


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ee.EDITABLE_KEYS:
        monkeypatch.setenv(key, "")  # restored after each test
        monkeypatch.delenv(key, raising=False)


def _paths(tmp_path):
    return tmp_path / "secrets.enc", tmp_path / "secrets.key", tmp_path / "missing.env"


def test_saved_value_is_encrypted_on_disk(tmp_path):
    store, key, env = _paths(tmp_path)
    ee.apply_updates({"BROKER_PASSWORD": "plain-pw-XYZ"}, store, key)
    raw = store.read_bytes()
    assert b"plain-pw-XYZ" not in raw
    assert b"BROKER_PASSWORD" not in raw


def test_status_reports_set_without_values(tmp_path):
    store, key, env = _paths(tmp_path)
    ee.apply_updates({"BROKER_PASSWORD": "supersecret123"}, store, key)
    st = ee.status(store, key, env)
    assert st["BROKER_PASSWORD"] is True
    assert st["BROKER_TOTP_SECRET"] is False
    assert "supersecret123" not in repr(st)


def test_empty_value_keeps_saved_value(tmp_path):
    store, key, env = _paths(tmp_path)
    ee.apply_updates({"BROKER_PASSWORD": "old-pw"}, store, key)
    assert ee.apply_updates({"BROKER_PASSWORD": ""}, store, key) == []
    assert ee._read_store(store, key) == {"BROKER_PASSWORD": "old-pw"}


def test_load_saved_secrets_fills_env_but_env_wins(tmp_path, monkeypatch):
    store, key, env = _paths(tmp_path)
    ee.apply_updates({"BROKER_TOTP_SECRET": "ABCDEF234567", "BROKER_USERNAME": "saved-user"}, store, key)
    for name in ("BROKER_TOTP_SECRET", "BROKER_USERNAME"):  # simulate a fresh process
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BROKER_USERNAME", "shell-user")
    loaded = ee.load_saved_secrets(store, key)
    assert loaded == ["BROKER_TOTP_SECRET"]
    assert os.environ["BROKER_TOTP_SECRET"] == "ABCDEF234567"
    assert os.environ["BROKER_USERNAME"] == "shell-user"


def test_wrong_key_reads_as_not_set_and_does_not_crash(tmp_path):
    store, key, env = _paths(tmp_path)
    ee.apply_updates({"BROKER_PASSWORD": "pw-one"}, store, key)
    os.environ.pop("BROKER_PASSWORD", None)  # simulate a fresh process
    from cryptography.fernet import Fernet
    key.write_bytes(Fernet.generate_key() + b"\n")
    assert ee.status(store, key, env)["BROKER_PASSWORD"] is False
    assert ee.load_saved_secrets(store, key) == []


def test_session_keys_are_not_editable(tmp_path):
    store, key, env = _paths(tmp_path)
    with pytest.raises(ee.EnvEditError) as err:
        ee.apply_updates({"DASHBOARD_SESSION_SECRET": "x"}, store, key)
    assert "not editable" in str(err.value)


def test_bad_value_refused_and_error_never_shows_value(tmp_path):
    store, key, env = _paths(tmp_path)
    with pytest.raises(ee.EnvEditError) as err:
        ee.apply_updates({"BROKER_PASSWORD": "line1\nline2"}, store, key)
    assert "line1" not in str(err.value) and "line2" not in str(err.value)
    assert not store.exists()


def test_whole_save_refused_if_any_key_is_bad(tmp_path):
    store, key, env = _paths(tmp_path)
    with pytest.raises(ee.EnvEditError):
        ee.apply_updates({"BROKER_PASSWORD": "ok", "not_upper": "x"}, store, key)
    assert not store.exists()


def test_files_are_owner_only(tmp_path):
    store, key, env = _paths(tmp_path)
    ee.apply_updates({"BROKER_PASSWORD": "pw"}, store, key)
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(store).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(key).st_mode) == 0o600


def test_legacy_env_value_still_counts_as_set(tmp_path):
    store, key, env = _paths(tmp_path)
    env.write_text("OPENALGO_USERNAME=legacy-user\n", encoding="utf-8")
    assert ee.status(store, key, env)["OPENALGO_USERNAME"] is True
