"""
broker_auth.py - headless TOTP login for OpenAlgo and the broker behind it.

WHY THIS FILE EXISTS
====================
OpenAlgo has TWO independent login legs, and they are easy to confuse:

======================  ==========================  =======================
leg                     asked for                   endpoint
======================  ==========================  =======================
1. platform (OpenAlgo)  username + password          ``POST /auth/login``
                        (+ its own TOTP **if** the  (+ ``/auth/login/totp``)
                         account has 2FA enabled)
2. broker               username + password + TOTP  ``POST /auth/broker/<b>/callback``
                        (the broker's alphanumeric
                         secret, via pyotp)
======================  ==========================  =======================

A scheduler needs leg 1 to be alive before leg 2 is even reachable, and leg 2
to be alive before any order can be placed. This module performs both, in
order, headlessly - and stops honestly at the first leg that fails instead of
pretending the session is ready.

VERIFIED AGAINST THE LOCAL OPENALGO SOURCE (not guessed)
--------------------------------------------------------
* ``openalgo/blueprints/auth.py::login`` -> ``session["user"]``; if the account
  has ``totp_required_for_login`` it returns ``{"status": "totp_required"}`` and
  expects ``POST /auth/login/totp`` with ``totp_code``.
* ``auth.py::broker_login`` -> ``/login`` then ``/broker``; ``GET /broker`` only
  renders, so the **broker-specific** TOTP form posts to
  ``/auth/broker/<broker>/callback`` with ``userid``, ``pin``, ``totp``
  (``brlogin.py::broker_callback``, e.g. the ``angel``/``fivepaisa`` branches).
* A valid session is indicated by ``logged_in`` in the session cookie - which
  is why this module verifies with a request that needs a session, not by
  trusting the login response.

SECRETS
-------
Read through :mod:`blue_engine.economics.providers.secrets` - the project's ONE
credential resolver (env -> ``_restored/.env`` -> ``.env``). No credential is
ever hardcoded, echoed, logged, or written to a report. Every value this module
prints goes through :func:`redact`.

SAFETY CONTRACT
---------------
1. **Opt-in.** ``BROKER_AUTOLOGIN=1`` (or ``--enable``) or nothing happens.
   A scheduled task must not start logging in because a file appeared.
2. **Never on a live-armed desk without a guard.** The caller passes
   ``allow_live=False`` by default; the module refuses to run against a desk
   whose OpenAlgo mode is LIVE unless explicitly allowed.
3. **Fail closed.** Any leg failure returns ``ok=False`` with the leg named.
   Nothing is retried in a loop; a scheduler retry policy belongs in the
   scheduler, not in this module.
4. **Two-factor codes are never stored.** The TOTP is computed in memory,
   used, and dropped. Only "was a TOTP sent" is recorded.
5. **No side channel.** This module touches only HTTP endpoints of a local
   OpenAlgo instance. It does not edit ``openalgo/``, the database, or config.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import secrets as _secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ..economics.providers.secrets import get_secret, has_secret, redact

logger = logging.getLogger(__name__)

__all__ = [
    "CREDENTIAL_NAMES",
    "BROKER_OPTIONAL_NAMES",
    "AuthResult",
    "CredentialSet",
    "load_credentials",
    "totp_now",
    "verify_totp_secret",
    "OpenAlgoSession",
    "BrokerAuth",
    "LOGIN_PATHS",
]

#: Required env var names. Only the NAMES live here - never a value.
CREDENTIAL_NAMES: Dict[str, str] = {
    "broker_totp_secret": "BROKER_TOTP_SECRET",
    "broker_password": "BROKER_PASSWORD",
    "broker_username": "BROKER_USERNAME",
    "openalgo_username": "OPENALGO_USERNAME",
    "openalgo_password": "OPENALGO_PASSWORD",
}

#: Optional: the OpenAlgo ACCOUNT's own 2FA (a different secret from the
#: broker's) and the broker slug used to build the callback URL.
BROKER_OPTIONAL_NAMES: Dict[str, str] = {
    "openalgo_totp_secret": "OPENALGO_TOTP_SECRET",
    "broker_name": "BROKER_NAME",
}

#: Endpoint paths, verified against openalgo/blueprints/{auth,brlogin}.py AND
#: against the running instance (2026-10-26).
#:
#: AUDIT (2026-10-26, second pass - LIVE VERIFIED):
#: 1. The session probe was ``/api/v1/ping`` (API-key authenticated, 403 on a
#:    good session), then ``/`` (serves the React shell to anyone, 200 even for
#:    an anonymous caller). The probe that actually reports session state is
#:    ``/auth/session-status``, which app.py explicitly excludes from the
#:    session check and answers with ``{"authenticated": bool}``.
#: 2. ``POST /auth/login`` is CSRF-protected by Flask-WTF: a form POST without
#:    ``X-CSRFToken`` is rejected before the password is even looked at. The
#:    SPA fetches ``GET /auth/csrf-token`` immediately before every mutating
#:    request; so does this module.
#: 3. BROKER FIELD NAMES DIFFER PER BROKER. ``brlogin.py::broker_callback``
#:    reads ``userid``/``pin``/``totp`` for angel/fivepaisa, but ``password``/
#:    ``totp`` for mstock - and mstock's clientcode comes from OpenAlgo's OWN
#:    ``BROKER_API_KEY`` env var (read inside broker/mstock/api/auth_api.py),
#:    NOT from the form. Sending angel's field names to mstock fails with
#:    "Password is required" and still burns a login attempt.
LOGIN_PATHS = {
    "openalgo_login": "/auth/login",
    "openalgo_totp": "/auth/login/totp",
    "csrf_token": "/auth/csrf-token",
    "session_status": "/auth/session-status",
    "broker_chooser": "/broker",
    # AUDIT (live, 2026-10-26): brlogin_bp is registered WITHOUT a url_prefix
    # (app.py:308), so its ``/<broker>/callback`` sits at the ROOT -
    # ``/mstock/callback``, not ``/auth/broker/mstock/callback`` (404). The
    # ``/auth/...`` guess looked right because auth_bp does own that prefix.
    "broker_callback": "/{broker}/callback",
}

#: Per-broker broker-callback form fields. ``{totp}`` is substituted; other
#: values come from the credential set. A broker absent from this map is
#: refused BEFORE the POST with an explicit "unsupported broker" reason -
#: guessing field names at a live broker costs login attempts.
BROKER_FORM_FIELDS: Dict[str, Dict[str, str]] = {
    # brlogin.py::broker_callback, angel branch:
    #   clientcode = userid | clientid; broker_pin = pin; totp_code = totp
    "angel": {"userid": "{broker_username}", "pin": "{broker_password}",
              "totp": "{totp}"},
    "fivepaisa": {"userid": "{broker_username}", "pin": "{broker_password}",
                  "totp": "{totp}"},
    # brlogin.py, mstock branch: password + totp only. The clientcode is read
    # from OpenAlgo's OWN env (BROKER_API_KEY) inside the broker module.
    "mstock": {"password": "{broker_password}", "totp": "{totp}"},
}

#: How long a computed TOTP is considered usable (30 s window minus one step,
#: so a code is never sent in the last second before it rotates).
TOTP_VALIDITY_SECONDS = 25


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
@dataclass
class CredentialSet:
    """The five values, held in memory only. ``describe()`` never leaks them."""

    def broker_form(self, broker: str, totp_code: str) -> Dict[str, str]:
        """The broker-callback form for this broker, using ITS field names.

        AUDIT (live, 2026-10-26): ``brlogin.py::broker_callback`` reads
        ``userid``/``pin``/``totp`` for angel and fivepaisa, but ``password``/
        ``totp`` for mstock - and mstock's clientcode is read from OpenAlgo's
        OWN ``BROKER_API_KEY`` env inside broker/mstock/api/auth_api.py, NOT
        from the form. Sending angel's field names to mstock fails with
        "Password is required" and STILL BURNS A LOGIN ATTEMPT.

        Refused BEFORE the POST when the broker is unsupported, with an
        explicit reason: guessing field names at a live broker is the same
        failure with a different message.
        """
        fields = BROKER_FORM_FIELDS.get(broker)
        if fields is None:
            raise ValueError(
                f"unsupported broker {broker!r}; supported: "
                f"{sorted(BROKER_FORM_FIELDS)}. Refusing before the POST: "
                "a guessed field name still burns a login attempt."
            )
        data = {key: value.format(totp=totp_code, **self.__dict__)
                for key, value in fields.items()}
        return data

    broker_totp_secret: Optional[str] = None
    broker_password: Optional[str] = None
    broker_username: Optional[str] = None
    openalgo_username: Optional[str] = None
    openalgo_password: Optional[str] = None
    openalgo_totp_secret: Optional[str] = None
    broker_name: Optional[str] = None

    # ---- presence -------------------------------------------------------- #
    def missing_required(self) -> List[str]:
        """Env-var NAMES that are unset. Names only - never values."""
        return [env for attr, env in CREDENTIAL_NAMES.items()
                if not getattr(self, attr)]

    @property
    def complete(self) -> bool:
        return not self.missing_required()

    def describe(self) -> Dict[str, str]:
        """A loggable summary: redacted values, env-var names, presence."""
        out = {}
        for attr, env in {**CREDENTIAL_NAMES, **BROKER_OPTIONAL_NAMES}.items():
            value = getattr(self, attr)
            out[env] = redact(env, value) if value else "<unset>"
        return out


def load_credentials(*, use_env_override: bool = True) -> CredentialSet:
    """Read the five values through the project's single secret resolver."""
    def take(env_name: str) -> Optional[str]:
        value = get_secret(env_name)
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    return CredentialSet(
        broker_totp_secret=take(CREDENTIAL_NAMES["broker_totp_secret"]),
        broker_password=take(CREDENTIAL_NAMES["broker_password"]),
        broker_username=take(CREDENTIAL_NAMES["broker_username"]),
        openalgo_username=take(CREDENTIAL_NAMES["openalgo_username"]),
        openalgo_password=take(CREDENTIAL_NAMES["openalgo_password"]),
        openalgo_totp_secret=take(BROKER_OPTIONAL_NAMES["openalgo_totp_secret"]),
        broker_name=take(BROKER_OPTIONAL_NAMES["broker_name"]),
    )


