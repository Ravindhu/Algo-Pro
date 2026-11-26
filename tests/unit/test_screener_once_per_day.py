"""Screener.in sign-in happens ONCE per day, then the id is reused (owner request 2026-11-26).

Status: PHASE 5 IMPLEMENTED (2026-11-26)

The rule under test: a login already done today must NOT be repeated. Two independent
entry points share the same day marker, so the dashboard button and the bot's scraper
cannot disagree about what "already signed in" means.
"""
import os
from pathlib import Path

import pytest

from quant.data import screener_session as ss
from quant.data import screen_refresh as sr

COOKIE = "cookie-once-2f7"


class _Reply:
    def __init__(self, ok=True, error=None, data=None):
        self.ok, self.error, self.data = ok, error, data


class _Client:
    base_url = "https://www.screener.in"

    def __init__(self, reply=None):
        self._reply = reply or _Reply(data={"sessionid": COOKIE})
        self.calls = 0

    def login(self, username, password):
        self.calls += 1
        return self._reply


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("SCREENER_USERNAME = 'me@example.com'\nSCREENER_PASSWORD = 'pw'\n",
                    encoding="utf-8", newline="")
    monkeypatch.setenv("SCREENER_USERNAME", "me@example.com")
    monkeypatch.setenv("SCREENER_PASSWORD", "pw")
    monkeypatch.delenv(ss.SESSION_KEY, raising=False)
    return path


# ---- refresh_session: once per day ------------------------------------------------------

def test_first_login_then_the_second_call_reuses_without_a_login(env_file):
    first = _Client()
    r1 = ss.refresh_session(env_file=env_file, client=first)
    assert r1["saved"] is True and first.calls == 1

    second = _Client()
    r2 = ss.refresh_session(env_file=env_file, client=second)
    assert r2["ok"] is True and r2["saved"] is True
    assert r2["reason"] == "session reused from today"
    assert second.calls == 0, "a same-day session must not trigger another login"


def test_reuse_today_false_forces_a_fresh_login(env_file):
    ss.refresh_session(env_file=env_file, client=_Client())
    forced = _Client()
    r = ss.refresh_session(env_file=env_file, client=forced, reuse_today=False)
    assert forced.calls == 1 and r["saved"] is True


def test_a_yesterdays_marker_does_not_block_a_login(env_file, monkeypatch):
    ss.mark_signed_in_today(env_file)
    marker = ss._marker_path(env_file)
    marker.write_text("2000-01-01\n", encoding="utf-8")  # a stale day
    monkeypatch.delenv(ss.SESSION_KEY, raising=False)
    client = _Client()
    ss.refresh_session(env_file=env_file, client=client)
    assert client.calls == 1


def test_a_marker_without_a_cookie_still_logs_in(env_file, monkeypatch):
    """The .env cookie was removed by hand, so the marker alone is not proof of a session."""
    ss.mark_signed_in_today(env_file)
    monkeypatch.delenv(ss.SESSION_KEY, raising=False)
    client = _Client()
    ss.refresh_session(env_file=env_file, client=client)
    assert client.calls == 1


def test_a_failed_login_does_not_mark_the_day(env_file):
    ss.refresh_session(env_file=env_file, client=_Client(_Reply(ok=False, error="bad pw")))
    assert not ss._saved_today(env_file)


def test_the_marker_is_not_inside_the_env_file(env_file):
    ss.refresh_session(env_file=env_file, client=_Client())
    assert ss._marker_path(env_file).parent.name == "screened"
    # the marker is a separate file, never a .env key (it is not a secret)
    assert "screener_session_day" not in env_file.read_text(encoding="utf-8")


# ---- ensure_session: the scraper's entry point ------------------------------------------

def test_ensure_session_reuses_the_day_marker_with_no_network(env_file, monkeypatch):
    ss.mark_signed_in_today(env_file)
    monkeypatch.setenv(ss.SESSION_KEY, COOKIE)

    def _boom(*_a, **_k):
        raise AssertionError("ensure_session must not log in when today is already marked")

    monkeypatch.setattr(ss, "refresh_session", _boom)
    out = sr.ensure_session(env_file=env_file)
    assert out["usable"] is True and out["action"] == "reused"


def test_ensure_session_uses_an_existing_cookie_with_no_login(env_file, monkeypatch):
    monkeypatch.setenv(ss.SESSION_KEY, COOKIE)

    def _boom(*_a, **_k):
        raise AssertionError("a present cookie needs no login")

    monkeypatch.setattr(ss, "refresh_session", _boom)
    out = sr.ensure_session(env_file=env_file)
    assert out["usable"] is True and out["action"] == "cookie present"


