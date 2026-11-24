"""Broker login after a dashboard sign-in (owner decision 2026-11-24).

Status: PHASE 1 IMPLEMENTED (sign-in triggered login; tests in tests/unit/test_broker_signin.py)

A successful dashboard sign-in also logs in OpenAlgo and the broker. The login
runs in a background thread so the sign-in reply is not delayed. Live mode is
included. The broker login cooldown still applies. Only the result is audited:
no secret and no TOTP code.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from quant.audit.log import AuditLog

REASON_MAX = 200


def _default_runner(**kwargs: Any) -> Dict[str, Any]:
    from quant.engines.core import broker_auth  # imported late: keeps this module light
    return broker_auth.run_scheduled_login(**kwargs)


def login_after_signin(actor: str, audit_path: Path,
                       runner: Optional[Callable[..., Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Run the broker login and audit the outcome. Never raises."""
    run = runner or _default_runner
    try:
        result = run(enable=True, allow_live=True)
    except Exception as exc:  # a login failure must never break the dashboard
        result = {"ok": False, "reason": type(exc).__name__, "leg": ""}
    AuditLog(audit_path).append("broker_autologin_after_signin", {
        "ok": bool(result.get("ok")), "skipped": bool(result.get("skipped")),
        "leg": str(result.get("leg", ""))[:40], "reason": str(result.get("reason", ""))[:REASON_MAX],
        "actor": actor})
    return result
