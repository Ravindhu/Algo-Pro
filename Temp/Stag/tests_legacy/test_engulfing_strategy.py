"""Flow stage: Strategy (engulfing with fib entries).
Status: TESTS WRITTEN; run result is in the commit message.

Checks the ported engulfing strategy and the Hilega-Milega direction helper.
"""
import numpy as np
import pandas as pd
import pytest

from quant.indicators.hm import hm_direction
from quant.strategies.engulfing.engulfing import Engulfing


def _frame(seed: int, n: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    opens = 100 + rng.normal(0, 1.0, n).cumsum()
    closes = opens + rng.normal(0, 1.0, n)
    highs = np.maximum(opens, closes) + rng.uniform(0, 0.6, n)
    lows = np.minimum(opens, closes) - rng.uniform(0, 0.6, n)
    idx = pd.date_range("2026-01-01", periods=n, freq="h")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes}, index=idx)


def test_constructor_rejects_bad_fib():
    with pytest.raises(ValueError):
        Engulfing("INFY", fib_pct=1.2)


def test_constructor_rejects_bad_wait():
    with pytest.raises(ValueError):
        Engulfing("INFY", max_wait=0)


def test_short_frame_returns_none():
    assert Engulfing("INFY").signal(_frame(1, n=10)) is None


def test_flat_frame_returns_none():
    n = 60
    idx = pd.date_range("2026-01-01", periods=n, freq="h")
    flat = pd.DataFrame({"open": [100.0] * n, "high": [100.0] * n,
                         "low": [100.0] * n, "close": [100.0] * n}, index=idx)
    assert Engulfing("INFY").signal(flat) is None


def test_hm_direction_warmup_is_zero():
    closes = [float(x) for x in np.linspace(100, 110, 30)]
    d = hm_direction(closes, rsi_period=9, ema_period=3)
    assert set(d[:9]) == {0}
    assert set(np.unique(d)) <= {-1, 0, 1}


def test_signal_fields_are_consistent_when_found():
    strat = Engulfing("INFY", max_wait=5)
    found = None
    for seed in range(500):
        df = _frame(seed)
        sig = strat.signal(df)
        if sig is not None:
            found = (df, sig)
            break
    if found is None:
        pytest.skip("no engulfing setup in 500 seeded walks; candle logic is covered elsewhere")
    df, sig = found
    assert sig["side"] in ("BUY", "SELL")
    assert sig["sl_dist"] > 0 and sig["tp_dist"] > 0
    assert sig["tp_dist"] == pytest.approx(3.0 * sig["atr"])
    assert sig["sl_dist"] == pytest.approx(1.5 * sig["atr"])
    assert sig["time"] == str(df.index[-2])
