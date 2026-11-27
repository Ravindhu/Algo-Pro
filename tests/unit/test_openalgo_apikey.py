"""Save OpenAlgo's current API key after a login (2026-11-27, read the existing key).

Rules under test:
1. The key is READ from GET /apikey. Nothing is generated, and no POST is ever sent.
2. _restored/.env is updated only when it exists and the value differs; other lines are kept.
   The file is never created.
3. The key value never appears in a step, a result or a log line.
4. A key problem never fails the broker login (result.ok stays True).
"""
import logging

import pytest

from quant.core import openalgo_apikey as apikey
from quant.engines.core import broker_auth as ba

KEY = "ak_test_0123456789abcdef"


@pytest.fixture(autouse=True)
def _never_touch_the_real_repo_env(tmp_path, monkeypatch):
    """sync_api_key also writes the repo-root .env (2026-11-28). Point it at a temp file in every test."""
    monkeypatch.setattr(apikey, "REPO_ENV", tmp_path / "repo_root.env")


class FakeSession:
    def __init__(self, body=None, ok=True, raises=None):
        self.body, self.ok, self.raises = body, ok, raises
        self.paths = []
        self.posts = 0

    def get(self, path):
        self.paths.append(path)
        if self.raises:
            raise self.raises
        return {"ok": self.ok, "status": 200, "body": self.body}

    def post_form(self, *a, **k):
        self.posts += 1
        raise AssertionError("no POST may be sent for the key step")

    def post_json(self, *a, **k):
        self.posts += 1
        raise AssertionError("no POST may be sent for the key step")


def _auth(session):
    obj = ba.BrokerAuth.__new__(ba.BrokerAuth)
    obj.session = session
    return obj


def test_fetch_returns_existing_key_and_only_uses_get():
    s = FakeSession({"has_api_key": True, "api_key": KEY})
    out = apikey.fetch_key(s.get)
    assert out == {"key": KEY, "reason": ""}
    assert s.paths == ["/apikey"]


def test_fetch_says_no_key_yet_without_generating_one():
    out = apikey.fetch_key(FakeSession({"has_api_key": False, "api_key": None}).get)
    assert out["key"] is None and "create one at /apikey" in out["reason"]


def test_fetch_handles_a_failed_page():
    assert apikey.fetch_key(FakeSession(body=None, ok=False).get)["key"] is None


def test_sync_never_creates_the_env_file(tmp_path):
    env = tmp_path / "_restored" / ".env"
    assert apikey.sync_env(KEY, env).startswith("skipped")
    assert not env.exists()


def test_sync_unchanged_leaves_the_file_byte_identical(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"OPENALGO_API_KEY={KEY}\nOTHER=1\n", encoding="utf-8")
    before = env.read_bytes()
    assert apikey.sync_env(KEY, env) == "unchanged"
    assert env.read_bytes() == before


def test_sync_updates_only_the_key_line(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# keep me\nOPENALGO_API_KEY=old\nOTHER=1\n", encoding="utf-8")
    assert apikey.sync_env(KEY, env) == "updated"
    text = env.read_text(encoding="utf-8")
    assert apikey.read_env_value(env, "OPENALGO_API_KEY") == KEY
    assert "# keep me" in text and "OTHER=1" in text and "old" not in text


def test_sync_step_records_outcome_and_never_the_key(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OPENALGO_API_KEY=old\n", encoding="utf-8")
    monkeypatch.setattr(apikey, "RESTORED_ENV", env)
    result = ba.AuthResult(ok=True)
    out = _auth(FakeSession({"has_api_key": True, "api_key": KEY})).sync_api_key(result)
    assert out.ok is True
    step = out.steps[-1]
    assert step["step"] == "openalgo_apikey" and step["ok"] is True and step["detail"] == "updated"
    assert KEY not in repr(out.to_dict())


def test_key_step_failure_does_not_fail_the_login(monkeypatch):
    result = ba.AuthResult(ok=True)
    out = _auth(FakeSession(raises=RuntimeError("secret " + KEY))).sync_api_key(result)
    assert out.ok is True
    assert out.steps[-1]["detail"] == "failed (RuntimeError)"
    assert KEY not in repr(out.to_dict())


def test_no_key_yet_is_reported_as_a_step_not_a_failure():
    result = ba.AuthResult(ok=True)
    out = _auth(FakeSession({"has_api_key": False})).sync_api_key(result)
    assert out.ok is True and out.steps[-1]["ok"] is False
    assert "create one at /apikey" in out.steps[-1]["detail"]


def test_key_value_never_reaches_the_log(tmp_path, monkeypatch, caplog):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(apikey, "RESTORED_ENV", env)
    with caplog.at_level(logging.DEBUG):
        _auth(FakeSession({"has_api_key": True, "api_key": KEY})).sync_api_key(ba.AuthResult(ok=True))
    assert KEY not in caplog.text
