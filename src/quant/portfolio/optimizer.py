"""Hierarchical Risk Parity (Lopez de Prado). Weights from the cluster tree, no matrix inversion.

Flow stage: HRP optimizer
Status: PHASE 2 IMPLEMENTED (stage 3 port 2026-11-09: HRP is local; no Blue import)
Ported from: Blue_engine/portfolio/optim/hrp.py (hrp_weights, cluster_order, quasi_diag,
_single_linkage, _corr_dist, _cluster_var, _recursive_bisection) and
Blue_engine/portfolio/risk/covariance.py (vols_from_cov, corr_from_cov).
Parity: tests/parity/test_hrp_parity.py (runs wherever Blue loads).
Independent reference (scipy): src/quant/tests/test_phase0_hrp_cov.py.

WHAT THIS FILE KEEPS
--------------------
- The n == 1 shortcut (weight 1.0).
- Fail-closed policy: if the algorithm returns None (no weights), this raises ValueError.
- Zero-variance assets: inverse-variance falls back to equal weights inside a cluster,
  with a 1e-18 variance floor (same as Blue).
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np


def _vols_from_cov(cov: Sequence[Sequence[float]]) -> List[float]:
    """Per-asset vols (sqrt of the diagonal)."""
    return [math.sqrt(max(float(cov[i][i]), 0.0)) for i in range(len(cov))]


def _corr_from_cov(cov: Sequence[Sequence[float]]) -> List[List[float]]:
    """Correlation matrix from a covariance matrix (1 on the diagonal, 0 if a vol is zero)."""
    n = len(cov)
    vols = _vols_from_cov(cov)
    corr = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            denom = vols[i] * vols[j]
            corr[i][j] = (float(cov[i][j]) / denom) if denom > 0 else (1.0 if i == j else 0.0)
    return corr


def _corr_dist(corr: Sequence[Sequence[float]]) -> List[List[float]]:
    """Correlation -> distance matrix ``d = sqrt(0.5 (1 - rho))`` (clipped)."""
    n = len(corr)
    d = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            rho = max(min(float(corr[i][j]), 1.0), -1.0)
            d[i][j] = math.sqrt(max(0.5 * (1.0 - rho), 0.0))
    return d


def _cluster_var(cov: Sequence[Sequence[float]], idx: Sequence[int]) -> float:
    """Inverse-variance-weighted variance of a cluster (Lopez de Prado)."""
    ivp = [1.0 / cov[i][i] if cov[i][i] > 0 else 0.0 for i in idx]
    s = sum(ivp)
    if s <= 0:
        k = len(idx)
        ivp = [1.0 / k] * k
    else:
        ivp = [x / s for x in ivp]
    var = 0.0
    for a, i in enumerate(idx):
        for b, j in enumerate(idx):
            var += ivp[a] * ivp[b] * float(cov[i][j])
    return max(var, 1e-18)


def _single_linkage(dist: Sequence[Sequence[float]], n: int) -> List[Tuple[int, int, float]]:
    """Single-linkage agglomerative clustering (pure Python).

    Returns ``(cluster_a, cluster_b, distance)`` rows. Leaves are 0..n-1; the k-th merge
    creates cluster id n + k.
    """
    dist_between: Dict[Tuple[int, int], float] = {}
    for a in range(n):
        for b in range(a + 1, n):
            dist_between[(a, b)] = dist[a][b]

    link: List[Tuple[int, int, float]] = []
    next_id = n
    active = list(range(n))
    while len(active) > 1:
        best = None
        best_d = float("inf")
        for x in range(len(active)):
            for y in range(x + 1, len(active)):
                a, b = active[x], active[y]
                key = (a, b) if a < b else (b, a)
                d = dist_between.get(key, float("inf"))
                if d < best_d:
                    best_d = d
                    best = (a, b)
        if best is None:
            break
        a, b = best
        link.append((a, b, best_d))
        for other in active:
            if other in (a, b):
                continue
            d_ao = dist_between.get((min(a, other), max(a, other)), float("inf"))
            d_bo = dist_between.get((min(b, other), max(b, other)), float("inf"))
            dist_between[(min(next_id, other), max(next_id, other))] = min(d_ao, d_bo)
        active = [c for c in active if c not in (a, b)]
        active.append(next_id)
        next_id += 1
    return link


def _quasi_diag(link: List[Tuple[int, int, float]], n: int) -> List[int]:
    """Leaf order from a linkage list: expand the root merge depth-first, left first."""
    if n <= 1:
        return list(range(n))
    if not link:
        return list(range(n))
    order: List[int] = []
    stack: List[int] = [int(link[-1][1]), int(link[-1][0])]
    while stack:
        node = stack.pop()
        if node < n:
            order.append(node)
        else:
            left, right = int(link[node - n][0]), int(link[node - n][1])
            stack.append(right)
            stack.append(left)
    if len(order) != n:
        return list(range(n))
    return order


def _cluster_order(corr: Sequence[Sequence[float]]) -> List[int]:
    """HRP quasi-diagonal asset order for a correlation matrix."""
    n = len(corr)
    if n <= 1:
        return list(range(n))
    dist = _corr_dist(corr)
    link = _single_linkage(dist, n)
    return _quasi_diag(link, n)


def _recursive_bisection(cov: Sequence[Sequence[float]], order: Sequence[int]) -> List[float]:
    """Split the ordered list in half; the lower-variance half gets the larger share."""
    m = len(order)
    w = [1.0] * m
    items = list(order)

    def _recurse(idx: List[int]) -> None:
        if len(idx) <= 1:
            return
        mid = len(idx) // 2
        left_pos, right_pos = idx[:mid], idx[mid:]
        left_assets = [items[p] for p in left_pos]
        right_assets = [items[p] for p in right_pos]
        var_l = _cluster_var(cov, left_assets)
        var_r = _cluster_var(cov, right_assets)
        alpha = 1.0 - var_l / (var_l + var_r) if (var_l + var_r) > 0 else 0.5
        for p in left_pos:
            w[p] *= alpha
        for p in right_pos:
            w[p] *= (1.0 - alpha)
        _recurse(left_pos)
        _recurse(right_pos)

    _recurse(list(range(m)))
    return w


def _hrp_list(cov: Sequence[Sequence[float]]) -> Optional[List[float]]:
    """HRP weights aligned to the original asset order, or None if there is no answer."""
    n = len(cov or [])
    if n == 0:
        return None
    corr = _corr_from_cov(cov)
    order = _cluster_order(corr)
    w = _recursive_bisection(cov, order)
    out = [0.0] * n
    for pos, asset_idx in enumerate(order):
        out[asset_idx] = w[pos]
    total = sum(out)
    if total <= 0:
        return None
    return [x / total for x in out]


def hrp_weights(cov) -> np.ndarray:
    cov = np.asarray(cov, float)
    n = cov.shape[0]
    if n == 1:
        return np.array([1.0])
    weights = _hrp_list(cov.tolist())
    if weights is None:
        raise ValueError("HRP: no valid weights from covariance")
    return np.asarray(weights, float)
