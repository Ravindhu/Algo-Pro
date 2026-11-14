"""Scheduled jobs wrapper: read-only job gates and last status, DELEGATED to Blue_engine.

Flow stage: Scheduled jobs
Status: PHASE 5 IMPLEMENTED (wrap; read only: the jobs are never run from here)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/jobs/daily_portfolio.py
(is_market_closed, read_status)

The job runners (run, main) and the report scripts stay in Blue. This module only tells
a scheduler whether the post-close window has opened and what the last run reported.

FAIL-CLOSED RULES
-----------------
- Blue unavailable or a read fails: ``window_open=False`` and no status.
- Never raises.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["window_open", "last_run_status"]


def window_open(now: Optional[datetime] = None) -> Dict[str, Any]:
    """True when it is a weekday at or after the post-close job window. Never raises."""
    try:
        mod = load("jobs.daily_portfolio")
    except BlueEngineUnavailable as exc:
        return {"ok": False, "window_open": False, "reason": f"Blue jobs unavailable: {exc}"}
    try:
        open_now = bool(mod.is_market_closed(now))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "window_open": False, "reason": f"window check failed: {exc!r}"}
    return {"ok": True, "window_open": open_now, "reason": ""}


def last_run_status() -> Dict[str, Any]:
    """The last job status as Blue recorded it. Never raises."""
    try:
        mod = load("jobs.daily_portfolio")
    except BlueEngineUnavailable as exc:
        return {"ok": False, "status": None, "reason": f"Blue jobs unavailable: {exc}"}
    try:
        status = mod.read_status()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": None, "reason": f"status read failed: {exc!r}"}
    return {"ok": status is not None, "status": status,
            "reason": "" if status is not None else "no status recorded"}
