"""Recovery: restart is automatic, RESUME is gated. Live never resumes itself.

Flow stage: Supervisor/recovery
Status: TEST

The invariant under test is AGENTS.md's: "restart is not resume. Resuming live
trading needs all checks passed; otherwise the system stays parked and asks for
a human."
"""
import pytest

from quant.monitor.recovery import (
    REQUIRED_CHECKS,
    ResumeDecision,
    Supervisor,
    resume_decision,
)


def _all_pass():
    return {name: (lambda: True) for name in REQUIRED_CHECKS}


class _Audit:
    def __init__(self):
        self.rows = []

    def append(self, kind, payload):
        self.rows.append((kind, payload))


class _Alerts:
    def __init__(self):
        self.sent = []

    def emit(self, level, component, message):
        self.sent.append((level, component, message))


# --------------------------------------------------------------------------- #
# PASS path
# --------------------------------------------------------------------------- #
def test_all_checks_passing_allows_resume():
    out = resume_decision(_all_pass(), live=True)
    assert out.action == "resume"
    assert out.failed == []
    assert out.requires_human is True   # resume still needs the human sign-off


def test_paper_mode_tolerates_an_unwired_check():
    checks = _all_pass()
    checks.pop("positions_verified")
    out = resume_decision(checks, live=False)
    assert out.action == "resume"
    assert "positions_verified" in out.unknown


# --------------------------------------------------------------------------- #
# BLOCK paths
# --------------------------------------------------------------------------- #
def test_live_refuses_to_resume_on_an_unwired_check():
    checks = _all_pass()
    checks.pop("risk_limits_set")
    out = resume_decision(checks, live=True)
    assert out.action == "parked"
    assert "risk_limits_set" in out.failed
    assert any("NOT WIRED" in r for r in out.reasons)


def test_a_failing_check_parks_the_system():
    checks = _all_pass()
    checks["kill_switch_clear"] = lambda: False
    out = resume_decision(checks, live=True)
    assert out.action == "parked"
    assert out.failed == ["kill_switch_clear"]


def test_a_raising_check_counts_as_failed_not_ok():
    def boom():
        raise RuntimeError("broker socket gone")

    checks = _all_pass()
    checks["broker_connected"] = boom
    out = resume_decision(checks, live=True)
    assert out.action == "parked"
    assert "broker_connected" in out.failed
    assert any("raised RuntimeError" in r for r in out.reasons)


@pytest.mark.parametrize("returned", [None, "yes", 1, [], {}])
def test_a_truthy_value_that_is_not_True_is_not_a_pass(returned):
    """'I could not check' must never be read as 'fine'."""
    checks = _all_pass()
    checks["orders_verified"] = lambda: returned
    out = resume_decision(checks, live=True)
    assert out.action == "parked"
    assert "orders_verified" in out.failed


def test_every_required_check_is_in_the_decision():
    out = resume_decision({}, live=True)
    assert out.action == "parked"
    assert set(out.failed) == set(REQUIRED_CHECKS)


# --------------------------------------------------------------------------- #
# Supervisor
# --------------------------------------------------------------------------- #
def test_a_dead_process_is_restarted_but_not_resumed():
    started = {"n": 0}

    def spawn():
        started["n"] += 1

    sup = Supervisor(spawn=spawn, is_alive=lambda: False)
    checks = _all_pass()
    checks["positions_verified"] = lambda: False
    out = sup.restart_if_dead(checks, live=True)

    assert started["n"] == 1, "restart is automatic"
    assert out["restarted"] is True
    assert out["action"] == "parked", "restart must NOT mean resume"


def test_an_alive_process_is_not_restarted():
    started = {"n": 0}
    sup = Supervisor(spawn=lambda: started.__setitem__("n", started["n"] + 1),
                     is_alive=lambda: True)
    out = sup.restart_if_dead(_all_pass(), live=False)
    assert started["n"] == 0
    assert out["action"] == "resume"


def test_restart_cap_stops_a_crash_loop_and_parks():
    sup = Supervisor(spawn=lambda: None, is_alive=lambda: False, max_restarts=2)
    for _ in range(3):
        out = sup.restart_if_dead({}, live=False)
    assert out["restarted"] is False
    assert out["action"] == "parked"
    assert "restart_cap" in out["decision"]["failed"]


def test_a_failed_spawn_parks_rather_than_retrying_forever():
    def spawn():
        raise OSError("exec failed")

    sup = Supervisor(spawn=spawn, is_alive=lambda: False)
    out = sup.restart_if_dead({}, live=False)
    assert out["restarted"] is False
    assert out["action"] == "parked"
    assert "spawn" in out["decision"]["failed"]


def test_an_is_alive_that_raises_parks_instead_of_assuming_dead():
    def boom():
        raise RuntimeError("process table unreadable")

    sup = Supervisor(spawn=lambda: None, is_alive=boom)
    out = sup.restart_if_dead({}, live=False)
    assert out["action"] == "parked"
    assert "is_alive" in out["decision"]["failed"]


def test_every_recovery_event_is_audited_and_alerted():
    audit = _Audit()
    alerts = _Alerts()
    sup = Supervisor(spawn=lambda: None, is_alive=lambda: False,
                     audit=audit, alerts=alerts)
    sup.restart_if_dead({name: (lambda: False) for name in REQUIRED_CHECKS}, live=True)

    kinds = [k for k, _ in audit.rows]
    assert "process_died" in kinds
    assert "process_restarted" in kinds
    assert "resume_decision" in kinds
    assert any(level == "CRITICAL" for level, _, _ in alerts.sent)


def test_decision_dataclass_serialises_for_the_dashboard():
    out = ResumeDecision("parked", failed=["x"], reasons=["r"])
    assert out.to_dict()["action"] == "parked"
    assert out.to_dict()["failed"] == ["x"]
