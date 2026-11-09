"""Parity (decouple item 8): local HRP vs the real Blue_engine hrp_weights.

Runs in the sandbox through conftest.blue (no openalgo needed). Local code must give the same
weights as Blue on random and degenerate covariances (rel 1e-12).
"""
import numpy as np
import pytest

from quant.portfolio.optimizer import hrp_weights


def _random_panel(seed, n_assets, n_obs):
    rng = np.random.default_rng(seed)
    scale = rng.uniform(0.5, 2.0, n_assets)
    return rng.normal(0, 0.01, (n_obs, n_assets)) * scale + rng.normal(0, 0.003, (n_obs, 1))


@pytest.mark.parametrize("seed", range(40))
def test_hrp_matches_blue_on_random_covariances(blue, seed):
    hrp = blue("portfolio.optim.hrp")
    n = 2 + seed % 7
    S = np.cov(_random_panel(seed, n, 200 + seed * 5), rowvar=False, ddof=1)
    want = np.asarray(hrp.hrp_weights(S.tolist()), float)
    assert np.allclose(hrp_weights(S), want, rtol=1e-12, atol=1e-15)


def test_hrp_matches_blue_with_zero_variance_asset(blue):
    hrp = blue("portfolio.optim.hrp")
    S = np.diag([0.01, 0.0, 0.04])
    want = np.asarray(hrp.hrp_weights(S.tolist()), float)
    assert np.allclose(hrp_weights(S), want, rtol=1e-12, atol=1e-15)


def test_hrp_all_zero_covariance_matches_blue_or_refuses(blue):
    hrp = blue("portfolio.optim.hrp")
    S = np.zeros((3, 3))
    want = hrp.hrp_weights(S.tolist())
    if want is None:
        with pytest.raises(ValueError):
            hrp_weights(S)
    else:
        assert np.allclose(hrp_weights(S), np.asarray(want, float), rtol=1e-12, atol=1e-15)


def test_hrp_single_asset_is_one(blue):
    assert hrp_weights(np.array([[0.01]])).tolist() == [1.0]
