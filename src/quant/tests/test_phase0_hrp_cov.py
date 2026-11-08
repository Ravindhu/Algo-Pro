"""Phase 0: HRP is kept local and checked against an independent reference; shrinkage is wrapped.

Decision record: docs/decisions/0003-hrp-and-shrinkage.md.

- HRP (src/quant/portfolio/optimizer.py): our output equals a reference implementation of
  Lopez de Prado (2016) on random covariances. Blue's HRP differs from the reference on
  the same input, so it is NOT wrapped.
- Shrinkage (src/quant/portfolio/covariance.py): wrapped on Blue ledoit_wolf_cov. Needs the
  OpenAlgo SDK; the Blue-agreement test skips without it.
"""
import numpy as np
import pytest
from scipy.cluster.hierarchy import linkage, to_tree
from scipy.spatial.distance import squareform

from quant.portfolio.optimizer import hrp_weights
from quant.portfolio.covariance import shrink_cov


def reference_hrp(cov):
    """Lopez de Prado HRP: single-linkage quasi-diagonal order, then recursive bisection."""
    cov = np.asarray(cov, float)
    sd = np.sqrt(np.diag(cov))
    corr = np.clip(cov / np.outer(sd, sd), -1.0, 1.0)  # float noise can exceed 1
    dist = np.sqrt(0.5 * (1.0 - corr))
    np.fill_diagonal(dist, 0.0)
    order = to_tree(linkage(squareform(dist, checks=False), method="single")).pre_order()

    def cluster_var(items):
        ivp = 1.0 / np.diag(cov)[items]
        ivp = ivp / ivp.sum()
        return float(ivp @ cov[np.ix_(items, items)] @ ivp)

    w = np.ones(len(order))
    stack = [order]
    while stack:
        items = stack.pop()
        if len(items) <= 1:
            continue
        mid = len(items) // 2
        left, right = items[:mid], items[mid:]
        vl, vr = cluster_var(left), cluster_var(right)
        alpha = 1.0 - vl / (vl + vr)
        for i in left:
            w[i] *= alpha
        for i in right:
            w[i] *= 1.0 - alpha
        stack += [left, right]
    return w / w.sum()


def _random_panel(seed, n_assets, n_obs):
    rng = np.random.default_rng(seed)
    scale = rng.uniform(0.5, 2.0, n_assets)
    return rng.normal(0, 0.01, (n_obs, n_assets)) * scale + rng.normal(0, 0.003, (n_obs, 1))


@pytest.mark.parametrize("seed", range(25))
def test_hrp_matches_reference_on_random_covariances(seed):
    n = 3 + seed % 6
    S = np.cov(_random_panel(seed, n, 250), rowvar=False, ddof=1)
    assert np.allclose(hrp_weights(S), reference_hrp(S), atol=1e-12)


def test_hrp_weights_are_long_only_and_sum_to_one():
    S = np.cov(_random_panel(99, 6, 300), rowvar=False, ddof=1)
    w = hrp_weights(S)
    assert np.all(w > 0) and abs(w.sum() - 1.0) < 1e-12


def test_shrink_at_zero_is_the_sample_covariance():
    R = _random_panel(7, 5, 300)
    assert np.allclose(shrink_cov(R, delta=0.0), np.cov(R, rowvar=False, ddof=1), atol=1e-14)


def test_shrink_rejects_out_of_range_delta():
    with pytest.raises(ValueError):
        shrink_cov(_random_panel(1, 3, 50), delta=1.5)


def test_shrink_agrees_with_blue_ledoit_wolf():
    from quant.core import blue_bridge
    from quant.core.blue_bridge import BlueEngineUnavailable

    try:
        cov_mod = blue_bridge.load("portfolio.risk.covariance")
    except BlueEngineUnavailable as exc:
        pytest.skip(f"Blue_engine not importable here: {exc}")
    worst = 0.0
    for seed in range(40):
        R = _random_panel(seed, 2 + seed % 6, 60 + seed * 7)
        for d in (0.0, 0.25, 0.5, 1.0):
            out, _ = cov_mod.ledoit_wolf_cov(R.T.tolist(), annualise=False, shrink=d)
            worst = max(worst, float(np.abs(shrink_cov(R, delta=d) - np.array(out)).max()))
    assert worst < 1e-10