def test_ensure_session_logs_in_exactly_once_when_nothing_exists(env_file, monkeypatch):
    monkeypatch.delenv(ss.SESSION_KEY, raising=False)
    client = _Client()
    monkeypatch.setattr(ss, "_client", lambda base_url: client)

    out1 = sr.ensure_session(env_file=env_file)
    assert out1["action"] == "logged in" and client.calls == 1
    out2 = sr.ensure_session(env_file=env_file)   # the second call, same process
    assert out2["action"] == "reused"
    assert client.calls == 1, "the day marker must prevent a second login"


def test_ensure_session_reports_unusable_when_the_login_fails(env_file, monkeypatch):
    monkeypatch.delenv(ss.SESSION_KEY, raising=False)
    monkeypatch.setattr(ss, "_client", lambda base_url: _Client(_Reply(ok=False, error="bad pw")))
    out = sr.ensure_session(env_file=env_file)
    assert out["usable"] is False and out["action"] == "failed"


# ---- the scraper writes the bot's source file -------------------------------------------

def test_scraper_writes_last_screen_and_reports_the_count(tmp_path, env_file, monkeypatch):
    monkeypatch.setenv(ss.SESSION_KEY, COOKIE)
    target = tmp_path / "data" / "screened" / "last_screen.json"
    payload = {"ok": True, "data": {"selected": ["AAA", "BBB"], "symbols": ["AAA", "BBB"]}}
    scrape = sr.make_screener_scraper(source=target, env_file=env_file,
                                      run_engine=lambda: payload)
    out = scrape()
    assert out["ok"] is True and out["count"] == 2
    import json
    assert json.loads(target.read_text(encoding="utf-8"))["ok"] is True


def test_scraper_reports_a_failed_engine_without_raising(tmp_path, env_file, monkeypatch):
    monkeypatch.setenv(ss.SESSION_KEY, COOKIE)
    scrape = sr.make_screener_scraper(source=tmp_path / "last.json", env_file=env_file,
                                      run_engine=lambda: {"ok": False, "error": "login required"})
    out = scrape()
    assert out["ok"] is False
    assert "login required" in out["reason"]


def test_scraper_does_not_run_the_engine_without_a_session(tmp_path, env_file, monkeypatch):
    monkeypatch.delenv(ss.SESSION_KEY, raising=False)
    monkeypatch.setattr(ss, "_client", lambda base_url: _Client(_Reply(ok=False, error="no")))
    called = []
    scrape = sr.make_screener_scraper(source=tmp_path / "last.json", env_file=env_file,
                                      run_engine=lambda: called.append(1) or {"ok": True})
    out = scrape()
    assert out["ok"] is False and called == []


# ---- the 5-minute loop scrapes only when the source is stale -----------------------------

def test_refresh_scrapes_only_when_the_source_is_missing_or_old(tmp_path):
    source = tmp_path / "last.json"
    calls = []

    def scrape():
        calls.append(1)
        import json
        source.write_text(json.dumps({"results": [{"symbol": "AAA", "selected": True}]}),
                          encoding="utf-8")
        return {"ok": True, "count": 1}

    class Reader:
        def quotes(self, key, symbol, exchange):
            return {"status": "success",
                    "data": {"upper_circuit_limit": 110.0, "lower_circuit_limit": 90.0,
                             "prev_close": 100.0, "asm": False, "gsm": False}}

    refresh = sr.make_screen_refresher(Reader(), "KEY", source=source, target=tmp_path / "latest.json",
                                       scrape=scrape, scrape_every_minutes=60)
    from datetime import datetime
    now = datetime(2026, 11, 26, 10, 0)

    refresh(now)                 # source missing -> scrape
    assert len(calls) == 1
    refresh(now)                 # source now fresh -> NO scrape
    assert len(calls) == 1


def test_a_failed_scrape_does_not_stop_the_refresh(tmp_path):
    source = tmp_path / "last.json"

    def scrape():
        raise RuntimeError("site down")

    class Reader:
        def quotes(self, key, symbol, exchange):
            raise RuntimeError("no quotes either")

    refresh = sr.make_screen_refresher(Reader(), "KEY", source=source, target=tmp_path / "latest.json",
                                       scrape=scrape)
    from datetime import datetime
    out = refresh(datetime(2026, 11, 26, 10, 0))
    assert out["exit_code"] == 2
    assert out["scrape"]["ok"] is False
