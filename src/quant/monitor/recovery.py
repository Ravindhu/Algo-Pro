"""Supervised recovery: restart a dead process, then decide whether it may RESUME.

Flow stage: Supervisor/recovery
Status: PHASE 4 IMPLEMENTED (paper only; live resume always needs a human)

THE ONE RULE
------------
**Restart is not resume.** AGENTS.md is explicit:

    "if the process dies, the supervisor restarts it through the gates. But
     restart is not resume. Resuming live trading needs all checks passed;
     otherwise the system stays parked and asks for a human."

So this module separates two things that are easy to conflate:

* ``restart()`` - bring the process back. Always allowed, because a dead
  monitor helps nobody.
* ``resume_decision()`` - may it trade again? Answer ``resume`` only when every
  integrity check passes. Anything unknown or failed returns ``parked`` plus the
  reason list, and the caller must surface it.

FAIL CLOSED ON UNKNOWN
----------------------
A check that raises, or returns something that is not a bool, counts as FAILED.
A guard that silently treats "I could not check" as "fine" is not a guard. The
same rule is used by the kill-switch store (``_unreadable_state`` returns
tripped=True) and by the risk gate.

WHY CHECKS ARE INJECTED
-----------------------
The real checks need a broker connection, a filled ledger and a risk gate. Those
are not available in paper mode or in tests, so the caller passes a dict of
zero-argument callables. This keeps the policy testable without a broker and
keeps the live wiring in one place (the runner), instead of a hidden import
chain this module cannot see.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

#: The checks that must all pass before live trading may resume. Order is the
#: order AGENTS.md lists them, so a log line reads the same as the rule.
REQUIRED_CHECKS = (
    "broker_connected",
    "openalgo_connected",
    "market_data_fresh",
    "positions_verified",
    "orders_verified",
    "risk_limits_set",
    "kill_switch_clear",
    "calendar_verified",
)


@dataclass
class ResumeDecision:
    """The verdict. Plain data so it can be audited and shown on the dashboard."""

    action: str                       # "resume" | "parked"
    failed: List[str] = field(default_factory=list)
    unknown: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    requires_human: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "failed": list(self.failed),
            "unknown": list(self.unknown),
            "reasons": list(self.reasons),
            "requires_human": self.requires_human,
        }


def _run_check(check: Callable[[], Any]) -> tuple:
    """(passed, note). Anything that is not a clean True counts as not passed."""
    try:
        out = check()
    except Exception as exc:  # noqa: BLE001 - a broken check must not pass
        return False, f"raised {type(exc).__name__}: {exc}"
    if out is True:
        return True, "ok"
    if out is False:
        return False, "returned False"
    return False, f"returned {type(out).__name__}={out!r} (not a bool True)"


def resume_decision(checks: Dict[str, Callable[[], Any]],
                    *, live: bool = True) -> ResumeDecision:
    """May trading resume? All of :data:`REQUIRED_CHECKS` must pass.

    ``live=False`` (paper) still runs the checks, but a missing check is
    reported rather than treated as fatal - paper has no broker to verify
    positions against. Live refuses to resume on a missing check.
    """
    failed: List[str] = []
    unknown: List[str] = []
    reasons: List[str] = []

    for name in REQUIRED_CHECKS:
        check = checks.get(name)
        if check is None:
            if live:
                failed.append(name)
                reasons.append(f"{name}: NOT WIRED (live resume requires it)")
            else:
                unknown.append(name)
                reasons.append(f"{name}: not wired (accepted in paper mode)")
            continue
        passed, note = _run_check(check)
        if passed:
            reasons.append(f"{name}: ok")
        else:
            failed.append(name)
            reasons.append(f"{name}: {note}")

    if failed:
        return ResumeDecision("parked", failed=failed, unknown=unknown, reasons=reasons,
                              requires_human=True)
    return ResumeDecision("resume", failed=[], unknown=unknown, reasons=reasons,
                          requires_human=live)


class Supervisor:
    """Restart a process through the gates, and record every event.

    Deliberately small: it does not own a process table. The caller supplies
    ``spawn`` (start it) and ``is_alive`` (ask whether it is running), which
    keeps this module usable for a subprocess, a thread or a remote worker.
    """

    def __init__(self, spawn: Callable[[], Any], is_alive: Callable[[], bool],
                 audit: Optional[object] = None, alerts: Optional[object] = None,
                 max_restarts: int = 3) -> None:
        self.spawn = spawn
        self.is_alive = is_alive
        self.audit = audit
        self.alerts = alerts
        self.max_restarts = max(0, int(max_restarts))
        self.restarts = 0
        self.history: List[Dict[str, Any]] = []

    def _record(self, kind: str, payload: Dict[str, Any]) -> None:
        self.history.append({"kind": kind, **payload})
        if self.audit is not None:
            self.audit.append(kind, payload)

    def restart_if_dead(self, checks: Optional[Dict[str, Callable[[], Any]]] = None,
                        *, live: bool = True) -> Dict[str, Any]:
        """Restart a dead process, then decide resume. Never resumes live by itself.

        Returns ``{"restarted", "action", "decision"}``. A process that is alive
        is left alone and its resume decision is still evaluated, because
        "alive" says nothing about whether it is safe.
        """
        alive = False
        try:
            alive = self.is_alive() is True
        except Exception as exc:  # noqa: BLE001
            self._record("recovery_check_failed", {"error": str(exc)})
            return {"restarted": False, "action": "parked",
                    "decision": ResumeDecision(
                        "parked", failed=["is_alive"],
                        reasons=[f"is_alive raised {type(exc).__name__}: {exc}"]).to_dict()}

        restarted = False
        if not alive:
            self._record("process_died", {"restarts_so_far": self.restarts})
            if self.alerts is not None:
                self.alerts.emit("CRITICAL", "supervisor", "process died; restarting")
            if self.restarts >= self.max_restarts:
                self._record("restart_refused",
                             {"reason": f"restart cap {self.max_restarts} reached"})
                decision = ResumeDecision(
                    "parked", failed=["restart_cap"],
                    reasons=[f"restart cap {self.max_restarts} reached; human needed"])
                return {"restarted": False, "action": "parked",
                        "decision": decision.to_dict()}
            try:
                self.spawn()
                restarted = True
                self.restarts += 1
            except Exception as exc:  # noqa: BLE001 - a failed spawn is a parked system
                self._record("restart_failed", {"error": str(exc)})
                decision = ResumeDecision(
                    "parked", failed=["spawn"],
                    reasons=[f"spawn raised {type(exc).__name__}: {exc}"])
                return {"restarted": False, "action": "parked",
                        "decision": decision.to_dict()}
            self._record("process_restarted", {"restarts_so_far": self.restarts})

        decision = resume_decision(checks or {}, live=live)
        self._record("resume_decision", {"restarted": restarted, **decision.to_dict()})
        if decision.action == "parked" and self.alerts is not None:
            self.alerts.emit("CRITICAL", "supervisor",
                             "recovery parked: " + "; ".join(decision.failed))
        return {"restarted": restarted, "action": decision.action,
                "decision": decision.to_dict()}