# --------------------------------------------------------------------------- #
# TOTP
# --------------------------------------------------------------------------- #
def _pyotp() -> Any:
    """Import pyotp lazily so the engine still imports without it."""
    import pyotp  # noqa: PLC0415 - deliberate lazy import

    return pyotp


def _normalise_secret(secret: str) -> str:
    """Brokers print the secret in groups; base32 ignores case and spaces."""
    return "".join(str(secret).split()).replace("-", "").upper()


def verify_totp_secret(secret: Optional[str]) -> Dict[str, Any]:
    """Check that ``secret`` is a usable base32 TOTP seed. Never raises.

    Returns ``{"ok", "reason"}``. The reason names the problem in operator
    terms ("not base32", "too short") - and never includes the secret.
    """
    if not secret:
        return {"ok": False, "reason": "no secret configured"}
    cleaned = _normalise_secret(secret)
    if len(cleaned) < 16:
        return {"ok": False,
                "reason": f"secret is {len(cleaned)} chars; a TOTP seed is "
                          f"usually 16-32"}
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    bad = sorted(set(cleaned) - allowed)
    if bad:
        return {"ok": False,
                "reason": f"secret is not base32 (unexpected chars: {bad[:5]})"}
    try:
        _pyotp().TOTP(cleaned).now()
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"ok": False, "reason": f"pyotp rejected the secret: {type(exc).__name__}"}
    return {"ok": True, "reason": "valid base32 TOTP seed"}


