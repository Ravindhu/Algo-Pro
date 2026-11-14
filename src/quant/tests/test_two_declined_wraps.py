"""Two wraps that were declined before: each one is limited to a safe subset.

Flow stage: Infrastructure engine, Pipeline orchestrator
Status: TEST
"""
from __future__ import annotations

import ast
import datetime as dt
import pathlib

import pytest

from quant.core.blue_bridge import BlueEngineUnavailable
from quant.execution import pipeline_dry_run, supervisor_status
from quant.monitor import health_gate


def _calls(path: pathlib.Path) -> set:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            fn = node.func
            names.add(fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", ""))
    return names


# ----------------------------------------------------------------- infrastructure
def test_health_gate_never_passes_a_client_or_calls_the_broker(monkeypatch):
    seen = {}

    class Fake:
        def __init__(self, cfg=None, provider=None, **kw):
            seen["cfg"] = cfg
        def run(self, context=None):
            seen["context"] = context
            class R:
                ok = True
                data = {"ready_to_trade": False, "checks": {"openalgo": {"ok": True}, "dashboard": {"ok": True}, "servers": {"ok": True}}}
            return R()

    monkeypatch.setattr(health_gate, "load", lambda name: type("M", (), {"InfrastructureEngine": Fake}))
    res = health_gate.health_gate()
    assert "client" not in (seen["context"] or {})
    assert res["broker_checked"] is False and res["ready"] is True


def test_health_gate_not_ready_when_openalgo_port_closed(monkeypatch):
    class Fake:
        def __init__(self, *a, **k):
            pass
        def run(self, context=None):
            class R:
                ok = True
                data = {"checks": {"openalgo": {"ok": False}, "dashboard": {"ok": True}, "servers": {"ok": True}}}
            return R()
    monkeypatch.setattr(health_gate, "load", lambda name: type("M", (), {"InfrastructureEngine": Fake}))
    assert health_gate.health_gate()["ready"] is False


def test_health_gate_missing_section_is_not_ready(monkeypatch):
    class Fake:
        def __init__(self, *a, **k):
            pass
        def run(self, context=None):
            class R:
                ok = True
                data = {"checks": {"openalgo": {"ok": True}}}
            return R()
    monkeypatch.setattr(health_gate, "load", lambda name: type("M", (), {"InfrastructureEngine": Fake}))
    assert health_gate.health_gate()["ready"] is False


def test_health_gate_real_run_has_its_sections():
    res = health_gate.health_gate()
    assert res["broker_checked"] is False
    assert isinstance(res["ready"], bool)
    assert res["ready"] is False or {"openalgo", "dashboard", "servers"} <= set(res["sections"])


def test_health_gate_failure_is_not_ready(monkeypatch):
    monkeypatch.setattr(health_gate, "load", lambda name: (_ for _ in ()).throw(BlueEngineUnavailable("gone")))
    assert health_gate.health_gate()["ready"] is False


def test_health_gate_module_has_no_broker_or_order_call():
    names = _calls(pathlib.Path(health_gate.__file__))
    assert not (names & {"placeorder", "place_order", "funds", "orderbook", "analyzerstatus"})


# ----------------------------------------------------------------- pipeline (dry run only)
class _FakePipe:
    seen = {}
    def __call__(self, cfg, ctx, log):
        _FakePipe.seen = {"cfg": dict(cfg), "ctx": dict(ctx)}
        return {"ok": True, "stages": ["alpha"]}


def test_pipeline_dry_run_is_forced_on_even_if_config_says_live(monkeypatch):
    monkeypatch.setattr(pipeline_dry_run, "load", lambda name: type("M", (), {"run_pipeline": _FakePipe()}))
    res = pipeline_dry_run.run_dry(config={"dry_run": False})
    assert _FakePipe.seen["cfg"]["dry_run"] is True
    assert res["dry_run"] is True and res["order_path"] == "none"


def test_pipeline_dry_run_strips_client_like_keys(monkeypatch):
    monkeypatch.setattr(pipeline_dry_run, "load", lambda name: type("M", (), {"run_pipeline": _FakePipe()}))
    res = pipeline_dry_run.run_dry(context={"client": object(), "sdk_client": object(),
                                            "openalgo_api": "k", "universe": ["A"]})
    assert "client" not in _FakePipe.seen["ctx"]
    assert "sdk_client" not in _FakePipe.seen["ctx"]
    assert _FakePipe.seen["ctx"] == {"universe": ["A"]}
    assert set(res["stripped_keys"]) == {"client", "sdk_client", "openalgo_api"}


def test_pipeline_failure_is_not_ok(monkeypatch):
    def boom(cfg, ctx, log):
        raise RuntimeError("bad config")
    monkeypatch.setattr(pipeline_dry_run, "load", lambda name: type("M", (), {"run_pipeline": boom}))
    res = pipeline_dry_run.run_dry()
    assert res["ok"] is False and res["dry_run"] is True


def test_pipeline_module_never_calls_an_order_or_the_supervisor():
    names = _calls(pathlib.Path(pipeline_dry_run.__file__))
    assert not (names & {"placeorder", "place_order", "send_order", "Supervisor"})


def test_supervisor_module_never_restarts_or_calls_the_broker():
    names = _calls(pathlib.Path(supervisor_status.__file__))
    assert not (names & {"Supervisor", "run_once", "should_restart", "preflight",
                         "positionbook_gate", "working_orders_gate", "quote_gate",
                         "broker_session_gate", "placeorder"})


# ----------------------------------------------------------------- supervisor (read only)
def test_journal_status_is_safe_with_no_journal(tmp_path):
    res = supervisor_status.journal_status(journal_path=str(tmp_path / "missing.jsonl"))
    assert isinstance(res["ok"], bool)


def test_market_hours_refused_on_a_saturday():
    sat = dt.datetime(2026, 11, 14, 11, 0)
    res = supervisor_status.market_hours(now=sat)
    assert res["allowed"] is False


def test_market_hours_fails_closed_when_blue_missing(monkeypatch):
    monkeypatch.setattr(supervisor_status, "load", lambda name: (_ for _ in ()).throw(BlueEngineUnavailable("gone")))
    res = supervisor_status.market_hours()
    assert res["allowed"] is False and res["ok"] is False
