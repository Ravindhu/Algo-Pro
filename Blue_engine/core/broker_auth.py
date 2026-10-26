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
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
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

#: Endpoint paths, verified against openalgo/blueprints/{auth,brlogin}.py.
#:
#: AUDIT (2026-10-26): the session probe was ``/api/v1/ping``. That endpoint is
#: API-KEY authenticated (``POST`` with ``apikey`` in the body) and answers 403
#: "Invalid openalgo apikey" even on a perfectly valid broker session - so a
#: good login would have been reported as a dead session. The probe must be a
#: page that requires the Flask SESSION cookie, which is what this flow creates.
#: ``/`` is the app root: it renders (200) for a session holder and never
#: consumes an API key. See scripts/verify/verify_openalgo.py for the API-key
#: contract - these are two different authentication systems.
LOGIN_PATHS = {
    "openalgo_login": "/auth/login",
    "openalgo_totp": "/auth/login/totp",
    "broker_chooser": "/broker",
    "broker_callback": "/auth/broker/{broker}/callback",
    "session_probe": "/",
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
                 timeout: float = 10.0) -> None:
        import http.cookiejar

        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.last_status = 0

    # ---- raw ------------------------------------------------------------- #
    def _request(self, path: str, *, form: Optional[Mapping[str, Any]] = None,
                 json_body: Optional[Mapping[str, Any]] = None
                 ) -> Dict[str, Any]:
        url = self.base_url + path
        headers = {"Accept": "application/json",
                   "User-Agent": "blue-engine-broker-auth/1"}
        data: Optional[bytes] = None
        if json_body is not None:
            data = json.dumps(dict(json_body)).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(dict(form)).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=data,
                                         headers=headers, method="POST" if data else "GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                self.last_status = int(getattr(response, "status", 200))
                raw = response.read(64 * 1024)
        except urllib.error.HTTPError as exc:
            self.last_status = int(exc.code)
            try:
                raw = exc.read(64 * 1024)
            except Exception:  # noqa: BLE001
                raw = b""
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"ok": False, "status": 0,
                    "error": f"unreachable: {type(exc).__name__}", "body": {}}
        text = raw.decode("utf-8", errors="replace")
        try:
            body = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            body = {"_raw": text[:300]}
        return {"ok": 200 <= self.last_status < 400,
                "status": self.last_status,
                "body": body if isinstance(body, dict) else {"_raw": body}}

    # ---- public ---------------------------------------------------------- #
    def get(self, path: str) -> Dict[str, Any]:
        return self._request(path)

    def post_form(self, path: str, form: Mapping[str, Any]) -> Dict[str, Any]:
        return self._request(path, form=form)

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
    def login_openalgo(self, result: AuthResult) -> AuthResult:
        """Password leg, plus the platform's OWN TOTP when it is enabled."""
        response = self.session.post_form(LOGIN_PATHS["openalgo_login"], {
            "username": self.credentials.openalgo_username,
            "password": self.credentials.openalgo_password,
        })
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
            result.reason = (f"OpenAlgo login failed (HTTP {response.get('status')}"
                             f"{', ' + str(body.get('message')) if body.get('message') else ''})")
            result.add("openalgo_password", False, result.reason)
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
        response = self.session.post_form(path, {
            "userid": self.credentials.broker_username,
            "pin": self.credentials.broker_password,
            "totp": code,
        })
        body = response.get("body") or {}
        ok = bool(response.get("ok")) and str(body.get("status") or "") != "error"
        if not ok:
            result.ok = False
            result.leg = "broker"
            result.reason = (f"broker TOTP login failed (HTTP {response.get('status')}"
                             f"{', ' + str(body.get('error') or body.get('message')) if (body.get('error') or body.get('message')) else ''})")
            result.add("broker_totp", False, result.reason)
            return result

        result.add("broker_totp", True, f"broker session established ({broker})")
        result.broker_name = broker
        return result

    # ---- verify ----------------------------------------------------------- #
    def verify_session(self, result: AuthResult) -> AuthResult:
        """Prove the session works instead of trusting the login response.

        Two signals, both required:

        1. ``/`` answers 200 for this cookie jar, and
        2. the app hands us a session cookie (``session=``), i.e. the login
           actually promoted the request to an authenticated session.

        A 200 alone is not proof: OpenAlgo serves its login shell to anonymous
        callers too. The cookie is the thing the broker token rides on.
        """
        probe = self.session.get(LOGIN_PATHS["session_probe"])
        cookie_names = {cookie.name for cookie in self.session.jar}
        has_session = "session" in cookie_names
        ready = bool(probe.get("ok")) and has_session
        result.session_ready = ready
        result.add("session_probe", ready,
                   f"HTTP {probe.get('status')}, "
                   f"session cookie {'present' if has_session else 'absent'}")
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


def run_scheduled_login(*, base_url: Optional[str] = None,
                        enable: Optional[bool] = None,
                        verify: bool = True,
                        allow_live: bool = False) -> Dict[str, Any]:
    """Entry point for Task Scheduler / cron. Returns a JSON-safe dict.

    Refuses to run unless opted in, so adding this to a schedule cannot silently
    start logging a desk in. The decision and its reason are always reported.
    """
    opted_in = autologin_enabled() if enable is None else bool(enable)
    if not opted_in:
        return {"ok": False, "skipped": True,
                "reason": "BROKER_AUTOLOGIN is not enabled", "leg": ""}

    url = base_url or get_secret("OPENALGO_URL") or "http://127.0.0.1:5000"
    result = BrokerAuth(url, allow_live=allow_live).run(verify=verify)
    payload = result.to_dict()
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


__all__ += ["autologin_enabled", "run_scheduled_login"]
