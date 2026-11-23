"""scripts/run/engine_entry.py: an analyze run gets a guarded gateway; live is refused."""
import importlib.util
from pathlib import Path

import pytest

from quant.execution.openalgo_client import EngineOrderGateway

ROOT = Path(__file__).resolve().parents[3]


def _load_entry():
    spec = importlib.util.spec_from_file_location("engine_entry", ROOT / "scripts/run/engine_entry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Sdk:
    pass


def test_analyze_mode_gets_a_guarded_gateway(monkeypatch):
    entry = _load_entry()
    monkeypatch.setattr(entry, "init_client", lambda **kw: _Sdk())
    monkeypatch.setattr(entry, "read_mode", lambda c: "analyze")
    gw = entry.build_order_client({"api_key": "k", "host": "http://h:5000"})
    assert isinstance(gw, EngineOrderGateway)


def test_live_mode_is_refused(monkeypatch):
    entry = _load_entry()
    monkeypatch.setattr(entry, "init_client", lambda **kw: _Sdk())
    monkeypatch.setattr(entry, "read_mode", lambda c: "live")
    with pytest.raises(RuntimeError, match="refused"):
        entry.build_order_client({"api_key": "k"})


def test_unreadable_mode_fails_closed(monkeypatch):
    entry = _load_entry()
    monkeypatch.setattr(entry, "init_client", lambda **kw: _Sdk())

    def boom(c):
        raise ConnectionError("no OpenAlgo")
    monkeypatch.setattr(entry, "read_mode", boom)
    with pytest.raises(ConnectionError):
        entry.build_order_client({"api_key": "k"})


def test_live_mode_without_live_flag_is_refused(monkeypatch):
    entry = _load_entry()
    monkeypatch.setattr(entry, "init_client", lambda **kw: _Sdk())
    monkeypatch.setattr(entry, "read_mode", lambda c: "live")
    with pytest.raises(RuntimeError, match="refused"):
        entry.build_order_client({"api_key": "k"}, live=False)


def _live_env(monkeypatch, entry, tmp_path, checks):
    monkeypatch.setattr(entry, "init_client", lambda **kw: _Sdk())
    monkeypatch.setattr(entry, "read_mode", lambda c: "live")

    class _Checks:
        @staticmethod
        def startup_checks(**kw):
            return checks
    monkeypatch.setattr(entry, "_live_checks", lambda: _Checks)
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("x: 1\n", encoding="utf-8")
    return limits


def test_live_refused_when_start_gate_fails(monkeypatch, tmp_path):
    entry = _load_entry()
    limits = _live_env(monkeypatch, entry, tmp_path, {"ok": False, "reasons": ["kill switch on"]})
    with pytest.raises(RuntimeError, match="kill switch on"):
        entry.build_order_client({"api_key": "k"}, live=True, limits_path=limits, state_dir=tmp_path)


def test_live_refused_without_approval(monkeypatch, tmp_path):
    entry = _load_entry()
    limits = _live_env(monkeypatch, entry, tmp_path, {"ok": True, "approval": None, "capital_cap": 1000})
    with pytest.raises(RuntimeError, match="approval"):
        entry.build_order_client({"api_key": "k"}, live=True, limits_path=limits, state_dir=tmp_path)


def test_live_refused_without_capital_cap(monkeypatch, tmp_path):
    entry = _load_entry()
    limits = _live_env(monkeypatch, entry, tmp_path, {"ok": True, "approval": object(), "capital_cap": None})
    with pytest.raises(RuntimeError, match="capital cap"):
        entry.build_order_client({"api_key": "k"}, live=True, limits_path=limits, state_dir=tmp_path)


def test_live_gateway_built_when_every_gate_passes(monkeypatch, tmp_path):
    entry = _load_entry()
    limits = _live_env(monkeypatch, entry, tmp_path,
                       {"ok": True, "approval": object(), "capital_cap": 100000.0})
    gw = entry.build_order_client({"api_key": "k"}, live=True, limits_path=limits, state_dir=tmp_path)
    assert isinstance(gw, EngineOrderGateway)
