"""Sign in to Screener.in, then save the new ``sessionid`` back into the repository .env.

Flow stage: Liquidity screener (credential upkeep for the screener engine; no candidate,
            no order, and no market-data output comes from here)
Status: PHASE 5 IMPLEMENTED (2026-11-26; read-only against screener.in, writes only .env)

WHY THIS EXISTS
---------------
``ScreenerClient.login`` already authenticates with username + password and receives a
fresh ``sessionid`` cookie - but that cookie lived ONLY in process memory, so every
fresh process had to log in again, and an expired cookie in .env was invisible until an
ungraceful failure. The owner asked for the button to fix that:

    click START ENGINE -> sign in with SCREENER_USERNAME / SCREENER_PASSWORD
                       -> on success, write the new sessionid back to .env atomically
                       -> never print the value, never commit it

WHAT IT DOES, IN ORDER
-----------------------
1. Read the base URL from ``URL_SCREENER_DOT_IN`` (the screen URL also carries the
   host) or fall back to ``https://www.screener.in``.
2. Read the credentials from the process environment, which the dashboard has already
   populated from .env and from the encrypted secrets store.
3. ``ScreenerClient.login(username, password)`` - the existing, tested login path.
   No new scraper is written here.
4. Only if that returned ``ok``: write ``Screenerdotin_sessionid`` to .env through
   ``quant.core.env_write.write_env_values`` (locked read-modify-write + atomic replace),
   and set ``os.environ`` so the running process sees it without a restart.

REFUSALS (each fails closed, and none of them raises into the dashboard)
-----------------------------------------------------------------------
* Username or password missing -> ``reason="credentials missing"``. Nothing is attempted,
  so a half-configured .env never burns a login attempt against screener.in.
* Login failed (bad password, CAPTCHA, MFA, site down) -> ``reason="login failed"``.
  The reason string is the client's own error, which never contains the password.
* Login ok but no session cookie came back -> ``reason="no session cookie"``.
* The .env write fails -> the login still happened, so the cookie is in memory only.
  Reported as ``saved=False``: the desk is not told the session is persisted.

SAFETY
------
* The cookie VALUE is never logged, never returned by :func:`refresh_session`, and never
  placed in an exception message. Only ``sessionid``'s presence is reported.
* The value is length-capped and line-break-refused by ``env_write``, so it cannot inject
  a second variable into .env.
* Read-only against screener.in: no order path, no OpenAlgo import, no broker.
* Nothing here is committed: ``.env`` is git-ignored.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load
from quant.core.env import DEFAULT_ENV_FILE
from quant.core import env_write

__all__ = ["SESSION_KEY", "refresh_session", "credentials_present", "mark_signed_in_today"]

_log = logging.getLogger("quant.data.screener_session")

#: The .env key the screener engine reads first (screener_engine._get_client).
SESSION_KEY = "Screenerdotin_sessionid"

#: Every credential name the engine/scripts accept, in the order they are tried.
#: ``SCREENER_USERNAME`` / ``SCREENER_PASSWORD`` are the documented pair (.env.example);
#: the rest are legacy aliases already honoured by the Blue engine. ``username`` /
#: ``email`` / ``password`` are generic names that some shells pre-set, so they are tried
#: LAST and only when non-blank.
_USERNAME_KEYS = ("SCREENER_USERNAME", "Screenerdotin_username", "username", "email")
_PASSWORD_KEYS = ("SCREENER_PASSWORD", "password")

#: The ONLY names this module reads from .env. Everything comes from that file - no
#: prompt, no argument, no hard-coded credential. Owner rule 2026-11-26.
#: URL_SCREENER_DOT_IN is verified: screener_engine.py:103 parses the screen id/slug from it.
ENV_ONLY_KEYS = tuple(_USERNAME_KEYS) + tuple(_PASSWORD_KEYS) + ("URL_SCREENER_DOT_IN",)

#: Base URL when URL_SCREENER_DOT_IN is unset or carries no host.
_FALLBACK_BASE = "https://www.screener.in"

#: Marker file: the date a login last succeeded. Not a secret, so it lives beside the
#: screen data rather than in .env. Relative to the .env file's folder (the repo root).
_DAY_MARKER = Path("data") / "screened" / ".screener_session_day"


def _marker_path(env_file: Path) -> Path:
    return Path(env_file).parent / _DAY_MARKER


def _ist_today() -> str:
    """Today's date on the exchange's clock (IST)."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date().isoformat()


