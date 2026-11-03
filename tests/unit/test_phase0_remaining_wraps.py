"""Phase 0: wraps decided for the remaining undecided modules (decision record docs/decisions/0005).

- models/factors.py::momentum_12_1 equals Blue ml.features.engineering.momentum(window=231, shift=21).
- reporting/dashboard.py::profit_factor follows Blue backtest.metrics.profit_factor (None when no losses).
Blue-dependent checks skip when the OpenAlgo SDK is missing.
"""
import numpy as np
import pandas as pd
import pytest

from quant.core import blue_bridge
from quant.core.blue_bridge import BlueEngineUnavailable


@pytest.fixture
def blue_eng():
    try:
        return blue_bridge.load("ml.features.engineering")
    except BlueEngineUnavailable as exc:
        pytest.skip(f"Blue_engine not importable here: {exc}")


def test_momentum_12_1_matches_blue(blue_eng):
    from quant.models.factors import momentum_12_1

    rng = np.random.default_rng(0)
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.01, (400, 4)), axis=0)),
                         columns=list("ABCD"))
    ours = momentum_12_1(close)
    for c in close.columns:
        vals = blue_eng.momentum(close[c].tolist(), 231, shift=21)
        want = np.array([np.nan if v is None else v for v in vals], float)
        assert np.array_equal(np.isnan(want), ours[c].isna().to_numpy())
        assert np.allclose(want, ours[c].to_numpy(), equal_nan=True, atol=1e-12)


def test_momentum_12_1_uses_only_past_bars():
    from quant.models.factors import momentum_12_1

    close = pd.DataFrame({"A": np.linspace(100, 200, 300)})
    base = momentum_12_1(close)["A"]
    shocked = close.copy()
    shocked.iloc[-1, 0] = 1e9  # changing the last bar must not change earlier values
    assert np.allclose(momentum_12_1(shocked)["A"].iloc[:-1], base.iloc[:-1], equal_nan=True)


def test_profit_factor_none_without_losses_and_matches_ratio():
    from quant.reporting.dashboard import profit_factor

    assert profit_factor(100.0, 0.0) is None
    assert profit_factor(150.0, -50.0) == pytest.approx(3.0)


def test_profit_factor_agrees_with_blue():
    from quant.reporting.dashboard import profit_factor

    try:
        m = blue_bridge.load("backtest.metrics")
    except BlueEngineUnavailable as exc:
        pytest.skip(f"Blue_engine not importable here: {exc}")
    for gp, gl in [(100.0, 0.0), (0.0, -10.0), (75.0, -25.0)]:
        assert profit_factor(gp, gl) == m.profit_factor(gp, gl)
