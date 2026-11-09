"""Percent-of-capital risk limits (owner decision 0031). Hand-checked numbers."""
import pytest

from quant.core.config import RiskLimits


def test_percent_converts_to_rupees_at_load():
    r = RiskLimits.from_mapping({"capital_inr": 1_000_000, "max_daily_loss_pct": 0.005,
                                 "max_strategy_loss_pct": 0.0005, "max_order_notional_pct": 0.01,
                                 "max_open_exposure_pct": 0.02})
    assert r.max_daily_loss == 5000.0
    assert r.max_strategy_loss == 500.0
    assert r.max_order_notional == 10000.0
    assert r.max_open_exposure == 20000.0


def test_no_capital_leaves_rupee_fields_none_so_gate_fails_closed():
    r = RiskLimits.from_mapping({"max_daily_loss_pct": 0.005})
    assert r.max_daily_loss is None
    assert "max_daily_loss" in RiskLimits.from_mapping({}).unset() or RiskLimits().max_daily_loss is None


@pytest.mark.parametrize("key", ["max_daily_loss", "max_strategy_loss", "max_order_notional", "max_open_exposure"])
def test_rupee_key_is_refused(key):
    with pytest.raises(ValueError, match="rupee limits are not allowed"):
        RiskLimits.from_mapping({"capital_inr": 1_000_000, key: 1000})


@pytest.mark.parametrize("bad", [0, -0.1, 1.5, True, float("nan")])
def test_bad_percent_is_refused(bad):
    with pytest.raises(ValueError):
        RiskLimits.from_mapping({"capital_inr": 1_000_000, "max_daily_loss_pct": bad})


@pytest.mark.parametrize("bad", [0, -5, float("inf"), "10 lakh"])
def test_bad_capital_is_refused(bad):
    with pytest.raises(ValueError):
        RiskLimits.from_mapping({"capital_inr": bad, "max_daily_loss_pct": 0.005})
