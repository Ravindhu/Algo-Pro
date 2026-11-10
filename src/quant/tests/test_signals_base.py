"""Flow stage: Strategy (signal package).
Status: TESTS PASS (10/10, sandbox run).

Unit tests for the signal base class, check_signal() and the sample template.
"""
import pandas as pd
import pytest

from quant.strategies.signals.base import SignalStrategy, check_signal
from quant.strategies.signals.sample_strategy import SampleStrategy


def _bars(n: int = 5) -> pd.DataFrame:
    return pd.DataFrame({
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.5] * n,
    })


class _AlwaysBuy(SignalStrategy):
    name = "always_buy"

    def signal(self, df):
        return {"side": "BUY", "qty": 1, "sl": 99.0, "tp": 103.0}


def test_base_is_abstract():
    with pytest.raises(TypeError):
        SignalStrategy("RELIANCE")  # abstract signal() not implemented


def test_base_normalises_symbol_and_params():
    s = _AlwaysBuy(" reliance ", timeframe="5m", count=50, foo=1)
    assert s.symbol == "RELIANCE"
    assert s.timeframe == "5m"
    assert s.count == 50
    assert s.params == {"foo": 1}


def test_sample_never_trades():
    assert SampleStrategy("RELIANCE").signal(_bars()) is None


def test_sample_handles_short_frame():
    assert SampleStrategy("RELIANCE").signal(_bars(1)) is None
    assert SampleStrategy("RELIANCE").signal(None) is None


def test_check_signal_none_blocks():
    ok, _ = check_signal(None)
    assert ok is False


def test_check_signal_bad_side_blocks():
    ok, _ = check_signal({"side": "HOLD"})
    assert ok is False


def test_check_signal_live_needs_qty_sl_tp():
    ok, reason = check_signal({"side": "BUY"}, live=True)
    assert ok is False
    assert "qty" in reason and "sl" in reason and "tp" in reason


def test_check_signal_live_zero_qty_blocks():
    ok, _ = check_signal({"side": "BUY", "qty": 0, "sl": 1, "tp": 2}, live=True)
    assert ok is False


def test_check_signal_live_full_passes():
    ok, _ = check_signal({"side": "SELL", "qty": 5, "sl": 1, "tp": 2}, live=True)
    assert ok is True


def test_check_signal_paper_needs_only_side():
    ok, _ = check_signal({"side": "BUY"}, live=False)
    assert ok is True
