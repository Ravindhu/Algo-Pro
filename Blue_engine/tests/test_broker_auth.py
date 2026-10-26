"""
test_broker_auth.py - headless TOTP login.

Non-negotiables under test:

1. No credential or TOTP ever appears in a result, a log line or a run record.
2. TOTP secrets are validated as base32 and computed with pyotp (6 digits).
3. The two legs are ordered and ISOLATED: a failed platform login never reaches
   the broker callback (otherwise a wrong password burns broker login attempts
   and can lock the account).
4. Login is opt-in (`BROKER_AUTOLOGIN`), and the refusal is explicit.
5. A login response is not trusted - the session is probed.
6. Every leg failure is reported with its leg name, never swallowed.

The flow is exercised against a real loopback HTTP server that mimics the
verified OpenAlgo endpoints (openalgo/blueprints/auth.py, brlogin.py).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List
from urllib.parse import parse_qs

import pytest

pyotp = pytest.importorskip("pyotp")

from blue_engine.core import broker_auth
from blue_engine.core.broker_auth import (
    BrokerAuth,
    CredentialSet,
    OpenAlgoSession,
    verify_totp_secret,
    totp_now,
)

SECRET = "JBSWY3DPEHPK3PXP"          # a well-known test seed, not a real one


# --------------------------------------------------------------------------- #
# A loopback OpenAlgo that behaves like the real endpoints
# --------------------------------------------------------------------------- #
class FakeOpenAlgo:
    """Records every request so the tests can assert ordering and payloads."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.platform_ok = True
        self.broker_ok = True
        self.require_platform_totp = False
        self.session_ok = True
        self.broker_callback_totp_ok = True
        self._cookies: set = set()

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):    # silence the test output
                return

            def _body(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                ctype = self.headers.get("Content-Type", "")
                if "json" in ctype:
                    try:
                        return json.loads(raw or "{}")
                    except json.JSONDecodeError:
                        return {}
                return {k: v[0] for k, v in parse_qs(raw).items()}

            def _send(self, status: int, payload: Any, cookie: str = "") -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append({"method": "GET", "path": self.path})
                if self.path == "/":
                    # The real app serves this to anyone; the SESSION COOKIE is
                    # what proves authentication. A dead session = no cookie.
                    if outer.session_ok:
                        return self._send(200, {"status": "ok"},
                                          cookie="session=alive; Path=/")
                    return self._send(200, {"status": "ok"})
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                body = self._body()
                outer.requests.append({"method": "POST", "path": self.path,
                                       "body": body})
                if self.path == "/auth/login":
                    if not outer.platform_ok:
                        return self._send(401, {"status": "error",
                                                "message": "Invalid credentials"})
                    if outer.require_platform_totp:
                        return self._send(200, {"status": "totp_required"})
                    if outer.session_ok:
                        return self._send(200, {"status": "success"},
                                          cookie="session=abc; Path=/")
                    return self._send(200, {"status": "success"})
                if self.path == "/auth/login/totp":
                    good = bool(body.get("totp_code"))
                    return self._send(200 if good else 401,
                                      {"status": "success" if good else "error"})
                if self.path.startswith("/auth/broker/") and \
                        self.path.endswith("/callback"):
                    if not outer.broker_ok:
                        # Deliberate: the broker callback succeeds/fails on the
                        # broker's own rules. A DEAD SESSION is observed at the
                        # probe (GET / withholds the cookie), not here - that
                        # keeps the two failure modes distinguishable.
                        return self._send(401, {"status": "error",
                                                "error": "Invalid TOTP"})
                    code = str(body.get("totp") or "")
                    totp = pyotp.TOTP(SECRET)
                    if outer.broker_callback_totp_ok and not totp.verify(code):
                        return self._send(401, {"status": "error",
                                                "error": "Invalid TOTP"})
                    return self._send(200, {"status": "success"},
                                      cookie=("session=ready; Path=/"
                                              if outer.session_ok else ""))
                return self._send(404, {"error": "not found"})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def paths(self) -> List[str]:
        return [r["path"] for r in self.requests if r["method"] == "POST"]


@pytest.fixture()
def fake():
    server = FakeOpenAlgo()
    yield server
    server.stop()


def creds(**overrides) -> CredentialSet:
    base = dict(
        broker_totp_secret=SECRET,
        broker_password="1234",
        broker_username="AB1234",
        openalgo_username="algo",
        openalgo_password="s3cret",
        openalgo_totp_secret=None,
        broker_name="angel",
    )
    base.update(overrides)
    return CredentialSet(**base)


# --------------------------------------------------------------------------- #
# TOTP
# --------------------------------------------------------------------------- #
def test_a_valid_base32_seed_is_accepted():
    assert verify_totp_secret(SECRET)["ok"] is True


def test_grouped_and_lowercase_seeds_are_normalised():
    assert verify_totp_secret("jbsw y3dp ehpk 3pxp")["ok"] is True


@pytest.mark.parametrize("bad", [None, "", "short", "not-base32!!1234xyz", "11"])
def test_an_unusable_seed_is_rejected_with_a_reason_and_no_secret(bad):
    out = verify_totp_secret(bad)
    assert out["ok"] is False
    assert out["reason"]
    if bad:
        assert str(bad) not in out["reason"]


def test_the_code_is_six_digits_and_matches_pyotp():
    code = totp_now(SECRET)
    assert code.isdigit() and len(code) == 6
    assert code == pyotp.TOTP(SECRET).now()


def test_the_code_is_stable_within_a_window_and_rotates_across_one():
    import time

    now = time.time()
    window_start = int(now // 30) * 30
    assert totp_now(SECRET, at=window_start + 1) == totp_now(SECRET, at=window_start + 28)
    assert totp_now(SECRET, at=window_start + 1) != totp_now(SECRET, at=window_start + 61)


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
def test_missing_credentials_report_names_only(monkeypatch):
    """Isolate from the operator's real .env, which now HAS values filled in.

    AUDIT (2026-10-26): this test originally only deleted the env vars - but
    ``get_secret`` falls back to ``.env``, and once the operator filled the
    credentials in, the test started reading real values and never saw
    "missing". The store is replaced, not just the environment.
    """
    from blue_engine.economics.providers import secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "_file_env", lambda: {})
    for env in broker_auth.CREDENTIAL_NAMES.values():
        monkeypatch.delenv(env, raising=False)
    empty = broker_auth.load_credentials()
    assert set(empty.missing_required()) <= set(broker_auth.CREDENTIAL_NAMES.values())
    for name, value in empty.describe().items():
        assert value == "<unset>", name


def test_describe_redacts_every_value():
    text = json.dumps(creds().describe())
    assert SECRET not in text
    assert "s3cret" not in text
    assert "AB1234" not in text


# --------------------------------------------------------------------------- #
# Preflight - no network beyond reachability
# --------------------------------------------------------------------------- #
def test_preflight_fails_closed_on_missing_credentials(fake):
    result = BrokerAuth(fake.url, credentials=creds(openalgo_password=None)).preflight()
    assert result.ok is False
    assert "OPENALGO_PASSWORD" in result.reason


def test_preflight_rejects_a_bad_seed_before_any_login(fake):
    result = BrokerAuth(fake.url, credentials=creds(
        broker_totp_secret="bad!")).preflight()
    assert result.ok is False
    assert "TOTP secret" in result.reason


def test_preflight_reports_an_unreachable_openalgo():
    result = BrokerAuth("http://127.0.0.1:9", credentials=creds()).preflight()
    assert result.ok is False
    assert "not reachable" in result.reason


# --------------------------------------------------------------------------- #
# The two legs
# --------------------------------------------------------------------------- #
def test_the_full_flow_succeeds_and_hits_the_endpoints_in_order(fake):
    result = BrokerAuth(fake.url, credentials=creds()).run()
    assert result.ok is True, result.to_dict()
    assert result.session_ready is True
    assert result.broker_name == "angel"
    posts = fake.paths()
    assert posts[0] == "/auth/login"
    assert posts[-1] == "/auth/broker/angel/callback"
    # The session probe is the app root (session-cookie protected), NOT the
    # API-key endpoint - see LOGIN_PATHS in broker_auth.py.
    assert "/" in [r["path"] for r in fake.requests]
    assert "/api/v1/ping" not in [r["path"] for r in fake.requests]


def test_the_broker_callback_receives_a_valid_totp(fake):
    BrokerAuth(fake.url, credentials=creds()).run()
    callback = next(r for r in fake.requests
                    if r["path"] == "/auth/broker/angel/callback")
    assert pyotp.TOTP(SECRET).verify(callback["body"]["totp"])
    assert callback["body"]["userid"] == "AB1234"
    assert callback["body"]["pin"] == "1234"


def test_a_failed_platform_login_never_reaches_the_broker(fake):
    """A wrong password must not burn broker login attempts."""
    fake.platform_ok = False
    result = BrokerAuth(fake.url, credentials=creds()).run()
    assert result.ok is False
    assert result.leg == "openalgo"
    assert not any("callback" in p for p in fake.paths())


def test_a_failed_broker_login_is_reported_at_the_broker_leg(fake):
    fake.broker_ok = False
    result = BrokerAuth(fake.url, credentials=creds()).run()
    assert result.ok is False
    assert result.leg == "broker"
    assert result.session_ready is False


def test_a_missing_broker_slug_fails_before_the_callback(fake):
    result = BrokerAuth(fake.url, credentials=creds(broker_name=None)).run()
    assert result.ok is False
    assert result.leg == "broker"
    assert not any("callback" in p for p in fake.paths())


def test_the_platform_totp_leg_runs_when_the_account_has_2fa(fake):
    fake.require_platform_totp = True
    # The platform's 2FA is a DIFFERENT seed from the broker's.
    result = BrokerAuth(fake.url, credentials=creds(
        openalgo_totp_secret=SECRET)).run()
    assert result.ok is True, result.to_dict()
    assert "/auth/login/totp" in fake.paths()
    # ... and both legs used a code, without either being recorded
    assert SECRET not in json.dumps(result.to_dict())


def test_platform_2fa_without_a_secret_fails_honestly(fake):
    fake.require_platform_totp = True
    # No OPENALGO_TOTP_SECRET supplied: must fail with the env-var NAME, not a
    # wrong code and not a silent success.
    result = BrokerAuth(fake.url, credentials=creds(
        openalgo_totp_secret=None)).run()
    assert result.ok is False
    assert "OPENALGO_TOTP_SECRET" in result.reason


def test_a_login_200_with_a_dead_session_is_not_trusted(fake):
    """Both legs answer 200, but the probe withholds the session cookie.

    AUDIT (2026-10-26): with the cookie check this is the exact scenario that
    the old /api/v1/ping probe MISREPORTED - a 200 from the probe made a dead
    session look ready. Now the missing cookie is what is detected.
    """
    fake.session_ok = False
    result = BrokerAuth(fake.url, credentials=creds()).run()
    assert result.ok is False
    assert result.leg == "verify"
    assert result.session_ready is False
    step = next(s for s in result.to_dict()["steps"]
                if s["step"] == "session_probe")
    assert "session cookie absent" in step["detail"]


def test_verify_can_be_skipped_but_is_on_by_default(fake):
    """verify=False reports the LOGIN's own success, not the session's.

    With verify skipped the result reflects only the two legs, which is why the
    flag exists (a scheduler may probe separately). The default is verify=True,
    and that default is what catches a dead session.
    """
    fake.session_ok = False
    unverified = BrokerAuth(fake.url, credentials=creds()).run(verify=False)
    assert unverified.ok is True          # legs only
    verified = BrokerAuth(fake.url, credentials=creds()).run()   # default
    assert verified.ok is False
    assert verified.leg == "verify"


# --------------------------------------------------------------------------- #
# Leakage
# --------------------------------------------------------------------------- #
def test_no_result_field_contains_a_credential_or_a_code(fake):
    """A 6-digit code is the thing that must never be recorded.

    Checking "no digits at all" would be wrong: "base32" and "HTTP 200" both
    contain digits and are legitimate. What matters is that no *value* and no
    TOTP appear anywhere in the record.
    """
    result = BrokerAuth(fake.url, credentials=creds()).run()
    blob = json.dumps(result.to_dict())
    for secret_value in (SECRET, "s3cret", "AB1234"):
        assert secret_value not in blob, f"{secret_value!r} leaked"

    codes = {totp_now(SECRET, at=t) for t in range(0, 120, 30)}
    steps = json.dumps(result.to_dict()["steps"])
    for code in codes:
        assert code not in steps, f"a live TOTP ({code}) was recorded"
    assert not any(step["detail"].isdigit() for step in result.to_dict()["steps"])


# --------------------------------------------------------------------------- #
# Opt-in gate
# --------------------------------------------------------------------------- #
def test_autologin_is_off_by_default(monkeypatch):
    from blue_engine.economics.providers import secrets as secrets_mod

    monkeypatch.delenv("BROKER_AUTOLOGIN", raising=False)
    monkeypatch.setattr(secrets_mod, "_file_env", lambda: {})
    assert broker_auth.autologin_enabled() is False


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True),
                                            ("yes", True), ("0", False),
                                            ("", False), ("no", False)])
