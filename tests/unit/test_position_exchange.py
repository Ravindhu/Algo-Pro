"""Slice 3: positions and broker stops carry their exchange (default NSE). Read or fake only."""
import pytest

from quant.execution.exit_manager import Position
from quant.execution.openalgo_client import OpenAlgoClient
from test_analyze_mode import ON, PLACED, Fake


def _pos(**extra):
    return Position(key="K1", symbol="RELIANCE", side="BUY", qty=10, entry=1500.0, stop=1490.0,
                    target=1520.0, atr=5.0, **extra)


def test_position_defaults_to_nse():
    assert _pos().exchange == "NSE"


def test_position_can_be_held_on_bse():
    assert _pos(exchange="BSE").exchange == "BSE"


def test_position_refuses_unknown_exchange():
    with pytest.raises(ValueError):
        _pos(exchange="MCX")


def test_saved_position_without_exchange_loads_as_nse():
    doc = {"key": "K1", "symbol": "RELIANCE", "side": "BUY", "qty": 10, "entry": 1500.0,
           "stop": 1490.0, "target": 1520.0, "atr": 5.0}  # saved before this field existed
    assert Position(**doc).exchange == "NSE"


def _client(fake):
    return OpenAlgoClient("http://x", fake, mode="analyze", exchange="NSE")


def test_stop_defaults_to_nse_on_the_wire():
    fake = Fake(analyzer=ON, place=PLACED)
    _client(fake).place_stop("RELIANCE", "SELL", 10, 1490.0, "S1", "key")
    assert fake.sent()[-1][1]["exchange"] == "NSE"


def test_stop_on_bse_names_bse():
    fake = Fake(analyzer=ON, place=PLACED)
    _client(fake).place_stop("RELIANCE", "SELL", 10, 1490.0, "S1", "key", exchange="BSE")
    assert fake.sent()[-1][1]["exchange"] == "BSE"


def test_stop_refuses_unknown_exchange_and_sends_nothing():
    fake = Fake(analyzer=ON, place=PLACED)
    with pytest.raises(ValueError):
        _client(fake).place_stop("RELIANCE", "SELL", 10, 1490.0, "S1", "key", exchange="MCX")
    assert fake.sent() == []
