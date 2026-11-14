"""Broker auth wrapper: read-only login status, DELEGATED to Blue_engine.

Flow stage: Broker auth
Status: PHASE 5 IMPLEMENTED (wrap; status read only: never logs in, never reads credentials)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/broker_auth.py
(autologin_enabled, login_guard)

The login itself (BrokerAuth, run_scheduled_login) stays in Blue and is NOT called
here. Turning automated login on is an owner decision (see the flow doc open items).
This module only reports whether autologin is opted in and whether the login
cooldown allows an attempt. ``allowed`` covers the cooldown only.

FAIL-CLOSED RULES
-----------------
- Blue unavailable or any read fails: ``autologin_enabled=False`` and ``allowed=False``.
- Never raises.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["login_status"]


def login_status(now: Optional[float] = None) -> Dict[str, Any]:
    """Return ``{ok, autologin_enabled, allowed, wait_seconds, reason}``."""
    failed = {"ok": False, "autologin_enabled": False, "allowed": False,
              "wait_seconds": None}
    try:
        mod = load("core.broker_auth")
    except BlueEngineUnavailable as exc:
        return {**failed, "reason": f"broker auth unavailable: {exc}"}
    try:
        enabled = bool(mod.autologin_enabled())
        guard = mod.login_guard(now=now)
    except Exception as exc:  # noqa: BLE001
        return {**failed, "reason": f"broker auth status failed: {exc!r}"}
    if not isinstance(guard, dict):
        return {**failed, "reason": "login guard returned no mapping"}
    return {
        "ok": True,
        "autologin_enabled": enabled,
        "allowed": bool(guard.get("allowed", False)),
        "wait_seconds": guard.get("wait_seconds"),
        "reason": str(guard.get("reason") or ""),
    }