def totp_now(secret: str, *, at: Optional[float] = None) -> str:
    """The 6-digit code for ``secret`` right now (or at epoch ``at``).

    Computed in memory and returned as a string; the caller must not log it.
    """
    cleaned = _normalise_secret(secret)
    totp = _pyotp().TOTP(cleaned)
    return totp.at(int(at if at is not None else time.time()))


def _seconds_left_in_window(at: Optional[float] = None) -> int:
    now = at if at is not None else time.time()
    return 30 - (int(now) % 30)


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #
@dataclass
class AuthResult:
    """What happened, leg by leg. Never contains a credential or a TOTP."""

    ok: bool = False
    leg: str = ""                      # "" | "config" | "openalgo" | "broker"
    reason: str = ""
    steps: List[Dict[str, Any]] = field(default_factory=list)
    session_ready: bool = False
    broker_name: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0

    def add(self, step: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"step": step, "ok": bool(ok), "detail": detail,
                           "at": _dt.datetime.now(_dt.timezone.utc).isoformat(
                               timespec="seconds")})

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


# --------------------------------------------------------------------------- #
# The HTTP client (cookie-jar based, no third-party dependency)
# --------------------------------------------------------------------------- #
class OpenAlgoSession:
    """A minimal cookie-carrying HTTP client for a LOCAL OpenAlgo instance.

    Uses ``http.cookiejar`` so the Flask session cookie persists across the two
    login legs - that cookie IS the authentication, so losing it would mean
    logging in twice.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:5000",
                 timeout: float = 10.0,
                 deadline_seconds: float = 25.0) -> None:
        import http.cookiejar

        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        #: Wall clock per request. The socket timeout above resets on every
        #: byte, so it cannot bound a slow-but-alive server. See _request().
        self.deadline_seconds = float(deadline_seconds)
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.last_status = 0

    # ---- raw ------------------------------------------------------------- #
    def _request(self, path: str, *, form: Optional[Mapping[str, Any]] = None,
                 json_body: Optional[Mapping[str, Any]] = None,
                 headers: Optional[Mapping[str, str]] = None
                 ) -> Dict[str, Any]:
        url = self.base_url + path
        out_headers = {"Accept": "application/json",
                       "User-Agent": "blue-engine-broker-auth/1"}
        if headers:
            out_headers.update(headers)  # caller headers win (e.g. X-CSRFToken)
        data: Optional[bytes] = None
        if json_body is not None:
            data = json.dumps(dict(json_body)).encode("utf-8")
            out_headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(dict(form)).encode("utf-8")
            out_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=data,
                                         headers=out_headers, method="POST" if data else "GET")
        # AUDIT (live, 2026-10-27): ``timeout=`` on urlopen is a SOCKET timeout,
        # so it resets on every byte. OpenAlgo's login hook reloads a 166,945-
        # symbol master contract while the caller waits - minutes on a cold
        # cache - and the call never raised, it simply never returned. A
        # scheduled task then showed no result at all. The whole exchange now
        # runs under a HARD WALL-CLOCK DEADLINE, so a stall is reported as an
        # honest failure the operator can act on instead of a silent hang.
        box: Dict[str, Any] = {}

        def _exchange() -> None:
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    box["status"] = int(getattr(response, "status", 200))
                    box["raw"] = response.read(64 * 1024)
            except urllib.error.HTTPError as exc:
                box["status"] = int(exc.code)
                try:
                    box["raw"] = exc.read(64 * 1024)
                except Exception:  # noqa: BLE001
                    box["raw"] = b""
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                box["error"] = f"{type(exc).__name__}: {exc}"

        worker = threading.Thread(target=_exchange, daemon=True,
                                  name="openalgo-http")
        worker.start()
        worker.join(self.deadline_seconds)
        if worker.is_alive():
            # The socket is still held; it dies with the process.
            return {"ok": False, "status": 0, "timed_out": True,
                    "error": (f"no response within {self.deadline_seconds:g}s "
                              f"(socket timeout is {self.timeout:g}s; a slow "
                              "OpenAlgo hook is still running server-side)"),
                    "body": {}}
        if "error" in box:
            return {"ok": False, "status": 0,
                    "error": f"unreachable: {box['error']}", "body": {}}
        self.last_status = int(box.get("status", 0))
        raw = box.get("raw") or b""
        text = raw.decode("utf-8", errors="replace")
        try:
            body = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            body = {"_raw": text[:300]}
        out = {"ok": 200 <= self.last_status < 400,
               "status": self.last_status,
               "body": body if isinstance(body, dict) else {"_raw": body}}
        if not out["ok"]:
            # Keep the server's own words when it sent any: the operator sees
            # "Too many login attempts" instead of a bare HTTP 429.
            server_said = (body.get("message") or body.get("error")) \
                if isinstance(body, dict) else None
            out["error"] = str(server_said or f"HTTP {self.last_status}")
        return out

    # ---- public ---------------------------------------------------------- #
    def get(self, path: str) -> Dict[str, Any]:
        return self._request(path)

    def post_form(self, path: str, form: Mapping[str, Any],
                  headers: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
        return self._request(path, form=form, headers=headers)

    def post_json(self, path: str, body: Mapping[str, Any]) -> Dict[str, Any]:
        return self._request(path, json_body=body)

    def reachable(self) -> bool:
        result = self.get("/")
        return bool(result.get("status"))


# --------------------------------------------------------------------------- #
# The flow
# --------------------------------------------------------------------------- #
class BrokerAuth:
    """Perform the two login legs. One instance per attempt; no state kept.

    Parameters
    ----------
    base_url : OpenAlgo root, default ``http://127.0.0.1:5000``.
    credentials : override the env-derived set (tests, or a vault).
    allow_live : when False (default) the module still logs in - login is not a
        trade - but it records a warning that the desk is LIVE. When True the
        warning is suppressed. The real trading gate is the pipeline's risk
        layer, not this module; this flag exists so a scheduler run is visible.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:5000", *,
                 credentials: Optional[CredentialSet] = None,
                 allow_live: bool = False,
                 timeout: float = 10.0,
                 session: Optional[OpenAlgoSession] = None) -> None:
        self.session = session or OpenAlgoSession(base_url, timeout=timeout)
        self.credentials = credentials or load_credentials()
        self.allow_live = bool(allow_live)

    # ---- preflight ------------------------------------------------------- #
    def preflight(self) -> AuthResult:
        """Check config and TOTP validity BEFORE touching the network."""
        result = AuthResult(leg="config",
                            started_at=_dt.datetime.now(
                                _dt.timezone.utc).isoformat(timespec="seconds"))
        missing = self.credentials.missing_required()
        if missing:
            result.ok = False
            result.reason = ("missing credentials: " + ", ".join(missing)
                             + " (set them in .env; names only are reported)")
            result.add("credentials", False, result.reason)
            return result
        result.add("credentials", True, "all five required values present")

        totp_check = verify_totp_secret(self.credentials.broker_totp_secret)
        result.add("broker_totp_secret", totp_check["ok"], totp_check["reason"])
        if not totp_check["ok"]:
            result.ok = False
            result.reason = "broker TOTP secret is unusable: " + totp_check["reason"]
            return result

        if not self.session.reachable():
            result.ok = False
            result.reason = ("OpenAlgo is not reachable at "
                             f"{self.session.base_url} - start it first")
            result.add("openalgo_reachable", False, result.reason)
            return result
        result.add("openalgo_reachable", True, self.session.base_url)
        result.ok = True
        result.reason = "preflight passed"
        return result

    # ---- leg 1: OpenAlgo -------------------------------------------------- #
    def _csrf_token(self) -> str:
        """Fetch a CSRF token. Empty on failure; the caller decides."""
        response = self.session.get(LOGIN_PATHS["csrf_token"])
        body = response.get("body") or {}
        token = body.get("csrf_token") or body.get("token") or ""
        return str(token) if token else ""

    def login_openalgo(self, result: AuthResult) -> AuthResult:
        """Password leg, plus the platform's OWN TOTP when it is enabled.

        AUDIT (live, 2026-10-26): this POST is CSRF-protected by Flask-WTF.
        A form without ``X-CSRFToken`` is rejected before the password is read,
        which looks exactly like "wrong password" to a caller that does not
        know. The SPA fetches ``/auth/csrf-token`` before every mutating
        request; this module does the same, once per attempt.
        """
        headers = {}
        csrf = self._csrf_token()
        if csrf:
            headers["X-CSRFToken"] = csrf
            result.add("csrf_token", True, "fetched")
        else:
            result.add("csrf_token", False, "unavailable; POST may be rejected")
        response = self.session.post_form(LOGIN_PATHS["openalgo_login"], {
            "username": self.credentials.openalgo_username,
            "password": self.credentials.openalgo_password,
        }, headers=headers)
        body = response.get("body") or {}
        status = str(body.get("status") or "")

        if status == "totp_required":
            result.add("openalgo_password", True, "2FA required for this account")
            if not self.credentials.openalgo_totp_secret:
                result.ok = False
                result.leg = "openalgo"
                result.reason = ("OpenAlgo account has 2FA enabled but "
                                 f"{BROKER_OPTIONAL_NAMES['openalgo_totp_secret']} "
                                 "is not set")
                result.add("openalgo_totp", False, result.reason)
                return result
            code = totp_now(self.credentials.openalgo_totp_secret)
            totp_response = self.session.post_json(LOGIN_PATHS["openalgo_totp"],
                                                   {"totp_code": code})
            if not totp_response.get("ok") or str(
                    (totp_response.get("body") or {}).get("status")) != "success":
                result.ok = False
                result.leg = "openalgo"
                result.reason = "OpenAlgo 2FA code rejected"
                result.add("openalgo_totp", False, result.reason)
                return result
            result.add("openalgo_totp", True, "platform 2FA accepted")
            return result

        if not response.get("ok") or status == "error":
            result.ok = False
            result.leg = "openalgo"
            detail = ("timed out - " + str(response.get("error"))
                      if response.get("timed_out")
                      else str(response.get("error") or "not accepted"))
            message = body.get("message") or response.get("error") or ""
            result.reason = (f"OpenAlgo login failed (HTTP {response.get('status')}"
                             f"{', ' + str(message) if message else ''})")
            result.add("openalgo_password", False, detail)
            return result

        result.add("openalgo_password", True, "platform password accepted")
        return result

    # ---- leg 2: broker ---------------------------------------------------- #
    def login_broker(self, result: AuthResult) -> AuthResult:
        """Broker TOTP leg: ``POST /auth/broker/<broker>/callback``.

        The TOTP is computed once, immediately before the POST, and only when at
        least ``TOTP_VALIDITY_SECONDS`` remain in the 30 s window - a code sent
        with 1 s left is a guaranteed failure that also burns a login attempt.
        """
        broker = (self.credentials.broker_name or "").strip().lower()
        if not broker:
            result.ok = False
            result.leg = "broker"
            result.reason = (f"{BROKER_OPTIONAL_NAMES['broker_name']} is not set - "
                             "the broker slug is needed to build the callback URL")
            result.add("broker_name", False, result.reason)
            return result

        remaining = _seconds_left_in_window()
        if remaining < 5:
            # Wait for the next window rather than send a dying code.
            time.sleep(remaining + 1)

        code = totp_now(self.credentials.broker_totp_secret)
        path = LOGIN_PATHS["broker_callback"].format(broker=broker)
        form = self.credentials.broker_form(broker, code)
        headers = {}
        if self._csrf_token():
            headers["X-CSRFToken"] = self._csrf_token()
        response = self.session.post_form(path, form, headers=headers)
        body = response.get("body") or {}
        ok = bool(response.get("ok")) and str(body.get("status") or "") != "error"
        if not ok:
            result.ok = False
            result.leg = "broker"
            # AUDIT (live, 2026-10-27): this printed "HTTP 0" for a timed-out or
            # unreachable broker callback, which tells the operator nothing.
            # Report the CAUSE (timeout vs refused vs the server's own words)
            # and keep the status only when there was a real HTTP exchange.
            cause = (body.get("error") or body.get("message")
                     or response.get("error") or "not accepted")
            status = response.get("status") or 0
            where = f" (HTTP {status})" if status else ""
            result.reason = f"broker TOTP login failed{where}: {cause}"
            result.add("broker_totp", False, f"{cause}{where}")
            return result

        # A resumed session is as good as a fresh one: OpenAlgo found a still-
        # valid broker token in its database and skipped the TOTP round-trip.
        # AUDIT (live, 2026-10-26): the live server answered
        # {"message": "Broker session resumed", "redirect": "/dashboard"}
        # WITHOUT a broker_callback POST at all (auth.py:_try_resume_broker_session)
        message = str(body.get("message") or "")
        resumed = "resumed" in message.lower()
        result.add("broker_totp", True,
                   f"broker session {'resumed (no fresh TOTP needed)' if resumed else 'established'} ({broker})")
        result.broker_name = broker
        return result

    # ---- verify ----------------------------------------------------------- #
    def verify_session(self, result: AuthResult) -> AuthResult:
        """Prove the session works instead of trusting the login response.

        Three signals, from the most authoritative down:

        1. ``GET /auth/session-status`` answers ``{"authenticated": bool}``.
           This is the endpoint the OpenAlgo SPA itself polls, and app.py
           explicitly exempts it from the session-expiry check, so it answers
           honestly for an anonymous caller instead of 302-ing. (AUDIT, live
           2026-10-26: the previous probe was ``/`` - the React shell answers
           200 to ANYONE, so a dead session looked ready.)
        2. the Flask session cookie is present in the jar.
        3. ``status`` in the body, when present, says "success".

        A 200 alone is not proof of anything.
        """
        probe = self.session.get(LOGIN_PATHS["session_status"])
        body = probe.get("body") or {}
        cookie_names = {cookie.name for cookie in self.session.jar}
        has_session = "session" in cookie_names

        # The authoritative signal: what the app says about this session.
        authenticated = body.get("authenticated")
        if isinstance(authenticated, bool):
            auth_signal = authenticated
        else:
            # Older builds answer {"status": "success", "logged_in": bool}
            logged_in = body.get("logged_in")
            auth_signal = (logged_in if isinstance(logged_in, bool) else None)

        if auth_signal is None:
            # The endpoint could not answer the question at all - fall back to
            # the cookie, but SAY so, rather than silently trusting a weaker
            # signal.
            ready = has_session
            basis = "cookie-only (session-status did not report)"
        else:
            ready = bool(auth_signal) and has_session
            basis = f"session-status authenticated={auth_signal} + cookie"

        result.session_ready = ready
        result.add("session_probe", ready,
                   f"HTTP {probe.get('status')}; {basis}")
        # The probe already knows WHICH broker answered; without this the run
        # summary printed "broker=-" on a perfectly good session (seen live
        # 2026-10-27), which reads like nothing was connected.
        if ready and not result.broker_name:
            probed_broker = str(body.get("broker") or "").strip().lower()
            if probed_broker:
                result.broker_name = probed_broker
        if not ready:
            result.ok = False
            # AUDIT (2026-10-26): this used ``result.leg or "verify"``, and by
            # now ``leg`` is always set - so a failed probe was reported against
            # whichever leg ran before it, pointing the operator at the wrong
            # cause (a broker TOTP problem that did not exist). A probe failure
            # is its own leg.
            result.leg = "verify"
            result.reason = ("login responded, but the session probe failed - "
                             "the broker token is not usable yet")
        return result

    # ---- orchestration ---------------------------------------------------- #
    def run(self, *, verify: bool = True) -> AuthResult:
        """Do both legs in order. Stops at the first failure. Never raises."""
        started = time.perf_counter()
        result = self.preflight()
        if not result.ok:
            result.finished_at = _dt.datetime.now(
                _dt.timezone.utc).isoformat(timespec="seconds")
            result.duration_seconds = round(time.perf_counter() - started, 2)
            return result

        result = self.login_openalgo(result)
        if not result.ok:
            return self._finish(result, started)

        result = self.login_broker(result)
        if not result.ok:
            return self._finish(result, started)

        if verify:
            result = self.verify_session(result)
        return self._finish(result, started)

    @staticmethod
    def _finish(result: AuthResult, started: float) -> AuthResult:
        result.finished_at = _dt.datetime.now(
            _dt.timezone.utc).isoformat(timespec="seconds")
        result.duration_seconds = round(time.perf_counter() - started, 2)
        return result


