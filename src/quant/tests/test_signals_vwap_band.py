"""Flow stage: Signal and conviction (VWAP band indicator).
Status: TESTS WRITTEN; run result is in the commit message.
"""
import math

import pytest

from quant.indicators.vwap_band import (
    BandConfig,
    BuyerInitiatedDailyVWAP,
    compute_band_fraction,
    compute_ceiling,
    compute_floor,
    compute_target_pov_rate,
)


def _cfg(action="BUY", passthrough="part_pct"):
    # vwap 1000 -> ceiling 1005 (0.5% above), floor 970 (3% below)
    return BandConfig(vwap=1000.0, vwap_ceiling_pct=0.5, vwap_floor_pct=3.0,
                      min_pov_pct=5.0, max_pov_pct=25.0, part_pct=10.0,
                      passthrough_floor=passthrough, action=action)


def test_anchors():
    assert compute_ceiling(1000.0, 0.5) == pytest.approx(1005.0)
    assert compute_floor(1000.0, 3.0) == pytest.approx(970.0)


def test_buy_halt_at_or_above_ceiling():
    d = compute_target_pov_rate(ltp=1010.0, config=_cfg("BUY"))
    assert d.reason == "halt" and d.rate == 0.0


def test_buy_passthrough_below_floor():
    d = compute_target_pov_rate(ltp=960.0, config=_cfg("BUY"))
    assert d.reason == "passthrough" and d.rate == pytest.approx(10.0)


def test_buy_interpolated_midpoint_is_15():
    d = compute_target_pov_rate(ltp=987.5, config=_cfg("BUY"))
    assert d.reason == "interpolated"
    assert d.f_band == pytest.approx(0.5)
    assert d.rate == pytest.approx(15.0)


def test_sell_flips_halt_and_passthrough():
    assert compute_target_pov_rate(ltp=960.0, config=_cfg("SELL")).reason == "halt"
    assert compute_target_pov_rate(ltp=1010.0, config=_cfg("SELL")).reason == "passthrough"


def test_passthrough_modes():
    assert compute_target_pov_rate(ltp=960.0, config=_cfg(passthrough="max_pov")).rate == 25.0
    assert compute_target_pov_rate(ltp=960.0, config=_cfg(passthrough="min_pov")).rate == 5.0


def test_nan_ltp_is_unavailable():
    d = compute_target_pov_rate(ltp=float("nan"), config=_cfg())
    assert d.reason == "ltp_unavailable" and d.rate == pytest.approx(10.0)


def test_zero_vwap_is_unavailable():
    cfg = BandConfig(vwap=0.0)
    d = compute_target_pov_rate(ltp=100.0, config=cfg)
    assert d.reason == "vwap_unavailable"


def test_band_fraction_degenerate_is_zero():
    assert compute_band_fraction(100.0, 90.0, 95.0) == 0.0


def test_rate_never_exceeds_max():
    for ltp in (900.0, 975.0, 1000.0, 1004.9):
        d = compute_target_pov_rate(ltp=ltp, config=_cfg("BUY"))
        assert 0.0 <= d.rate <= 25.0
        assert math.isfinite(d.rate)


def test_buyer_vwap_counts_only_buyer_initiated():
    acc = BuyerInitiatedDailyVWAP()
    acc.reset(level=100.0, action="BUY", start_bar=0)
    assert acc.add_daily_candle(price=100.0, volume=10.0, ltp=101.0, ask=100.5) is True
    assert acc.add_daily_candle(price=200.0, volume=10.0, ltp=99.0, ask=100.5) is False
    assert acc.vwap == pytest.approx(100.0)
    assert acc.count == 1


def test_buyer_vwap_fails_closed_without_classification():
    acc = BuyerInitiatedDailyVWAP()
    acc.reset(start_bar=0)
    assert acc.add_daily_candle(price=100.0, volume=10.0) is False
    assert acc.vwap is None


def test_buyer_vwap_flag_mode():
    acc = BuyerInitiatedDailyVWAP()
    acc.reset(start_bar=0)
    acc.add_daily_candle(price=100.0, volume=10.0, buyer_initiated=True)
    acc.add_daily_candle(price=110.0, volume=30.0, buyer_initiated=True)
    assert acc.vwap == pytest.approx((1000.0 + 3300.0) / 40.0)


def test_deviation_pct_and_bars_since_touch():
    acc = BuyerInitiatedDailyVWAP()
    acc.reset(start_bar=5)
    acc.add_daily_candle(price=100.0, volume=1.0, buyer_initiated=True)
    assert acc.deviation_pct(101.0) == pytest.approx(1.0)
    assert acc.bars_since_touch(8) == 3
    assert BuyerInitiatedDailyVWAP().bars_since_touch(8) is None