def _saved_today(env_file: Path) -> bool:
    """True when a login already succeeded today AND a cookie is still available.

    Both halves are required. A marker without a cookie means the .env was edited by hand
    since the login, so the cookie must be obtained again rather than assumed.
    """
    try:
        if _marker_path(env_file).read_text(encoding="utf-8").strip() != _ist_today():
            return False
    except OSError:
        return False
    return bool((os.environ.get(SESSION_KEY) or "").strip())


def mark_signed_in_today(env_file: Optional[Path] = None) -> None:
    """Record that a login succeeded today. Best effort: a failure only costs one re-login."""
    target = _marker_path(Path(env_file) if env_file is not None else DEFAULT_ENV_FILE)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_ist_today() + "\n", encoding="utf-8")
    except OSError:
        pass


def _first_set(names) -> str:
    """The first environment value that is set and non-blank. Never logs the value."""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def credentials_present() -> bool:
    """True when both a username and a password are available. Reads names only."""
    return bool(_first_set(_USERNAME_KEYS) and _first_set(_PASSWORD_KEYS))


def _base_url() -> str:
    """The screener host, taken from URL_SCREENER_DOT_IN in .env.

    That is the only screen-URL name the code reads (screener_engine.py parses the id and
    slug out of the same variable). With it unset the fallback is the public site, which is
    correct because the login form is on the same host as the screen.
    """
    raw = (os.environ.get("URL_SCREENER_DOT_IN") or "").strip()
    if raw:
        from urllib.parse import urlparse

        parsed = urlparse(raw)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        if raw.startswith("www.") or raw.startswith("screener."):
            return "https://" + raw.rstrip("/")
    return _FALLBACK_BASE


def _client(base_url: str):
    """Build the existing ScreenerClient. Blue's package is loaded through the bridge."""
    try:
        module = load("screener.client.screener_client")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(f"screener client unavailable: {exc}") from exc
    return module.ScreenerClient(
        base_url=base_url,
        min_interval_ms=400,          # the polite default; a sign-in makes 2 requests
        max_retries=1,                # never hammer a login form
        timeout_ms=20_000,
        cache_dir=None,               # a login is not cacheable
    )


def _load_env(env_file: Optional[Path] = None) -> Path:
    """Read the repository .env into ``os.environ`` and return its path.

    Every screener credential and the screen URL come from THIS FILE and nowhere else
    (owner rule 2026-11-26). ``load_env_file`` does not override a variable the shell
    already set, so an explicit export still wins.

    Calling it here - rather than relying on the caller to have done it - is what makes
    the bot runner and the dashboard behave the same. Before this, a bot started from a
    shell that had not sourced .env saw no credentials at all and reported
    "credentials missing" while they sat in the file.
    """
    from quant.core.env import load_env_file

    target = Path(env_file) if env_file is not None else DEFAULT_ENV_FILE
    load_env_file(target)
    return target


