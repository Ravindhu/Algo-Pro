"""START ENGINE signs in to Screener.in and saves the sessionid to .env.

Flow stage: Liquidity screener (credential upkeep; no candidate, no order)
Status: IMPLEMENTED 2026-11-26

What must hold:
1. A successful login writes the new cookie into .env, replacing the old value.
2. The cookie VALUE never leaves the function: not in the return dict, not in the log.
3. Every failure path is reported, not raised: the engine start must not depend on it.
4. No credentials means NO login attempt at all.
"""
import logging

import pytest

from quant.data import screener_session as ss

COOKIE = "secret-cookie-value-123"


class _Reply:
    def __init__(self, ok=True, error=None, data=None):
        self.ok, self.error, self.data = ok, error, data


class _Client:
    """Test seam: records the credentials it was handed, touches no network."""

    base_url = "https://www.screener.in"

    def __init__(self, reply):
        self._reply = reply
        self.calls = []

    def login(self, username, password):
        self.calls.append((username, password))
        return self._reply


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# screener\n"
        "SCREENER_USERNAME = 'me@example.com'\n"
        "SCREENER_PASSWORD = 'pw'\n"
        "Screenerdotin_sessionid='expired'\n"
        "KEEP=1\n",
        encoding="utf-8",
        newline="",
    )
    return path


@pytest.fixture
def logged_in(monkeypatch):
    monkeypatch.setenv("SCREENER_USERNAME", "me@example.com")
    monkeypatch.setenv("SCREENER_PASSWORD", "pw")
    monkeypatch.delenv(ss.SESSION_KEY, raising=False)


def test_success_writes_the_new_cookie_and_keeps_other_lines(env_file, logged_in):
    client = _Client(_Reply(data={"authenticated": True, "sessionid": COOKIE}))
    result = ss.refresh_session(env_file=env_file, client=client)

    assert result == {"ok": True, "saved": True, "reason": "session saved to .env"}
    text = env_file.read_text(encoding="utf-8")
    assert f"Screenerdotin_sessionid='{COOKIE}'" in text
    assert "expired" not in text
    assert "KEEP=1" in text and "SCREENER_USERNAME" in text
    assert client.calls == [("me@example.com", "pw")]


def test_the_cookie_value_is_never_returned(env_file, logged_in):
    client = _Client(_Reply(data={"sessionid": COOKIE}))
    result = ss.refresh_session(env_file=env_file, client=client)
    assert COOKIE not in repr(result)


def test_the_cookie_value_is_never_logged(env_file, logged_in, caplog):
    client = _Client(_Reply(data={"sessionid": COOKIE}))
    with caplog.at_level(logging.DEBUG):
        ss.refresh_session(env_file=env_file, client=client)
    assert COOKIE not in caplog.text


def test_the_password_is_never_logged_on_a_failure(env_file, logged_in, caplog):
    client = _Client(_Reply(ok=False, error="login was not accepted"))
    with caplog.at_level(logging.DEBUG):
        ss.refresh_session(env_file=env_file, client=client)
    assert "pw" not in caplog.text


def test_the_process_environment_is_updated_without_a_restart(env_file, logged_in, monkeypatch):
    import os

    monkeypatch.delenv(ss.SESSION_KEY, raising=False)
    ss.refresh_session(env_file=env_file, client=_Client(_Reply(data={"sessionid": COOKIE})))
    assert os.environ[ss.SESSION_KEY] == COOKIE


def test_no_credentials_means_no_login_attempt(tmp_path, monkeypatch):
    # A .env with NO credentials at all: this is what the fixture must be for this test.
    path = tmp_path / ".env"
    path.write_text("Screenerdotin_sessionid='expired'\n", encoding="utf-8", newline="")
    for name in ss._USERNAME_KEYS + ss._PASSWORD_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(ss.SESSION_KEY, raising=False)
    client = _Client(_Reply(data={"sessionid": COOKIE}))
    result = ss.refresh_session(env_file=path, client=client)
    assert result["ok"] is False and result["saved"] is False
    # The reason names the exact .env keys to fill, not a vague "missing".
    assert result["reason"] == "credentials missing in .env: SCREENER_USERNAME, SCREENER_PASSWORD"
    assert client.calls == []
    assert "expired" in path.read_text(encoding="utf-8")  # untouched


