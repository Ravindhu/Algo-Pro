"""Full Auto: switch, daily start, scheduler tick and OFF (2026-11-27).

Rules under test:
1. Nothing is due while the switch is OFF.
2. The start runs once a day, only on weekdays inside 08:45-15:10 IST.
3. Steps run in order and stop at the first failure. A failed day is NOT retried every minute.
4. The kill switch is read, never reset. A kill switch ON fails the start.
5. The engine is only started in analyze (sandbox) mode. No live mode.
6. OFF stops the engine and leaves the kill switch alone.
"""
import json
from datetime import datetime

import pytest

from quant.core import full_auto as fa
from quant.core import full_auto_steps as fas
from quant.audit.log import AuditLog
from quant.risk.kill_switch import KillSwitch

MON_0900 = datetime(2026, 11, 30, 9, 0)    # Monday IST
MON_1600 = datetime(2026, 11, 30, 16, 0)
SAT_0900 = datetime(2026, 11, 28, 9, 0)    # Saturday


@pytest.fixture
def paths(tmp_path):
    return {"state": tmp_path / "full_auto" / "state.json", "audit": tmp_path / "audit.jsonl",
            "kill": tmp_path / "bot_live" / "kill.json", "pid": tmp_path / "engine.pid",
            "log": tmp_path / "manual.log"}


def _ok(detail="ok"):
    return lambda: (True, detail)


def _all_ok():
    return {n: _ok() for n in ("analyzer", "login", "screener", "kill", "engine")}


# ---- switch and due actions -------------------------------------------------

def test_nothing_due_while_off(paths):
    state = fa.read_state(paths["state"])
    assert state["enabled"] is False
    assert fa.due_actions(MON_0900, state) == []
    assert fa.due_actions(MON_1600, state) == []


def test_set_enabled_is_audited_and_persisted(paths):
    fa.set_enabled(actor="owner", audit_path=paths["audit"], state_path=paths["state"], on=True)
    assert fa.read_state(paths["state"])["enabled"] is True
    kinds = [r["kind"] for r in AuditLog(paths["audit"]).read_all()]
    assert kinds == ["full_auto_switch"]


def test_start_due_in_window_on_weekday_only(paths):
    fa.set_enabled(actor="owner", audit_path=paths["audit"], state_path=paths["state"], on=True)
    state = fa.read_state(paths["state"])
    assert fa.due_actions(MON_0900, state) == ["start"]
    assert fa.due_actions(SAT_0900, state) == []
    assert fa.due_actions(datetime(2026, 11, 30, 8, 30), state) == []      # before window
    assert fa.due_actions(datetime(2026, 11, 30, 15, 15), state) == []     # after window


def test_report_due_after_1545_weekday(paths):
    fa.set_enabled(actor="owner", audit_path=paths["audit"], state_path=paths["state"], on=True)
    state = fa.read_state(paths["state"])
    assert "report" in fa.due_actions(MON_1600, state)
    assert "report" not in fa.due_actions(SAT_0900, state)


def test_ist_now_converts_aware_utc_and_keeps_naive_as_ist():
    assert fa.ist_now(datetime(2026, 11, 30, 9, 0)) == datetime(2026, 11, 30, 9, 0)   # naive = already IST
    aware = datetime(2026, 11, 30, 3, 30, tzinfo=fa.timezone.utc)
    assert fa.ist_now(aware) == datetime(2026, 11, 30, 9, 0)


def test_broken_state_file_is_off(paths):
    paths["state"].parent.mkdir(parents=True, exist_ok=True)
    paths["state"].write_text("{not json", encoding="utf-8")
    assert fa.read_state(paths["state"])["enabled"] is False


# ---- run_start ----------------------------------------------------------------

def test_all_steps_pass_in_order(paths):
    fa.set_enabled(actor="owner", audit_path=paths["audit"], state_path=paths["state"], on=True)
    seen = []
    steps = {n: (lambda n=n: (seen.append(n), (True, "ok"))[1]) for n in ("analyzer", "login", "screener", "kill", "engine")}
    result = fa.run_start(actor="t", audit_path=paths["audit"], state_path=paths["state"], steps=steps, now=MON_0900)
    assert seen == ["analyzer", "login", "screener", "kill", "engine"]
    assert result["ok"] is True and result["failed_step"] == ""
    assert fa.read_state(paths["state"])["last_start_date"] == "2026-11-30"


def test_first_failure_stops_and_later_steps_do_not_run(paths):
    seen = []
    steps = _all_ok()
    steps["login"] = lambda: (seen.append("login") or False, "broker down")
    steps["engine"] = lambda: (seen.append("engine") or True, "x")
    result = fa.run_start(actor="t", audit_path=paths["audit"], state_path=paths["state"], steps=steps, now=MON_0900)
    assert result["ok"] is False and result["failed_step"] == "login"
    assert "engine" not in seen


def test_failed_day_is_marked_attempted_so_no_retry_storm(paths):
    fa.set_enabled(actor="owner", audit_path=paths["audit"], state_path=paths["state"], on=True)
    steps = _all_ok()
    steps["analyzer"] = lambda: (False, "analyzer off")
    fa.run_start(actor="t", audit_path=paths["audit"], state_path=paths["state"], steps=steps, now=MON_0900)
    assert fa.due_actions(datetime(2026, 11, 30, 9, 5), fa.read_state(paths["state"])) == []


def test_missing_step_fails_closed(paths):
    steps = _all_ok()
    del steps["kill"]
    result = fa.run_start(actor="t", audit_path=paths["audit"], state_path=paths["state"], steps=steps, now=MON_0900)
    assert result["failed_step"] == "kill"


