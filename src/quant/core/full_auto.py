"""Full Auto: one owner switch for the daily start sequence (owner decision 2026-11-27).

Plain English: when the owner turns Full Auto ON, the dashboard does these steps in order, each
audited, and stops at the first failure:
    1. OpenAlgo analyzer is confirmed ON (sandbox). Full Auto never trades real money.
    2. OpenAlgo and broker sign-in (TOTP), then the saved API key is kept in step.
    3. Screener.in session and the saved screen.
    4. Kill switch must be OFF. It is NEVER reset automatically (owner decision 2).
    5. Engine starts in sandbox mode (the --sandbox runner).
Turning Full Auto OFF stops the engine. Sign-ins are left as they are.

Schedule (IST, weekdays): the start runs once a day between 08:45 and 15:10, and the day-end report
is written once a day from 15:45. Nothing runs while the switch is OFF.

Flow stage: Dashboard -> daily start and report (owner-controlled; sandbox only; no order path here)
Status: PHASE 2 IMPLEMENTED (2026-11-27; tests in tests/unit/test_full_auto.py)

The steps are passed in as functions, so this module makes no network call and sends no order.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from quant.audit.log import AuditLog

START_WINDOW = ((8, 45), (15, 10))   # IST: start only inside this window (before square-off)
REPORT_AT = (15, 45)                 # IST: day-end report
DEFAULT_STATE: Dict[str, Any] = {"enabled": False, "last_start_date": "", "last_report_date": "",
                                 "last_result": {}}


def ist_now(now: Optional[datetime] = None) -> datetime:
    """Naive IST clock. ``now`` is a test seam (an aware or naive datetime in IST)."""
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5, minutes=30)
    if now.tzinfo is not None:
        return now.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(hours=5, minutes=30)
    return now


def _at(now: datetime, hm: Tuple[int, int]) -> datetime:
    return now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def in_start_window(now: datetime) -> bool:
    return now.weekday() < 5 and _at(now, START_WINDOW[0]) <= now < _at(now, START_WINDOW[1])


def read_state(path) -> Dict[str, Any]:
    """Saved switch and last-run markers. A missing or broken file is OFF with no history."""
    state = dict(DEFAULT_STATE)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return state
    if isinstance(raw, dict):
        state["enabled"] = raw.get("enabled") is True
        for key in ("last_start_date", "last_report_date"):
            value = raw.get(key)
            state[key] = value if isinstance(value, str) else ""
        state["last_result"] = raw.get("last_result") if isinstance(raw.get("last_result"), dict) else {}
    return state


def write_state(path, state: Dict[str, Any]) -> None:
    """Atomic write (temp file, then os.replace). Never holds a secret."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix="." + target.name + "-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def due_actions(now: datetime, state: Dict[str, Any]) -> list:
    """What the scheduler should run now: ``start`` and/or ``report``. Empty while the switch is OFF."""
    if not state.get("enabled"):
        return []
    today = now.date().isoformat()
    actions = []
    if in_start_window(now) and state.get("last_start_date") != today:
        actions.append("start")
    if now.weekday() < 5 and now >= _at(now, REPORT_AT) and state.get("last_report_date") != today:
        actions.append("report")
    return actions


def _audit(audit: AuditLog, step: str, ok: bool, detail: str, actor: str) -> None:
    audit.append("full_auto_step", {"step": step, "ok": bool(ok), "detail": str(detail)[:200], "actor": actor})


def run_start(*, actor: str, audit_path, state_path, steps: Dict[str, Callable[[], Tuple[bool, str]]],
              now: Optional[datetime] = None) -> Dict[str, Any]:
    """Run the daily start steps in order. Each step returns ``(ok, detail)``. Stops at the first failure.
    The day is marked as attempted whether or not it passed (one attempt per day).

    ``steps`` must hold these keys, in this order: analyzer, login, screener, kill, engine.
    A missing step counts as a failure (fail closed). Never raises.
    """
    order = ("analyzer", "login", "screener", "kill", "engine")
    audit = AuditLog(audit_path)
    results = []
    failed = ""
    for name in order:
        fn = steps.get(name)
        try:
            ok, detail = fn() if fn is not None else (False, f"{name} step not wired")
        except Exception as exc:  # noqa: BLE001 - one step must not crash the switch
            ok, detail = False, f"{name} raised {type(exc).__name__}"
        ok = bool(ok)
        results.append({"step": name, "ok": ok, "detail": str(detail)[:200]})
        _audit(audit, name, ok, detail, actor)
        if not ok:
            failed = name
            break
    stamp = ist_now(now)
    state = read_state(state_path)
    state["last_result"] = {"at": stamp.isoformat(timespec="seconds"), "ok": not failed,
                            "failed_step": failed, "steps": results}
    # Marked as attempted even on failure: a failed start is NOT retried every minute (that would
    # re-trigger broker sign-ins all day). The owner sees the failure and can retry next day.
    state["last_start_date"] = stamp.date().isoformat()
    write_state(state_path, state)
    return state["last_result"]


def set_enabled(*, actor: str, audit_path, state_path, on: bool) -> Dict[str, Any]:
    """Turn the switch on or off, audited. Turning it off does not stop the engine by itself."""
    state = read_state(state_path)
    state["enabled"] = bool(on)
    write_state(state_path, state)
    AuditLog(audit_path).append("full_auto_switch", {"on": bool(on), "actor": actor})
    return state


def record_report(*, state_path, day: str) -> None:
    state = read_state(state_path)
    state["last_report_date"] = day
    write_state(state_path, state)
