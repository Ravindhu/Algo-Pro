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
