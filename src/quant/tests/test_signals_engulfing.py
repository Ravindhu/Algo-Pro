"""Flow stage: Signal and conviction (engulfing candle logic).
Status: TESTS WRITTEN; run result is in the commit message.

Checks the engulfing candle rules, parity between the DataFrame and single-candle forms, and that the
sell-failure strategies use the engulfing check while the plain OCL file does not.
"""
import numpy as np
import pandas as pd
import pytest

from quant.indicators.engulfing import (
    body_ratio,
    engulfing,
    engulfing_bear_pair,
    engulfing_bull_pair,
    fib_entry_level,
)
from quant.strategies.ocl.ocl import OclSignal
from quant.strategies.ocl.ocl_sell_fail_top import OclSellFailTopSignal


def _c(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def test_bullish_engulfing_with_big_bodies():
    prev = _c(10.0, 10.2, 8.9, 9.0)       # red, body 1.0 of range 1.3 (>50%)
    cur = _c(8.8, 10.5, 8.7, 10.3)        # green, engulfs prev, body 1.5 of 1.8
    assert engulfing_bull_pair(prev, cur) is True


def test_bearish_engulfing_with_big_bodies():
    prev = _c(9.0, 10.1, 8.9, 10.0)       # green, body 1.0 of 1.2
    cur = _c(10.2, 10.3, 8.8, 8.9)        # red, engulfs prev
    assert engulfing_bear_pair(prev, cur) is True


def test_small_body_rejected_by_default_filter():
    prev = _c(10.0, 11.0, 9.0, 9.0)       # red, body 1.0 of range 2.0 = 50%, not >50%
    cur = _c(8.8, 10.5, 8.7, 10.3)
    assert engulfing_bull_pair(prev, cur) is False
    assert engulfing_bull_pair(prev, cur, min_body_pct=0.0) is True


def test_non_engulfing_is_false():
    prev = _c(9.0, 10.1, 8.9, 10.0)       # green
    cur = _c(9.5, 9.6, 9.0, 9.1)          # red but does not cover prev body
    assert engulfing_bear_pair(prev, cur, min_body_pct=0.0) is False


def test_fib_entry_levels():
    assert fib_entry_level(110.0, 100.0, 1, 0.886) == pytest.approx(110.0 - 0.886 * 10.0)
    assert fib_entry_level(110.0, 100.0, -1, 0.886) == pytest.approx(100.0 + 0.886 * 10.0)


def test_body_ratio_zero_range_is_zero():
    df = pd.DataFrame({"open": [5.0], "high": [5.0], "low": [5.0], "close": [5.0]})
    assert body_ratio(df).iloc[0] == 0.0


def test_dataframe_and_pair_forms_agree_on_random_bars():
    rng = np.random.default_rng(7)
    n = 300
    opens = 100 + rng.normal(0, 1.5, n).cumsum()
    closes = opens + rng.normal(0, 1.2, n)
    highs = np.maximum(opens, closes) + rng.uniform(0, 0.8, n)
    lows = np.minimum(opens, closes) - rng.uniform(0, 0.8, n)
    df = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes})
    for min_body in (0.0, 0.5):
        series = engulfing(df, min_body).to_numpy()
        for i in range(1, n):
            prev = df.iloc[i - 1].to_dict()
            cur = df.iloc[i].to_dict()
            bull = engulfing_bull_pair(prev, cur, min_body)
            bear = engulfing_bear_pair(prev, cur, min_body)
            expected = 1 if bull else (-1 if bear else 0)
            assert series[i] == expected, (i, min_body)


class _FailingClient:
    def history(self, **kwargs):
        return {"status": "error", "message": "test"}


def _cfg():
    return {"symbol": "INFY", "exchange": "NSE", "poll_seconds": 0, "setup_mode": "SELL_FAILURE_TOP",
            "htf_interval": "15m", "ltf_interval": "5m", "quantity": 1}


def test_sell_failure_top_uses_engulfing_check():
    s = OclSellFailTopSignal(_FailingClient(), _cfg())
    prev = _c(9.0, 10.1, 8.9, 10.0)       # green C2
    engulf = _c(10.2, 10.3, 8.8, 8.9)     # red C3 engulfs C2
    not_engulf = _c(9.5, 9.6, 9.0, 9.1)   # red C3 that does not engulf
    assert s._is_sell_engulfing(prev, engulf) is True
    assert s._is_sell_engulfing(prev, not_engulf) is False


def test_plain_ocl_file_keeps_original_logic():
    s = OclSignal(_FailingClient(), {"symbol": "INFY", "exchange": "NSE", "poll_seconds": 0,
                                     "htf_interval": "15m", "ltf_interval": "5m", "quantity": 1})
    assert not hasattr(s, "_is_sell_engulfing")
    assert hasattr(s, "_is_sell_sequence")
