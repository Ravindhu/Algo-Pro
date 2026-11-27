"""Full Auto wiring: the real step functions, the daily tick and the day-end report (2026-11-27).

Plain English: ``full_auto.py`` decides WHAT is due and records results. This module does the work:
each start step calls the existing, already-tested function (analyzer check, broker and OpenAlgo
sign-in, Screener.in session, kill-switch read, engine start in ``analyze`` mode). The tick runs
from a background thread in the dashboard once a minute.

Safety rules held here:
* The engine is only ever started in ``analyze`` (sandbox) mode. There is no live option.
* The kill switch is read, never reset.
* The analyzer must be confirmed ON before the bot starts.
* Sign-in calls use ``allow_live=False``.

Flow stage: Dashboard -> daily start and report (owner-controlled; sandbox only; no order path)
Status: PHASE 2 IMPLEMENTED (2026-11-27; wiring for phases 2 and 3; tests in tests/unit/test_full_auto.py)
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from quant.core import full_auto
from quant.core.engine_control import request_manual_start, running_pid, stop_engine
from quant.risk.kill_switch import KillSwitch

ACTOR = "full-auto"
START_MODE = "analyze"  # sandbox only (owner decision 2026-11-27)


def _analyzer_step(readonly_factory: Callable[[], Any], api_key: Callable[[], str]) -> Tuple[bool, str]:
    key = api_key()
    if not key:
        return False, "OpenAlgo API key is missing"
    if readonly_factory().analyzer_mode(key) is True:
        return True, "analyzer confirmed ON (sandbox)"
    return False, "analyzer is not confirmed ON in OpenAlgo"


def _login_step(login: Callable[[], Dict[str, Any]]) -> Tuple[bool, str]:
    result = login() or {}
    if result.get("cooldown"):
        return False, f"broker login cooldown: {str(result.get('reason', ''))[:120]}"
    ok = bool(result.get("ok")) or bool(result.get("skipped"))
    return ok, str(result.get("reason") or ("signed in" if ok else "login failed"))[:200]


def _screener_step(signin: Callable[[], Dict[str, Any]]) -> Tuple[bool, str]:
    result = signin() or {}
    session = result.get("session") if isinstance(result.get("session"), dict) else {}
    ok = bool(session.get("ok"))
    return ok, str(session.get("reason") or ("Screener.in session ready" if ok else "Screener.in sign-in failed"))[:200]


def _kill_step(kill_path) -> Tuple[bool, str]:
    switch = KillSwitch(kill_path)
    if switch.active:
        return False, f"kill switch is ON ({switch.reason[:120]}) - not reset automatically"
    return True, "kill switch is OFF"


def _engine_step(pid_path, log_path, runner) -> Tuple[bool, str]:
    if running_pid(pid_path):
        return True, "engine already running (sandbox)"
    ok, message, _pid = request_manual_start(kill_active=False, mode=START_MODE, pid_path=pid_path,
                                             log_path=log_path, runner=runner)
    return ok, message


def make_steps(*, readonly_factory, api_key, login, signin, kill_path, pid_path, log_path,
               runner) -> Dict[str, Callable[[], Tuple[bool, str]]]:
    """The five start steps, in order, as the callables ``full_auto.run_start`` expects."""
    return {
        "analyzer": lambda: _analyzer_step(readonly_factory, api_key),
        "login": lambda: _login_step(login),
        "screener": lambda: _screener_step(signin),
        "kill": lambda: _kill_step(kill_path),
        "engine": lambda: _engine_step(pid_path, log_path, runner),
    }


def tick(now: datetime, *, state_path, audit_path, steps: Dict[str, Callable], report: Callable[[str], Any]
         ) -> list:
    """Run whatever is due at ``now``. Returns the actions that ran. Never raises."""
    ran = []
    state = full_auto.read_state(state_path)
    for action in full_auto.due_actions(now, state):
        try:
            if action == "start":
                full_auto.run_start(actor=ACTOR, audit_path=audit_path, state_path=state_path,
                                    steps=steps, now=now)
            elif action == "report":
                day = full_auto.ist_now(now).date().isoformat()
                report(day)
                full_auto.record_report(state_path=state_path, day=day)
            ran.append(action)
        except Exception:  # noqa: BLE001 - the scheduler thread must survive any single failure
            continue
    return ran


def switch_off(*, audit_path, state_path, pid_path) -> Dict[str, Any]:
    """Turn Full Auto OFF and stop the engine. Sign-ins and the kill switch are left as they are."""
    state = full_auto.set_enabled(actor="owner", audit_path=audit_path, state_path=state_path, on=False)
    ok, message, pid = stop_engine(pid_path=pid_path)
    return {"enabled": state["enabled"], "engine_stopped": ok, "message": message, "pid": pid}


def report_for_day(day: str, *, audit_paths, pnl_state_path, reports_dir) -> list:
    """Write the day-end report files. Returns their paths."""
    from quant.reporting import daily_report
    report = daily_report.build(day, audit_paths, pnl_state_path)
    return daily_report.write(report, reports_dir)


def status(state_path, kill_path) -> Dict[str, Any]:
    """Read-only status for the dashboard: switch, last start result, kill reason."""
    state = full_auto.read_state(state_path)
    switch = KillSwitch(kill_path)
    return {
        "enabled": state["enabled"],
        "last_start_date": state["last_start_date"],
        "last_report_date": state["last_report_date"],
        "last_result": state["last_result"],
        "kill_active": switch.active,
        "kill_reason": switch.reason if switch.active else "",
        "mode": START_MODE,
    }