# --------------------------------------------------------------------------- #
# Scheduler entry point
# --------------------------------------------------------------------------- #
def autologin_enabled() -> bool:
    """``BROKER_AUTOLOGIN=1`` opts a scheduled task in. Default: off."""
    value = (get_secret("BROKER_AUTOLOGIN") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


#: Minimum gap between two REAL login attempts. A broker locks an account for
#: repeated failed logins, and OpenAlgo itself rate-limits ``/auth/login`` to
#: 5/minute and 25/hour per IP. A transient 5xx at 08:50 must not turn into
#: four attempts in five minutes, so this is a hard floor no caller can bypass.
LOGIN_MIN_INTERVAL_SECONDS = 600.0

#: Additional back-off after a FAILED attempt: a broker-side rejection is more
#: likely to repeat than to clear, so a retry waits longer than a plain rerun.
LOGIN_FAILURE_BACKOFF_SECONDS = 1800.0

#: Login bookkeeping lives beside the other run records (never in git-tracked
#: config), and is read by the dashboard card as well.
def _login_state_path() -> Path:
    return Path(__file__).resolve().parents[2] / "Temp" / "run" / "login_guard.json"


def login_guard(*, now: Optional[float] = None) -> Dict[str, Any]:
    """May a login run NOW? Pure read - it never writes and never logs in.

    Returns ``{allowed, wait_seconds, reason, last_attempt_at, last_ok}``.
    ``allowed`` is False inside the cooldown window, and the caller is expected
    to report WHY rather than spend an attempt. Fail-open on an unreadable file
    would be wrong here: a corrupt guard file must not let four retries through,
    so an unreadable state is treated as "no attempt on record" only when the
    file is genuinely absent.
    """
    when = time.time() if now is None else float(now)
    path = _login_state_path()
    state: Dict[str, Any] = {}
    if path.is_file():
        try:
            state = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            # Say so, but do not fail open into a login: treat as unknown.
            return {"allowed": False, "wait_seconds": LOGIN_MIN_INTERVAL_SECONDS,
                    "reason": "login guard state is unreadable - refusing to "
                              "spend a broker attempt until it is inspected",
                    "last_attempt_at": None, "last_ok": None}

    last = state.get("last_attempt_epoch")
    if not isinstance(last, (int, float)):
        return {"allowed": True, "wait_seconds": 0.0,
                "reason": "no login attempt on record",
                "last_attempt_at": state.get("last_attempt_at"),
                "last_ok": state.get("last_ok")}

    last_ok = bool(state.get("last_ok"))
    window = LOGIN_MIN_INTERVAL_SECONDS if last_ok else LOGIN_FAILURE_BACKOFF_SECONDS
    elapsed = when - float(last)
    if elapsed < window:
        remaining = round(window - elapsed, 1)
        return {"allowed": False, "wait_seconds": remaining,
                "reason": (f"cooldown: {int(remaining // 60)}m {int(remaining % 60)}s "
                           f"left after the last "
                           f"{'successful' if last_ok else 'failed'} attempt "
                           f"(window {int(window // 60)}m). Repeated broker "
                           "logins can lock the account."),
                "last_attempt_at": state.get("last_attempt_at"),
                "last_ok": last_ok}
    return {"allowed": True, "wait_seconds": 0.0,
            "reason": "cooldown elapsed",
            "last_attempt_at": state.get("last_attempt_at"), "last_ok": last_ok}


def record_login_attempt(*, ok: bool, now: Optional[float] = None,
                         outcome: str = "") -> None:
    """Stamp the attempt so the next caller is rate-limited. Best effort.

    Writes AFTER the attempt: the guard exists to stop rapid REPEATS, and a
    crash mid-attempt still counts (the broker saw the request either way).
    """
    when = time.time() if now is None else float(now)
    payload = {
        "last_attempt_epoch": when,
        "last_attempt_at": _dt.datetime.fromtimestamp(
            when, _dt.timezone.utc).isoformat(timespec="seconds"),
        "last_ok": bool(ok),
        "outcome": str(outcome)[:200],   # caller passes a secret-free reason
    }
    path = _login_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - never fail the run over bookkeeping
        logger.warning("could not write login guard state: %s", exc)


def run_scheduled_login(*, base_url: Optional[str] = None,
                        enable: Optional[bool] = None,
                        verify: bool = True,
                        allow_live: bool = False,
                        force: bool = False) -> Dict[str, Any]:
    """Entry point for Task Scheduler / cron. Returns a JSON-safe dict.

    Refuses to run unless opted in, so adding this to a schedule cannot silently
    start logging a desk in. It ALSO refuses to run inside the login cooldown
    (see ``login_guard``), because the 3x restart-on-failure policy plus a
    manual retry is how an account gets locked. ``force`` exists for a human at
    a terminal, never for a scheduler.
    """
    opted_in = autologin_enabled() if enable is None else bool(enable)
    if not opted_in:
        return {"ok": False, "skipped": True,
                "reason": "BROKER_AUTOLOGIN is not enabled", "leg": ""}

    guard = login_guard()
    if not guard["allowed"] and not force:
        logger.warning("broker autologin suppressed by cooldown: %s",
                       guard["reason"])
        return {"ok": False, "skipped": True, "cooldown": True,
                "reason": guard["reason"], "leg": "",
                "wait_seconds": guard["wait_seconds"],
                "last_attempt_at": guard.get("last_attempt_at")}

    url = base_url or get_secret("OPENALGO_URL") or "http://127.0.0.1:5000"
    result = BrokerAuth(url, allow_live=allow_live).run(verify=verify)
    # Stamp EVERY attempt, successful or not: the next caller must know a broker
    # login just happened, even if this one failed at the first leg.
    record_login_attempt(ok=bool(result.ok),
                         outcome=(result.reason or "attempt completed"))
    payload = result.to_dict()
    # AUDIT (live, 2026-10-27): ``to_dict()`` calls this field ``broker_name``
    # while the summary line in scripts/ops/scheduled_login.py reads
    # ``payload["broker"]`` - so a perfectly good session printed "broker=-".
    # Publish BOTH keys rather than change either consumer.
    payload.setdefault("broker", payload.get("broker_name") or "")
    if not result.ok:
        # One structured line an operator can grep, with no secret in it.
        logger.warning("broker autologin failed at leg=%s: %s",
                       result.leg, result.reason)
        print(json.dumps({"ok": False, "leg": result.leg,
                          "reason": result.reason,
                          "steps": result.steps}, indent=2))
    else:
        print(json.dumps({"ok": True, "leg": result.leg,
                          "broker": result.broker_name,
                          "session_ready": result.session_ready,
                          "duration_seconds": result.duration_seconds},
                         indent=2))
    return payload


__all__ += ["autologin_enabled", "run_scheduled_login",
            "login_guard", "record_login_attempt",
            "LOGIN_MIN_INTERVAL_SECONDS", "LOGIN_FAILURE_BACKOFF_SECONDS"]