def test_no_credentials_names_only_the_key_that_is_absent(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("SCREENER_USERNAME='has-a-name'\n", encoding="utf-8", newline="")
    for name in ss._USERNAME_KEYS + ss._PASSWORD_KEYS:
        monkeypatch.delenv(name, raising=False)
    result = ss.refresh_session(env_file=path, client=_Client(_Reply(data={"sessionid": COOKIE})))
    assert result["reason"] == "credentials missing in .env: SCREENER_PASSWORD"


def test_a_refused_login_leaves_the_old_cookie_alone(env_file, logged_in):
    result = ss.refresh_session(env_file=env_file, client=_Client(_Reply(ok=False, error="bad password")))
    assert result["ok"] is False and result["saved"] is False
    assert result["reason"].startswith("login failed")
    assert "Screenerdotin_sessionid='expired'" in env_file.read_text(encoding="utf-8")


def test_a_reply_without_a_cookie_is_not_a_success(env_file, logged_in):
    result = ss.refresh_session(env_file=env_file, client=_Client(_Reply(data={})))
    assert result["ok"] is False
    assert "no session cookie" in result["reason"]
    assert "expired" in env_file.read_text(encoding="utf-8")


def test_the_cookie_lands_in_an_untouched_env_when_values_are_unquoted(tmp_path, logged_in):
    """The real .env style is bare ``KEY=value``; that line must be replaced, not duplicated."""
    path = tmp_path / ".env"
    path.write_text("Screenerdotin_sessionid=expired\n", encoding="utf-8", newline="")
    ss.refresh_session(env_file=path, client=_Client(_Reply(data={"sessionid": COOKIE})))
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    assert lines == [f"Screenerdotin_sessionid='{COOKIE}'"]


def test_a_failed_env_write_is_reported_not_raised(env_file, logged_in, monkeypatch):
    """Login ok, .env unwritable: say the session is live but not persisted, never crash."""
    from quant.core import env_write

    def boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(env_write, "write_env_values", boom)
    result = ss.refresh_session(env_file=env_file, client=_Client(_Reply(data={"sessionid": COOKIE})))
    assert result["ok"] is True
    assert result["saved"] is False
    assert ".env not updated" in result["reason"]
    assert "expired" in env_file.read_text(encoding="utf-8")


def test_a_raising_client_is_reported_not_raised(env_file, logged_in):
    class _Angry(_Client):
        def login(self, username, password):
            raise RuntimeError("socket reset")

    result = ss.refresh_session(env_file=env_file, client=_Angry(_Reply()))
    assert result["ok"] is False
    assert "RuntimeError" in result["reason"]


def test_credentials_present_reads_names_only(monkeypatch):
    for name in ss._USERNAME_KEYS + ss._PASSWORD_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SCREENER_USERNAME", "")
    monkeypatch.setenv("SCREENER_PASSWORD", "pw")
    assert ss.credentials_present() is False
    monkeypatch.setenv("SCREENER_USERNAME", "u")
    assert ss.credentials_present() is True


def test_a_blank_generic_alias_does_not_count_as_a_username(monkeypatch):
    """An empty ``username`` in .env must not be handed to the login form as a real name."""
    for name in ss._USERNAME_KEYS + ss._PASSWORD_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("username", "   ")
    monkeypatch.setenv("SCREENER_PASSWORD", "pw")
    assert ss.credentials_present() is False


@pytest.mark.parametrize("raw,expected", [
    ("https://www.screener.in/screens/123/xyz/", "https://www.screener.in"),
    ("www.screener.in", "https://www.screener.in"),
])
def test_base_url_follows_the_configured_screen_url(monkeypatch, raw, expected):
    monkeypatch.setenv("URL_SCREENER_DOT_IN", raw)
    monkeypatch.delenv("SCREENER_BASE_URL", raising=False)
    assert ss._base_url() == expected


def test_base_url_falls_back_when_unset(monkeypatch):
    monkeypatch.delenv("URL_SCREENER_DOT_IN", raising=False)
    monkeypatch.delenv("SCREENER_BASE_URL", raising=False)
    assert ss._base_url() == "https://www.screener.in"


# ---- everything comes from the .env FILE (owner rule 2026-11-26) ------------------------

def test_credentials_are_read_from_the_env_file_even_with_a_clean_shell(tmp_path, monkeypatch):
    """The bot runner may never have sourced .env. The module must read it itself.

    This is the bug the owner caught: with nothing exported, a correct .env still produced
    "credentials missing", so the desk was asked for a username that was already in the file.
    """
    for name in ss._USERNAME_KEYS + ss._PASSWORD_KEYS:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "SCREENER_USERNAME='from-dotenv@example.com'\n"
        "SCREENER_PASSWORD='dotenv-pw'\n",
        encoding="utf-8", newline="")
    client = _Client(_Reply(data={"sessionid": COOKIE}))

    result = ss.refresh_session(env_file=path, client=client)

    assert result["saved"] is True
    assert client.calls == [("from-dotenv@example.com", "dotenv-pw")]


def test_credentials_are_never_taken_from_an_argument_or_a_prompt():
    """The only input is .env: no parameter and no builtin input() on this path."""
    import inspect
    import pathlib

    params = set(inspect.signature(ss.refresh_session).parameters)
    assert not params & {"username", "password", "user", "pass"}

    source = pathlib.Path(ss.__file__).read_text(encoding="utf-8")
    assert "input(" not in source          # never prompt
    assert "getpass" not in source        # never prompt for a secret


def test_env_only_keys_lists_exactly_what_the_module_reads():
    assert set(ss._USERNAME_KEYS) | set(ss._PASSWORD_KEYS) | {"URL_SCREENER_DOT_IN"} == set(ss.ENV_ONLY_KEYS)


def test_the_session_key_is_the_one_the_engine_reads():
    """The cookie is written under the exact name screener_engine._get_client looks for."""
    from pathlib import Path

    engine = Path(__file__).resolve().parents[2] / "src" / "quant" / "engines" / "screener" / "screener_engine.py"
    text = engine.read_text(encoding="utf-8")
    assert f'environ.get("{ss.SESSION_KEY}")' in text
