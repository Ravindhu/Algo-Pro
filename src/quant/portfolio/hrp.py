"""Hierarchical Risk Parity (Lopez de Prado 2016). Local, no Blue import.

Flow stage: HRP optimizer
Status: PHASE 3 IMPLEMENTED (2026-11-11, decoupling stage 3 item 8)

Algorithm (matches the reference in test_phase0_hrp_cov.py, which this file replaces):
  1. Correlation from covariance, clipped to [-1, 1] (float noise can exceed 1).
  2. Distance d = sqrt(0.5 (1 - rho)); zero diagonal.
  3. Single-linkage hierarchical clustering -> quasi-diagonal leaf order.
  4. Recursive bisection of that order, splitting cluster risk (inverse-variance
     allocation inside each cluster) at each split.

Edge cases kept from the Blue-era wrapper:
  - n == 1 returns weight 1.0 without touching the tree.
  - All-zero covariance returns equal weights (the correlation helper handles
    zero-variance assets without NaN).
  - No valid clustering (None) raises ValueError — fail closed, never pass None on.
"""
from __future__ import annotations

import numpy as np
from scipy.cluster.hierarchy import linkage, to_tree
from scipy.spatial.distance import squareform

__all__ = ["hrp_weights"]


def _cluster_var(cov: np.ndarray, items: list) -> float:
    """Inverse-variance allocation inside one cluster, then its variance."""
    ivp = 1.0 / np.diag(cov)[items]
    ivp = ivp / ivp.sum()
    return float(ivp @ cov[np.ix_(items, items)] @ ivp)


def hrp_weights(cov) -> np.ndarray:
    """Weights summing to 1, all positive, from one covariance matrix."""
    cov = np.asarray(cov, float)
    n = cov.shape[0]
    if n == 1:
        return np.array([1.0])

    sd = np.sqrt(np.diag(cov))
    corr = np.clip(cov / np.outer(sd, sd), -1.0, 1.0)
    dist = np.sqrt(0.5 * (1.0 - corr))
    np.fill_diagonal(dist, 0.0)
    try:
        order = to_tree(linkage(squareform(dist, checks=False), method="single")).pre_order()
    except Exception as exc:
        raise ValueError(f"HRP: no valid clustering from covariance ({exc!r})") from exc
    if not order:
        raise ValueError("HRP: clustering returned an empty order")

    weights = np.ones(n)
    stack = [list(order)]
    while stack:
        items = stack.pop()
        if len(items) <= 1:
            continue
        mid = len(items) // 2
        left, right = items[:mid], items[mid:]
        vl, vr = _cluster_var(cov, left), _cluster_var(cov, right)
        total = vl + vr
        alpha = 0.5 if total <= 0 else 1.0 - vl / total
        for i in left:
            weights[i] *= alpha
        for i in right:
            weights[i] *= 1.0 - alpha
        stack.append(left)
        stack.append(right)
    weights /= weights.sum()
    if not np.all(np.isfinite(weights)) or weights.sum() <= 0:
        raise ValueError("HRP: weights are not usable (non-finite or zero sum)")
    return weights
