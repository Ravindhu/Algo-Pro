"""Flow stage: Strategy (signal indicators and patterns).
Status: TESTS WRITTEN; run result is in the commit message.

Tests for the ported indicator math (indicators.py) and pattern helpers (pattern.py).
"""
import pytest

from quant.strategies.signals.indicators import atr, ema, hilega_milega_rsi, wma
from quant.strategies.signals.pattern import (
    action_for_pattern,
    calculate_trigger_level,
    extract_ohlc,
    validate_pattern,
)


def test_rsi_all_up_is_100():
    closes = [float(i) for i in range(1, 20)]
    out = hilega_milega_rsi(closes, rsi_period=9)
    assert out[:9] == [None] * 9
    assert out[-1] == pytest.approx(100.0)


def test_rsi_flat_is_50():
    out = hilega_milega_rsi([10.0] * 15, rsi_period=5)
    assert out[-1] == pytest.approx(50.0)


def test_rsi_rejects_none():
    with pytest.raises(ValueError):
        hilega_milega_rsi([1.0, None, 2.0], rsi_period=1)


def test_ema_warmup_then_value():
    out = ema([1.0, 2.0, 3.0, 4.0], period=3)
    assert out[0] is None and out[1] is None
    assert out[2] is not None


def test_ema_skips_none_inputs():
    out = ema([None, 1.0, 2.0, 3.0], period=2)
    assert out[0] is None


def test_atr_length_mismatch_raises():
    with pytest.raises(ValueError):
        atr([1.0, 2.0], [1.0], [1.0, 2.0], period=2)


def test_atr_constant_range():
    highs = [11.0] * 6
    lows = [9.0] * 6
    closes = [10.0] * 6
    out = atr(highs, lows, closes, period=3)
    assert out[2] == pytest.approx(2.0)


def test_wma_weights_recent_more():
    out = wma([1.0, 2.0, 3.0], period=3)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx((1 * 1 + 2 * 2 + 3 * 3) / 6.0)


def test_extract_ohlc_bad_input_zeros():
    assert extract_ohlc("not a candle") == {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0}


def test_rrg_validates_red_red_green():
    red = {"open": 10.0, "high": 10.5, "low": 8.0, "close": 9.0}
    grn = {"open": 9.0, "high": 11.0, "low": 8.5, "close": 10.5}
    assert validate_pattern(red, red, grn, "RRG") is True
    assert validate_pattern(grn, red, grn, "RRG") is False


def test_unknown_pattern_rejected():
    red = {"open": 10.0, "high": 10.5, "low": 8.0, "close": 9.0}
    assert validate_pattern(red, red, red, "XYZ") is False


def test_trigger_level_rrg_is_min_body_low():
    c2 = {"open": 10.0, "close": 9.0}
    c3 = {"open": 9.5, "close": 10.5}
    assert calculate_trigger_level(c2, c3, "RRG") == pytest.approx(9.0)


def test_trigger_level_unknown_is_none():
    assert calculate_trigger_level({"open": 1, "close": 1}, {"open": 1, "close": 1}, "X") is None


def test_action_mapping():
    assert action_for_pattern("RRG") == "BUY"
    assert action_for_pattern("GRR") == "SELL"
    assert action_for_pattern("") == "BUY"
