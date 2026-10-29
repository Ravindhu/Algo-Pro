"""Supervisor tests (audit 2026-10-29, P0-7: nothing restarted the engine).

The supervisor's contract is narrow on purpose: restart ONLY recoverable
failures, ONLY when every safety gate passes, and NEVER after a safety stop.
These tests pin that contract, using an injected clock/sleep so the whole
state machine runs instantly.
"""
from __future__ import annotations

import pytest

from blue_engine.core.supervisor import (
    EXIT_FEED_UNAVAILABLE,
    EXIT_OK,
    EXIT_SAFETY_STOP,
    GateResult,
    Supervisor,
    SupervisorConfig,
    build_default_gates,
    kill_switch_gate,
    positionbook_gate,
    quote_gate,
    working_orders_gate,
)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def _sup(codes, *, gates=None, max_restarts=5, window=3600.0, delay=5.0,
         preflight=True):
    clock = Clock()
    codes = list(codes)  # noqa: F841 - consumed by the injected runner
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return codes.pop(0) if codes else EXIT_OK

    sup = Supervisor(
        SupervisorConfig(command=["engine"], restart_delay_seconds=delay,
                         max_restarts=max_restarts,
                         restart_window_seconds=window,
                         preflight_on_start=preflight),
        runner=runner, gates=gates or [], clock=clock, sleep=clock.sleep,
        log=lambda _m: None)
    return sup, calls, clock


# --------------------------------------------------------------------------- #
# Restart policy
# --------------------------------------------------------------------------- #
def test_clean_exit_is_not_restarted():
    sup, calls, _ = _sup([EXIT_OK])
    sup.run()
    assert len(calls) == 1
    assert sup.state == "STOPPED"


def test_safety_stop_is_never_restarted():
    sup, calls, _ = _sup([EXIT_SAFETY_STOP, EXIT_OK])
    sup.run()
    assert len(calls) == 1, "a kill switch / CRITICAL stop must not retry"
    assert sup.state == "HALTED"
    assert "operator" in (sup.halt_reason or "").lower()


def test_recoverable_crash_is_restarted_then_completes():
    sup, calls, clock = _sup([1, 1, EXIT_OK])
    sup.run()
    assert len(calls) == 3
    assert sup.restarts and len(sup.restarts) == 2
    # Exponential backoff: 5s then 10s.
    assert clock.slept == [5.0, 10.0]


def test_feed_unavailable_exit_is_restartable():
    decision = _sup([1])[0].should_restart(EXIT_FEED_UNAVAILABLE)
    assert decision["restart"] is True


def test_restart_budget_halts_the_supervisor():
    sup, calls, _ = _sup([1, 1, 1, 1, 1], max_restarts=2)
    sup.run()
    assert sup.state == "HALTED"
    assert "budget" in (sup.halt_reason or "")
    assert len(calls) == 3          # first run + 2 allowed restarts


def test_backoff_is_capped():
    sup, _, clock = _sup([1, 1, 1, 1, EXIT_OK], max_restarts=10,
                         delay=5.0)
    sup.config.max_restart_delay_seconds = 20.0
    sup.run()
    assert max(clock.slept) <= 20.0


def test_explicit_restart_code_list_is_honoured():
    sup = _sup([1])[0]
    sup.config.restart_on_exit_codes = [7]
    assert sup.should_restart(1)["restart"] is False
    assert sup.should_restart(7)["restart"] is True


def test_max_cycles_bounds_the_loop():
    sup, calls, _ = _sup([1, 1, 1], max_restarts=99)
    sup.run(max_cycles=2)
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
# Gates must block the start
# --------------------------------------------------------------------------- #
def _fail_gate():
    return GateResult("fake", False, "blocked for the test")


def test_a_failed_gate_blocks_the_first_start():
    sup, calls, _ = _sup([EXIT_OK], gates=[_fail_gate])
    code = sup.run_once()
    assert code == EXIT_SAFETY_STOP
    assert calls == [], "the engine must not start when a gate blocks"
    assert sup.state == "HALTED"


def test_gates_run_before_every_restart():
    state = {"ok": True}
    seen = []

    def gate():
        seen.append(state["ok"])
        return GateResult("g", state["ok"], "")

    codes = [1, EXIT_OK]
    sup, calls, _ = _sup(codes, gates=[gate])
    # Make the gate fail only after the first engine run.
    def runner(cmd):
        calls.append(cmd)
        state["ok"] = False
        return codes.pop(0)
    sup._runner = runner
    sup.run()
    assert seen == [True, False]
    assert len(calls) == 1, "a blocked restart must not reach the engine"
    assert sup.state == "HALTED"
    assert "g" in (sup.halt_reason or "")


def test_a_raising_gate_blocks_and_is_recorded():
    def gate():
        raise RuntimeError("probe exploded")

    sup, calls, _ = _sup([EXIT_OK], gates=[gate])
    sup.run_once()
    assert calls == []
    assert sup.state == "HALTED"
    assert "probe exploded" in (sup.halt_reason or "")


def test_preflight_disabled_still_runs_the_engine():
    sup, calls, _ = _sup([EXIT_OK], gates=[_fail_gate], preflight=False)
    sup.run_once()
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #
def test_journal_records_the_lifecycle(tmp_path):
    journal = tmp_path / "j.jsonl"
    sup, _calls, _clock = _sup([1, EXIT_OK])
    sup.config.journal_path = str(journal)
    sup.run()
    kinds = [e["kind"] for e in sup.events]
    assert "start" in kinds and "stop" in kinds
    assert "restart_decision" in kinds
    text = journal.read_text(encoding="utf-8")
    assert "restart_decision" in text
    assert text.count("\n") == len(sup.events)


