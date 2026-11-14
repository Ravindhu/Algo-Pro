"""Tests for risk models (covariance, shrinkage, weights)."""
import numpy as np
import pandas as pd
import pytest

from portfolio.risk_models import (LedoitWolfShrinkage, RiskMetrics, RiskModelConfig,
                                   estimate_covariance, ledoit_wolf_shrinkage)


@pytest.fixture
def returns_3d():
    """Three correlated return series with known structure."""
    rng = np.random.default_rng(42)
    common = rng.normal(0, 0.01, 500)
    x = common + rng.normal(0, 0.005, 500)
    y = common + rng.normal(0, 0.005, 500)
    z = rng.normal(0, 0.01, 500)
    return pd.DataFrame({"X": x, "Y": y, "Z": z})


def test_covariance_shape_and_psd(returns_3d):
    cov = estimate_covariance(returns_3d)
    assert cov.shape == (3, 3)
    eig = np.linalg.eigvalsh(cov.values)
    assert (eig > -1e-10).all()


def test_ledoit_wolf_shrinks_toward_target(returns_3d):
    S = returns_3d.cov().values
    S_shrunk = ledoit_wolf_shrinkage(returns_3d)
    scale = np.linalg.norm(S) + 1e-12
    assert np.linalg.norm(S_shrunk - S) / scale < 0.5
    assert np.allclose(np.diag(S_shrunk), np.diag(S))


def test_ledoit_wolf_class_matches_function(returns_3d):
    cfg = RiskModelConfig(shrinkage_method="ledoit_wolf")
    model = LedoitWolfShrinkage(cfg)
    a = model.fit(returns_3d).cov_
    b = ledoit_wolf_shrinkage(returns_3d)
    assert np.allclose(a, b)


def test_risk_metrics_basic(returns_3d):
    cov = estimate_covariance(returns_3d)
    metrics = RiskMetrics(cov)
    w = np.array([0.5, 0.5, 0.0])
    vol = metrics.portfolio_volatility(w)
    assert vol >= 0
    contrib = metrics.mcr(w)
    total = float(np.sum(w * contrib))
    assert total == pytest.approx(vol, rel=1e-6)


def test_mrc_sums_to_vol(returns_3d):
    cov = estimate_covariance(returns_3d)
    metrics = RiskMetrics(cov)
    w = np.array([0.2, 0.3, 0.5])
    vol = metrics.portfolio_volatility(w)
    assert np.sum(w * metrics.mcr(w)) == pytest.approx(vol, rel=1e-8)


def test_risk_budget_contributions_equal(returns_3d):
    from portfolio.risk_models import solve_risk_budget_weights
    cov = estimate_covariance(returns_3d)
    w = solve_risk_budget_weights(cov, np.ones(3) / 3)
    metrics = RiskMetrics(cov)
    contrib = w * metrics.mcr(w)
    share = contrib / contrib.sum()
    assert np.allclose(share, np.ones(3) / 3, atol=1e-4)


def test_weights_sum_to_one(returns_3d):
    from portfolio.risk_models import solve_risk_budget_weights
    cov = estimate_covariance(returns_3d)
    w = solve_risk_budget_weights(cov)
    assert w.sum() == pytest.approx(1.0, abs=1e-6)
    assert (w >= 0).all()