def test_autologin_parses_the_flag(monkeypatch, value, expected):
    from blue_engine.economics.providers import secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "_file_env", lambda: {"BROKER_AUTOLOGIN": value})
    assert broker_auth.autologin_enabled() is expected


def test_a_scheduled_run_refuses_when_not_opted_in(monkeypatch):
    from blue_engine.economics.providers import secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "_file_env", lambda: {})
    out = broker_auth.run_scheduled_login(enable=False)
    assert out["ok"] is False
    assert out["skipped"] is True
    assert "not enabled" in out["reason"]


# --------------------------------------------------------------------------- #
# The client itself
# --------------------------------------------------------------------------- #
def test_the_session_client_keeps_cookies_across_requests(fake):
    session = OpenAlgoSession(fake.url)
    session.post_form("/auth/login", {"username": "a", "password": "b"})
    assert len(session.jar) >= 1
    assert session.reachable() is True


# --------------------------------------------------------------------------- #
# REGRESSION (2026-10-26): the probe must not be an API-key endpoint.
# --------------------------------------------------------------------------- #
def test_the_session_probe_is_not_the_apikey_endpoint():
    """``/api/v1/ping`` is API-KEY authenticated and 403s on a valid session.

    Verifying a broker session with it reported every good login as a dead
    session, and sent the operator chasing an OpenAlgo API key that has nothing
    to do with broker login. The probe must require the SESSION cookie instead.
    """
    from blue_engine.core.broker_auth import LOGIN_PATHS

    assert LOGIN_PATHS["session_probe"] == "/"
    assert "api/v1" not in LOGIN_PATHS["session_probe"]


def test_a_200_without_a_session_cookie_is_not_ready(fake):
    """OpenAlgo serves its shell to anonymous callers too; that is not a login."""
    fake.session_ok = False
    result = BrokerAuth(fake.url, credentials=creds()).run()
    assert result.session_ready is False
    assert result.ok is False


def test_the_probe_requires_the_session_cookie(fake):
    result = BrokerAuth(fake.url, credentials=creds()).run()
    assert result.session_ready is True
    probe_step = next(s for s in result.to_dict()["steps"]
                      if s["step"] == "session_probe")
    assert "session cookie present" in probe_step["detail"]