# --------------------------------------------------------------------------- #
# The real gates, against stub brokers
# --------------------------------------------------------------------------- #
class StubBroker:
    def __init__(self, *, positions=None, orders=None, quote_price=100.0,
                 pos_ok=True, ord_ok=True, quote_ok=True):
        self._positions = positions or []
        self._orders = orders or []
        self._quote = quote_price
        self._pos_ok = pos_ok
        self._ord_ok = ord_ok
        self._quote_ok = quote_ok

    def positionbook(self):
        if not self._pos_ok:
            return {"status": "error", "message": "session expired"}
        return {"status": "success", "data": self._positions}

    def orderbook(self):
        if not self._ord_ok:
            return {"status": "error"}
        return {"status": "success", "data": self._orders}

    def quotes(self, *, symbol, exchange):
        if not self._quote_ok:
            return {"status": "error"}
        return {"status": "success", "data": {"ltp": self._quote,
                                              "symbol": symbol,
                                              "exchange": exchange}}


def test_positionbook_gate_accepts_a_flat_verified_book():
    assert positionbook_gate(StubBroker()).ok is True


def test_positionbook_gate_rejects_an_unreadable_book():
    result = positionbook_gate(StubBroker(pos_ok=False))
    assert result.ok is False, "unknown must never read as flat"
    assert "session expired" in result.detail


def test_positionbook_gate_counts_open_positions():
    result = positionbook_gate(StubBroker(positions=[{"netqty": 10},
                                                    {"netqty": "0"}]))
    assert result.ok is True
    assert "1 open" in result.detail


def test_working_orders_gate_blocks_stranded_exits():
    orders = [{"strategy": "POV-EXIT", "order_status": "trigger pending"}]
    result = working_orders_gate(StubBroker(orders=orders),
                                 strategy_tags=["POV-EXIT"])
    assert result.ok is False
    assert "reconciliation" in result.detail


def test_working_orders_gate_ignores_terminal_and_foreign_orders():
    orders = [{"strategy": "POV-EXIT", "order_status": "cancelled"},
              {"strategy": "OTHER", "order_status": "open"}]
    assert working_orders_gate(StubBroker(orders=orders),
                               strategy_tags=["POV-EXIT"]).ok is True


def test_quote_gate_needs_a_positive_price():
    assert quote_gate(StubBroker(quote_price=0.0), "INFY", "NSE").ok is False
    assert quote_gate(StubBroker(quote_price=1480.5), "INFY", "NSE").ok is True


def test_default_gate_chain_stops_at_the_first_failure():
    client = StubBroker(pos_ok=False)
    gates = build_default_gates(client, "http://127.0.0.1:1", "INFY", "NSE")
    # Pin the network gate so the test never depends on a closed port timing
    # out; the ordering under test is "first failure ends the chain".
    gates[1] = lambda: GateResult("openalgo", True, "stubbed up")
    sup = Supervisor(SupervisorConfig(command=["engine"], preflight_on_start=True),
                     runner=lambda cmd: 0, gates=gates, log=lambda _m: None,
                     clock=Clock(), sleep=lambda _s: None)
    out = sup.preflight()
    assert out["status"] == "blocked"
    # openalgo is stubbed up, so the chain stops at the unreadable positionbook
    # and never reaches the orderbook/quote gates after it.
    assert out["first_failure"]["gate"] == "positionbook"
    assert [g["gate"] for g in out["gates"]] == ["kill_switch", "openalgo",
                                                  "positionbook"]


def test_kill_switch_gate_reads_the_durable_store(monkeypatch):
    import blue_engine.portfolio.optim_policy as policy
    monkeypatch.setattr(policy, "peek_kill_switch",
                        lambda: {"tripped": True, "reason": "exit engine CRITICAL"})
    result = kill_switch_gate()
    assert result.ok is False
    assert "exit engine CRITICAL" in result.detail

    monkeypatch.setattr(policy, "peek_kill_switch",
                        lambda: {"tripped": False})
    assert kill_switch_gate().ok is True


# --------------------------------------------------------------------------- #
# Dashboard status reader
# --------------------------------------------------------------------------- #
def test_status_without_a_journal_is_explicitly_unknown(tmp_path):
    from blue_engine.core.supervisor import read_status
    status = read_status(str(tmp_path / "missing.jsonl"))
    assert status["available"] is False
    assert status["state"] == "unknown", "no journal must not read as running"


def test_status_reports_halted_from_the_journal(tmp_path):
    import json as _json
    from blue_engine.core.supervisor import read_status
    path = tmp_path / "j.jsonl"
    records = [
        {"ts": "2026-10-29T09:15:00", "kind": "start", "command": ["e"]},
        {"ts": "2026-10-29T09:16:00", "kind": "halted",
         "reason": "preflight blocked at kill_switch"},
    ]
    path.write_text("\n".join(_json.dumps(r) for r in records) + "\n",
                    encoding="utf-8")
    status = read_status(str(path))
    assert status["available"] is True
    assert status["state"] == "HALTED"
    assert "kill_switch" in status["halt_reason"]


def test_status_survives_a_torn_journal_line(tmp_path):
    from blue_engine.core.supervisor import read_status
    path = tmp_path / "j.jsonl"
    path.write_text('{"ts": "t", "kind": "start"}\n{"broken": \n',
                    encoding="utf-8")
    status = read_status(str(path))
    assert status["available"] is True
    assert status["state"] == "RUNNING"


def test_status_never_raises_on_a_directory_path(tmp_path):
    from blue_engine.core.supervisor import read_status
    status = read_status(str(tmp_path))
    assert status["available"] is False
