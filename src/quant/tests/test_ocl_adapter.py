"""OCL adapter: converts an OCL detect() intent into the bot's signal contract. No network."""
import pandas as pd
import pytest

from quant.strategies.ocl.adapter import BarsHistoryClient, OclStrategyAdapter, OCL_STRATEGIES


def _df(n=10):
    return pd.DataFrame({"open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
                         "close": [100.0] * n, "volume": [1000.0] * n})


class FakeOcl:
    """Stands in for an OCL class: records the client it was given, returns a fixed intent."""

    def __init__(self, client, config):
        self.client = client
        self.config = config
        self.intent = {"action": "BUY", "quantity": 10, "entry_price": 100.0,
                       "stop_loss": 98.0, "target": 106.0, "atr": 1.5, "time": "T1"}

    def detect(self):
        return self.intent


def test_history_client_serves_bars_without_network():
    df = _df()
    resp = BarsHistoryClient(df).history(symbol="INFY", interval="5m")
    assert resp["status"] == "success" and len(resp["data"]) == len(df)


def test_history_client_empty_bars_is_error():
    assert BarsHistoryClient(pd.DataFrame()).history()["status"] == "error"


def test_adapter_converts_intent_to_signal_contract():
    a = OclStrategyAdapter("ocl", FakeOcl, {"symbol": "INFY"})
    sig = a.signal(_df())
    assert sig["side"] == "BUY"
    assert sig["entry"] == pytest.approx(100.0)
    assert sig["sl_dist"] == pytest.approx(2.0)
    assert sig["tp_dist"] == pytest.approx(6.0)
    assert sig["atr"] == pytest.approx(1.5)
    assert sig["time"] == "T1"


def test_adapter_returns_sell_unchanged_long_only_is_enforced_downstream():
    class SellOcl(FakeOcl):
        def __init__(self, client, config):
            super().__init__(client, config)
            self.intent = dict(self.intent, action="SELL", stop_loss=102.0, target=94.0)

    sig = OclStrategyAdapter("ocl", SellOcl).signal(_df())
    assert sig["side"] == "SELL"  # the bot refuses it at scan time (long_only_refused)


def test_adapter_returns_none_on_bad_intent():
    class BadOcl(FakeOcl):
        def __init__(self, client, config):
            super().__init__(client, config)
            self.intent = {"action": "BUY", "stop_loss": None, "target": None}

    assert OclStrategyAdapter("ocl", BadOcl).signal(_df()) is None


def test_adapter_swallows_strategy_errors():
    class Boom(FakeOcl):
        def detect(self):
            raise RuntimeError("boom")

    assert OclStrategyAdapter("ocl", Boom).signal(_df()) is None


def test_adapter_needs_enough_bars():
    assert OclStrategyAdapter("ocl", FakeOcl).signal(_df(3)) is None


def test_registry_lists_three_ocl_strategies():
    assert set(OCL_STRATEGIES()) == {"ocl", "ocl_sell_fail_top", "ocl_sell_fail_bottom"}
