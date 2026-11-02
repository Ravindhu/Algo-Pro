"""Regression test (audit 2026-11-03): protection must run after a monitor halt.

Before the fix, a monitor halt returned from the runner before the exit engine
ran, so a partial fill was left without its stop.
"""
from blue_engine.core import runner


def _capture(monkeypatch, exit_rc=False):
    calls = []

    def fake_exit_engine(client, raw, filled):
        calls.append(filled)
        return exit_rc

    monkeypatch.setattr(runner, "_run_exit_engine", fake_exit_engine)
    return calls


def test_halt_after_fill_still_runs_protection(monkeypatch):
    calls = _capture(monkeypatch)
    rc = runner._finish_execution(None, {}, {"filled": 3}, True, None)
    assert calls == [3], "exit engine must run for the filled quantity"
    assert rc == 4, "a halted run still exits non-zero"


def test_clean_run_protects_and_exits_zero(monkeypatch):
    calls = _capture(monkeypatch)
    rc = runner._finish_execution(None, {}, {"filled": 5}, False, None)
    assert calls == [5]
    assert rc == 0


def test_critical_protection_failure_exits_four(monkeypatch):
    _capture(monkeypatch, exit_rc=True)
    assert runner._finish_execution(None, {}, {"filled": 1}, False, None) == 4


def test_non_dict_summary_protects_zero(monkeypatch):
    calls = _capture(monkeypatch)
    runner._finish_execution(None, {}, None, False, None)
    assert calls == [0]
