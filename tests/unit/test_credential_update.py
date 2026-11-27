"""Dashboard credential update with 2FA (2026-11-27).

Rules under test:
1. Only the allow-listed names can change. Unknown names are refused.
2. The owner must have 2FA enrolled and send a correct code.
3. A value with a line break, NUL, empty or too long is refused.
4. Only an existing file is written. Other lines in the file are kept.
5. The value is never returned in the result.
"""
import json
import time

import pytest

from quant.core import credential_update as cu
from quant.core import dashboard_accounts as acc
from quant.core.openalgo_apikey import read_env_value

SECRET = acc.generate_totp_secret()


def _code(now=None):
    stamp = time.time() if now is None else now
    return acc._totp_code(SECRET, int(stamp // acc.TOTP_PERIOD))


@pytest.fixture
def env(tmp_path):
    users = tmp_path / "users.json"
    users.write_text(json.dumps({"users": {"owner": {"totp_secret": SECRET}, "viewer": {}}}), encoding="utf-8")
    restored = tmp_path / "_restored" / ".env"
    restored.parent.mkdir(parents=True)
    restored.write_text("# keep me\nOPENALGO_USERNAME=old\nOTHER=1\n", encoding="utf-8")
    repo = tmp_path / ".env"
    repo.write_text("SCREENER_USERNAME=old\n", encoding="utf-8")
    files = {"OPENALGO_USERNAME": restored, "SCREENER_USERNAME": repo}
    return {"users": users, "files": files, "restored": restored, "repo": repo}


def test_allow_list_is_the_four_tabs_editable_names():
    assert [g["id"] for g in cu.GROUPS] == ["broker", "openalgo", "screener", "llm"]
    assert "OPENALGO_USERNAME" in cu.ALLOWED and "OPENROUTER_API_KEY" in cu.ALLOWED
    assert "OPENROUTER_MODEL" in cu.ALLOWED and "URL_SCREENER_DOT_IN" in cu.ALLOWED
    # OPENALGO_API_KEY is shown only: the login flow keeps it in step.
    assert "OPENALGO_API_KEY" not in cu.ALLOWED


def test_api_key_is_display_only_and_refused(env):
    ok, _ = cu.update("OPENALGO_API_KEY", "x", owner="owner", totp_code=_code(),
                      users_path=env["users"], files=env["files"])
    assert ok is False


def test_status_shows_four_char_preview_not_the_secret(env, tmp_path):
    env["restored"].write_text("BROKER_PASSWORD=Sup3rSecretPass\nBROKER_API_KEY=ab\n", encoding="utf-8")
    fields = {f["name"]: f for g in cu.status() for f in g["fields"]}
    # status() reads the real GROUPS paths; check the helper directly for the rule.
    assert cu.preview_of("Sup3rSecretPass", "password") == "Sup3…"
    assert cu.preview_of("ab", "key") == "**"                     # short values reveal nothing
    assert cu.preview_of("https://x.example", "url") == "https://x.example"
    assert "BROKER_USERNAME" in fields and fields["BROKER_USERNAME"]["set"] is False


def test_status_never_returns_a_full_secret(env, monkeypatch):
    secret = "Full-Secret-Value-123"
    env["restored"].write_text(f"BROKER_PASSWORD={secret}\n", encoding="utf-8")
    monkeypatch.setattr(cu, "GROUPS", [{"id": "b", "title": "B", "fields": [
        {"name": "BROKER_PASSWORD", "kind": "password", "file": env["restored"], "editable": True}]}])
    text = repr(cu.status())
    assert secret not in text and "Full…" in text   # the first 4 characters only


def test_unknown_name_refused(env):
    ok, reason = cu.update("PATH", "x", owner="owner", totp_code=_code(), users_path=env["users"],
                           files=env["files"])
    assert ok is False and "cannot be changed" in reason


def test_wrong_totp_refused_and_file_untouched(env):
    before = env["restored"].read_text(encoding="utf-8")
    ok, reason = cu.update("OPENALGO_USERNAME", "new", owner="owner", totp_code="000000",
                           users_path=env["users"], files=env["files"])
    assert ok is False and "2FA" in reason
    assert env["restored"].read_text(encoding="utf-8") == before


def test_owner_without_enrolled_2fa_refused(env):
    env["users"].write_text(json.dumps({"users": {"owner": {}}}), encoding="utf-8")
    ok, _ = cu.update("OPENALGO_USERNAME", "new", owner="owner", totp_code=_code(),
                      users_path=env["users"], files=env["files"])
    assert ok is False


def test_correct_totp_updates_only_that_line(env):
    ok, reason = cu.update("OPENALGO_USERNAME", "newuser", owner="owner", totp_code=_code(),
                           users_path=env["users"], files=env["files"])
    assert ok is True and reason.startswith("saved")
    text = env["restored"].read_text(encoding="utf-8")
    assert read_env_value(env["restored"], "OPENALGO_USERNAME") == "newuser"
    assert "# keep me" in text and "OTHER=1" in text


def test_repo_env_names_go_to_repo_file(env):
    ok, _ = cu.update("SCREENER_USERNAME", "me@example.com", owner="owner", totp_code=_code(),
                      users_path=env["users"], files=env["files"])
    assert ok is True
    assert read_env_value(env["repo"], "SCREENER_USERNAME") == "me@example.com"


@pytest.mark.parametrize("bad", ["", "a\nb", "a\rb", "a\x00b", "x" * 257])
def test_bad_values_refused(env, bad):
    ok, reason = cu.update("OPENALGO_USERNAME", bad, owner="owner", totp_code=_code(),
                           users_path=env["users"], files=env["files"])
    assert ok is False and "value" in reason


def test_missing_target_file_is_created_with_only_that_key(env, tmp_path):
    missing = tmp_path / "nope" / ".env"
    ok, reason = cu.update("OPENALGO_USERNAME", "newuser", owner="owner", totp_code=_code(),
                           users_path=env["users"], files={"OPENALGO_USERNAME": missing})
    assert ok is True and "saved to" in reason
    assert read_env_value(missing, "OPENALGO_USERNAME") == "newuser"
    assert missing.read_text(encoding="utf-8").count("=") == 1      # nothing else in it
    assert (missing.stat().st_mode & 0o777) == 0o600


def test_result_never_contains_the_value(env):
    secret_value = "Sup3r-secret-value"
    ok, reason = cu.update("BROKER_PASSWORD", secret_value, owner="owner", totp_code=_code(),
                           users_path=env["users"], files={"BROKER_PASSWORD": env["restored"]})
    assert secret_value not in reason
