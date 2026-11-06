"""Phase 0: OpenAlgo client key loading uses Blue_engine's .env loader, never the DB path.

Needs the OpenAlgo SDK (Blue_engine imports it). Skips with the reason when absent.
"""
import pytest

from quant.core import blue_bridge
from quant.core.blue_bridge import BlueEngineUnavailable
from quant.execution.openalgo_client import OpenAlgoClient


@pytest.fixture(autouse=True)
def _need_blue():
    try:
        blue_bridge.load("core.client")
    except BlueEngineUnavailable as exc:
        pytest.skip(f"Blue_engine not importable here: {exc}")


def test_key_read_from_environment(monkeypatch):
    monkeypatch.setenv("OPENALGO_API_KEY", "test-key-123")
    assert OpenAlgoClient.api_key_from_env() == "test-key-123"


def test_missing_key_fails_closed(monkeypatch):
    monkeypatch.delenv("OPENALGO_API_KEY", raising=False)
    client_mod = blue_bridge.load("core.client")
    monkeypatch.setattr(client_mod, "load_env_file", lambda *a, **k: False)
    with pytest.raises(RuntimeError):
        OpenAlgoClient.api_key_from_env()


def test_database_key_discovery_is_never_called(monkeypatch):
    """Blue's resolve_api_key can decrypt a key from the OpenAlgo DB. The src path must not."""
    client_mod = blue_bridge.load("core.client")
    monkeypatch.delenv("OPENALGO_API_KEY", raising=False)
    monkeypatch.setattr(client_mod, "load_env_file", lambda *a, **k: False)

    def boom(*a, **k):
        raise AssertionError("resolve_api_key must not be called")

    monkeypatch.setattr(client_mod, "resolve_api_key", boom)
    with pytest.raises(RuntimeError):
        OpenAlgoClient.api_key_from_env()
