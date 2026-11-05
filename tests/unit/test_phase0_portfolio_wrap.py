"""Phase 0: Kelly, vol-target and CVaR formulas delegated to Blue_engine.

Needs the OpenAlgo SDK (Blue_engine imports it). Skips with the reason when absent.
Equivalence with the pre-wrap formulas was checked on 180 Kelly, 24 vol-target and
3 CVaR cases (see docs/audits/PHASE0_REUSE_AUDIT.md, step 7).
"""
import itertools

import numpy as np
import pytest

from quant.core import blue_bridge
from quant.core.blue_bridge import BlueEngineUnavailable


@pytest.fixture(autouse=True)
def _need_blue():
    try:
        blue_bridge.load("portfolio.sizing.kelly")
    except BlueEngineUnavailable as exc:
        pytest.skip(f"Blue_engine not importable here: {exc}")


def _kelly_formula(mu, sigma, fraction, cap):
    if sigma <= 0 or fraction <= 0 or cap <= 0:
        return 0.0
    return min(cap, max(0.0, fraction * mu / sigma ** 2))


def test_fractional_kelly_matches_formula_grid():
    from quant.portfolio.sizing import fractional_kelly

    for mu, sigma, f, c in itertools.product(
            [-0.02, 0.0, 0.001, 0.01, 0.05], [0.005, 0.01, 0.03, 0.2], [0.25, 0.5, 1.0], [0.05, 0.1, 1.0]):
        assert fractional_kelly(mu, sigma, f, c) == pytest.approx(_kelly_formula(mu, sigma, f, c), abs=1e-12)


def test_fractional_kelly_zero_sigma_is_zero():
    from quant.portfolio.sizing import fractional_kelly

    assert fractional_kelly(0.05, 0.0) == 0.0


def test_vol_target_matches_formula_grid():
    from quant.portfolio.sizing import vol_target_scale

    for fv, tv in itertools.product([0.01, 0.05, 0.1, 0.2, 0.4, 1.0], [0.05, 0.1, 0.15, 0.3]):
        assert vol_target_scale(fv, tv, 1.0) == pytest.approx(min(1.0, tv / fv), abs=1e-12)


def test_vol_target_zero_vol_is_zero_not_blue_default():
    from quant.portfolio.sizing import vol_target_scale

    assert vol_target_scale(0.0, 0.1, 1.0) == 0.0


def test_historical_cvar_matches_tail_formula():
    from quant.portfolio.portfolio_risk import historical_cvar

    rng = np.random.default_rng(5)
    pnl = rng.normal(0, 0.01, 500)
    for alpha in [0.05, 0.01, 0.1]:
        x = np.sort(pnl)
        k = max(1, int(np.ceil(alpha * len(x))))
        assert historical_cvar(pnl, alpha=alpha) == pytest.approx(-x[:k].mean(), abs=1e-12)


def test_kelly_delegates_to_blue(monkeypatch):
    kl = blue_bridge.load("portfolio.sizing.kelly")
    calls = []
    real = kl.kelly_leverage
    monkeypatch.setattr(kl, "kelly_leverage", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    from quant.portfolio.sizing import fractional_kelly

    fractional_kelly(0.01, 0.2)
    assert calls and calls[0] == (0.01, 0.0, 0.2)


def test_max_drawdown_matches_blue_on_random_paths():
    from quant.monitor.live_monitor import max_drawdown

    metrics = blue_bridge.load("ml.evaluation.metrics")
    rng = np.random.default_rng(9)
    for _ in range(100):
        eq = list(100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, rng.integers(2, 300)))))
        assert max_drawdown(eq) == pytest.approx(metrics.max_drawdown(eq) or 0.0, abs=1e-12)


def test_max_drawdown_never_nan_on_zero_start():
    """Regression: the old local version returned NaN for a path starting at 0."""
    from quant.monitor.live_monitor import max_drawdown

    value = max_drawdown([0.0, 5.0])
    assert value == 0.0 and value == value  # not NaN


def test_max_drawdown_empty_and_single_are_zero():
    from quant.monitor.live_monitor import max_drawdown

    assert max_drawdown([]) == 0.0
    assert max_drawdown([100.0]) == 0.0
