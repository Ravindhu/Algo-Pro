"""Regression: Blue HRP matches the Lopez de Prado reference (scipy single linkage).

Bug fixed: quasi_diag pushed the root's children in the wrong order, so the root's
halves were swapped relative to every inner node. Leaf order then differed from the
reference on every random case, which changed the recursive-bisection weights.
Before the fix this test fails on the order check (300/300 mismatches).
"""
import numpy as np
import pytest
from scipy.cluster.hierarchy import linkage, to_tree
from scipy.spatial.distance import squareform

from blue_engine.portfolio.optim import hrp


def _reference_order(corr):
    d = np.sqrt(np.clip(0.5 * (1.0 - np.asarray(corr)), 0.0, None))
    np.fill_diagonal(d, 0.0)
    return list(to_tree(linkage(squareform(d, checks=False), "single")).pre_order())


def _reference_weights(cov):
    cov = np.asarray(cov, float)
    order = _reference_order(cov / np.outer(np.sqrt(np.diag(cov)), np.sqrt(np.diag(cov))))
    w = np.ones(len(order))
    stack = [order]
    while stack:
        items = stack.pop()
        if len(items) <= 1:
            continue
        mid = len(items) // 2
        left, right = items[:mid], items[mid:]

        def cvar(idx):
            ivp = 1.0 / np.diag(cov)[idx]
            ivp = ivp / ivp.sum()
            return float(ivp @ cov[np.ix_(idx, idx)] @ ivp)

        vl, vr = cvar(left), cvar(right)
        alpha = 1.0 - vl / (vl + vr)
        for i in left:
            w[i] *= alpha
        for i in right:
            w[i] *= 1.0 - alpha
        stack += [left, right]
    return w / w.sum()


@pytest.mark.parametrize("seed", range(40))
def test_quasi_diagonal_order_matches_reference(seed):
    rng = np.random.default_rng(seed)
    n = 3 + seed % 6
    R = rng.normal(size=(120, n)) @ rng.normal(size=(n, n)) * 0.1 + rng.normal(size=(120, 1)) * 0.2
    C = np.corrcoef(R, rowvar=False)
    assert hrp.cluster_order(C.tolist()) == _reference_order(C)


@pytest.mark.parametrize("seed", range(20))
def test_hrp_weights_match_reference(seed):
    rng = np.random.default_rng(seed)
    n = 3 + seed % 5
    R = rng.normal(0, 0.01, (250, n)) * rng.uniform(0.5, 2.0, n)
    S = np.cov(R, rowvar=False, ddof=1)
    got = np.asarray(hrp.hrp_weights(S.tolist()))
    assert np.allclose(got, _reference_weights(S), atol=1e-10)
