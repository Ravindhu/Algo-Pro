"""Hand-checked numbers for the stage 3 local ports (no Blue needed)."""
import pytest

from quant.portfolio.portfolio_risk import historical_cvar
from quant.portfolio.rebalance import rebalance_trades
from quant.portfolio.sizing import fractional_kelly, vol_target_scale
from quant.reporting.dashboard import profit_factor


def test_profit_factor_hand_values():
    assert profit_factor(300.0, -100.0) == 3.0
    assert profit_factor(1.0, 0.0) is None          # no losses -> None, not infinity


def test_cvar_hand_values():
    # sorted: -0.05, -0.03, 0.01, 0.02
    # alpha 0.5: quantile at pos 1.5 = -0.01; tail = [-0.05, -0.03]; mean -0.04 -> 0.04
    assert historical_cvar([-0.05, -0.03, 0.01, 0.02], alpha=0.5) == pytest.approx(0.04)
    # alpha 0.25: quantile at pos 0.75 = -0.035; tail = [-0.05] -> 0.05
    assert historical_cvar([-0.05, -0.03, 0.01, 0.02], alpha=0.25) == pytest.approx(0.05)


def test_cvar_drops_non_finite_values():
    assert historical_cvar([-0.05, None, float("nan"), -0.03, 0.01, 0.02], alpha=0.5) == pytest.approx(0.04)


def test_rebalance_only_names_outside_band_trade():
    current = {"A": 0.50, "B": 0.50, "C": 0.10}
    target = {"A": 0.47, "B": 0.50, "C": 0.13}
    # drifts: A 0.03, B 0, C 0.03; band 0.02 -> A and C trade; values are target - current
    got = rebalance_trades(current, target, band=0.02)
    assert set(got) == {"A", "C"}
    assert got["A"] == pytest.approx(-0.03)
    assert got["C"] == pytest.approx(0.03)


def test_fractional_kelly_hand_values():
    # full Kelly = 0.01 / 0.2^2 = 0.25; quarter Kelly = 0.0625 (under the 0.10 cap)
    assert fractional_kelly(0.01, 0.2) == pytest.approx(0.0625)
    # full Kelly = 0.2 / 0.04 = 5; quarter = 1.25 -> capped at 0.10
    assert fractional_kelly(0.2, 0.2) == pytest.approx(0.10)
    assert fractional_kelly(0.01, 0.0) == 0.0       # sigma 0 -> no position
    assert fractional_kelly(-0.01, 0.2) == 0.0      # negative edge -> no position


def test_vol_target_hand_values():
    assert vol_target_scale(0.20, 0.10, max_leverage=1.0) == pytest.approx(0.5)   # de-risk
    assert vol_target_scale(0.05, 0.10, max_leverage=1.0) == pytest.approx(1.0)   # clipped at max
    assert vol_target_scale(0.04, 0.30, max_leverage=2.0) == pytest.approx(2.0)   # clipped at max
    assert vol_target_scale(0.0, 0.10) == 0.0                                    # no vol -> 0