def test_raising_step_is_a_failure_not_a_crash(paths):
    steps = _all_ok()
    steps["screener"] = lambda: 1 / 0
    result = fa.run_start(actor="t", audit_path=paths["audit"], state_path=paths["state"], steps=steps, now=MON_0900)
    assert result["failed_step"] == "screener"
    assert "ZeroDivisionError" in result["steps"][-1]["detail"]


def test_each_step_is_audited(paths):
    fa.run_start(actor="t", audit_path=paths["audit"], state_path=paths["state"], steps=_all_ok(), now=MON_0900)
    rows = AuditLog(paths["audit"]).read_all()
    assert [r["payload"]["step"] for r in rows] == ["analyzer", "login", "screener", "kill", "engine"]
    assert all(r["kind"] == "full_auto_step" for r in rows)


# ---- real step functions ------------------------------------------------------

class _RO:
    def __init__(self, mode):
        self.mode = mode

    def analyzer_mode(self, key):
        return self.mode


def test_analyzer_step_needs_confirmed_on(monkeypatch):
    key = lambda: "k"
    assert fas._analyzer_step(lambda: _RO(True), key)[0] is True
    assert fas._analyzer_step(lambda: _RO(False), key)[0] is False
    assert fas._analyzer_step(lambda: _RO(None), key)[0] is False     # unknown = not confirmed
    assert fas._analyzer_step(lambda: _RO(True), lambda: "")[0] is False


def test_login_step_never_passes_cooldown():
    assert fas._login_step(lambda: {"ok": False, "skipped": True, "cooldown": True})[0] is False
    assert fas._login_step(lambda: {"ok": True})[0] is True
    assert fas._login_step(lambda: {"ok": False, "reason": "bad totp"})[0] is False


def test_screener_step_follows_session_ok():
    assert fas._screener_step(lambda: {"session": {"ok": True}})[0] is True
    assert fas._screener_step(lambda: {"session": {"ok": False, "reason": "x"}})[0] is False
    assert fas._screener_step(lambda: {})[0] is False


def test_kill_step_reads_and_never_resets(tmp_path):
    kill = tmp_path / "kill.json"
    assert fas._kill_step(kill)[0] is True
    KillSwitch(kill).trip("test", source="manual")
    ok, detail = fas._kill_step(kill)
    assert ok is False and "not reset automatically" in detail
    assert KillSwitch(kill).active is True          # still ON after the step


def test_engine_step_always_uses_analyze_mode(monkeypatch, tmp_path):
    calls = {}

    def fake_start(**kw):
        calls.update(kw)
        return True, "started", 123

    monkeypatch.setattr(fas, "request_manual_start", fake_start)
    ok, _ = fas._engine_step(tmp_path / "pid", tmp_path / "log", tmp_path / "runner")
    assert ok is True
    assert calls["mode"] == "analyze" and calls["kill_active"] is False


def test_engine_step_refuses_live_by_construction():
    # The only mode the module can request is the sandbox mode.
    assert fas.START_MODE == "analyze"


def test_tick_runs_start_once_a_day(paths):
    fa.set_enabled(actor="owner", audit_path=paths["audit"], state_path=paths["state"], on=True)
    steps = _all_ok()
    fas.tick(MON_0900, state_path=paths["state"], audit_path=paths["audit"], steps=steps, report=lambda d: None)
    fas.tick(datetime(2026, 11, 30, 9, 10), state_path=paths["state"], audit_path=paths["audit"],
             steps=steps, report=lambda d: None)
    starts = [r for r in AuditLog(paths["audit"]).read_all() if r["kind"] == "full_auto_step"]
    assert len(starts) == 5                          # one pass of the five steps only


def test_tick_writes_report_once_and_records_it(paths):
    fa.set_enabled(actor="owner", audit_path=paths["audit"], state_path=paths["state"], on=True)
    days = []
    fas.tick(MON_1600, state_path=paths["state"], audit_path=paths["audit"], steps=_all_ok(),
             report=days.append)
    fas.tick(MON_1600, state_path=paths["state"], audit_path=paths["audit"], steps=_all_ok(),
             report=days.append)
    assert days == ["2026-11-30"]


def test_tick_survives_a_report_failure(paths):
    fa.set_enabled(actor="owner", audit_path=paths["audit"], state_path=paths["state"], on=True)

    def boom(day):
        raise OSError("disk full")

    ran = fas.tick(MON_1600, state_path=paths["state"], audit_path=paths["audit"], steps=_all_ok(), report=boom)
    assert "report" not in ran                       # failed report is not recorded as done


def test_switch_off_stops_engine_and_keeps_kill_switch(monkeypatch, paths):
    fa.set_enabled(actor="owner", audit_path=paths["audit"], state_path=paths["state"], on=True)
    KillSwitch(paths["kill"]).trip("keep me", source="manual")
    monkeypatch.setattr(fas, "stop_engine", lambda pid_path: (True, "stopped", 42))
    out = fas.switch_off(audit_path=paths["audit"], state_path=paths["state"], pid_path=paths["pid"])
    assert out["enabled"] is False and out["engine_stopped"] is True
    assert KillSwitch(paths["kill"]).active is True


def test_status_reports_kill_reason(paths):
    KillSwitch(paths["kill"]).trip("drawdown", source="manual")
    st = fas.status(paths["state"], paths["kill"])
    assert st["enabled"] is False and st["kill_active"] is True and "drawdown" in st["kill_reason"]
    assert st["mode"] == "analyze"