def refresh_session(env_file: Optional[Path] = None,
                    client: Any = None,
                    reuse_today: bool = True) -> Dict[str, Any]:
    """Log in with the username + password in .env and persist the sessionid back to .env.

    Args:
        env_file: the .env to read and update. Defaults to the repository root .env.
        client: test seam - any object with ``login(username, password) -> ApiResponse``
            and ``base_url``. Injected, no network is touched.
        reuse_today: when True (the default, owner request 2026-11-26), a session saved
            earlier TODAY is reused and NO login is attempted - "refresh once, then get the
            id and reuse it". Set False only to force a fresh login (for example after an
            incident, when the desk knows the cookie is dead).

    Returns:
        ``{"ok", "saved", "reason"}``. The cookie value is deliberately NOT part of the
        reply - a dashboard response is rendered in a browser. A reused session returns
        ``saved=True`` with ``reason="session reused from today"``.

    Never raises for an expected failure: a dashboard button must not 500 because
    screener.in changed its login page. A bad .env path still raises ``EnvWriteError``.
    """
    target = _load_env(env_file)
    result: Dict[str, Any] = {"ok": False, "saved": False, "reason": ""}

    if reuse_today and _saved_today(target):
        # A login already succeeded today and the cookie is in .env (or the environment).
        # Owner request 2026-11-26: do not sign in again for the same day.
        result.update(ok=True, saved=True, reason="session reused from today")
        _log.info("screener session reused for today; no login attempted")
        return result

    username = _first_set(_USERNAME_KEYS)
    password = _first_set(_PASSWORD_KEYS)
    if not (username and password):
        # Name the exact keys to put in .env. A vague "credentials missing" is what sent the
        # desk looking for a username to type when the answer was: fill these two lines.
        missing = []
        if not username:
            missing.append("SCREENER_USERNAME")
        if not password:
            missing.append("SCREENER_PASSWORD")
        result["reason"] = f"credentials missing in .env: {', '.join(missing)}"
        _log.warning("screener sign-in skipped: %s not set in .env", ", ".join(missing))
        return result

    try:
        active = client if client is not None else _client(_base_url())
    except Exception as exc:  # noqa: BLE001 - report, never crash the button
        result["reason"] = f"screener client unavailable: {type(exc).__name__}"
        _log.warning("screener sign-in skipped: %s", result["reason"])
        return result

    try:
        reply = active.login(username, password)
    except Exception as exc:  # noqa: BLE001 - the client contract is "never raise", but be safe
        result["reason"] = f"login raised {type(exc).__name__}"
        _log.warning("screener sign-in failed: %s", result["reason"])
        return result

    if not getattr(reply, "ok", False):
        # The client's error text names no credential; it is safe to show the operator.
        result["reason"] = f"login failed: {getattr(reply, 'error', '') or 'no reason given'}"
        _log.warning("screener sign-in refused: %s", getattr(reply, "error", "") or "unknown")
        return result

    data = getattr(reply, "data", None)
    session_id = str((data or {}).get("sessionid") or "") if isinstance(data, dict) else ""
    if not session_id:
        result["reason"] = "login ok but no session cookie came back"
        _log.warning("screener sign-in: %s", result["reason"])
        return result

    result["ok"] = True
    try:
        env_write.write_env_values(target, {SESSION_KEY: session_id})
    except Exception as exc:  # noqa: BLE001 - EnvWriteError/OSError: report, do not crash
        # The login DID succeed and the cookie is live in the client's session; only the
        # persistence failed. Say so rather than claim the .env was updated.
        result["reason"] = f"session live but .env not updated: {type(exc).__name__}"
        _log.warning("screener sign-in ok, .env write failed (%s) for %s",
                     type(exc).__name__, target.name)
        return result

    # The running process must see the new cookie without a restart. Value not logged.
    env_write.set_quoted(SESSION_KEY, session_id)
    mark_signed_in_today(target)   # the rest of today reuses this session: no second login
    result["saved"] = True
    result["reason"] = "session saved to .env"
    _log.info("screener session refreshed and saved to %s", target.name)
    return result


def screener_login_after_signin(actor: str, audit_path, runner=None) -> Dict[str, Any]:
    """Refresh the Screener.in session when the OWNER signs in to the dashboard.

    Runs in a background thread started by the dashboard login, so it never blocks the login
    and never raises. ``reuse_today=True``: a login already saved today is reused with no
    network call, so repeated sign-ins do not hit screener.in. Audits the outcome only, never
    the cookie value. ``runner`` is a test seam.
    """
    from quant.audit.log import AuditLog

    run = runner or (lambda: refresh_session(reuse_today=True))
    try:
        result = run()
    except Exception as exc:  # noqa: BLE001 - a screener problem must never break the dashboard
        result = {"ok": False, "saved": False, "reason": type(exc).__name__}
    AuditLog(audit_path).append("screener_autologin_after_signin", {
        "ok": bool(result.get("ok")), "saved": bool(result.get("saved")),
        "reason": str(result.get("reason", ""))[:200], "actor": actor})
    return result
