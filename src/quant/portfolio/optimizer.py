"""Hierarchical Risk Parity (Lopez de Prado). Weights from the cluster tree, no matrix inversion.

Flow stage: HRP optimizer
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/auto_tune.py
"""
from __future__ import annotations

import numpy as np
from scipy.cluster.hierarchy import leaves_list, linkage
from scipy.spatial.distance import squareform


def _cluster_var(cov: np.ndarray, idx) -> float:
    sub = cov[np.ix_(idx, idx)]
    ivp = 1.0 / np.diag(sub)
    ivp /= ivp.sum()
    return float(ivp @ sub @ ivp)


def hrp_weights(cov) -> np.ndarray:
    cov = np.asarray(cov, float)
    n = cov.shape[0]
    if n == 1:
        return np.array([1.0])
    sd = np.sqrt(np.diag(cov))
    corr = np.clip(cov / np.outer(sd, sd), -1, 1)
    dist = np.sqrt(0.5 * (1 - corr))
    np.fill_diagonal(dist, 0.0)
    order = list(leaves_list(linkage(squareform(dist, checks=False), "single")))
    w = np.ones(n)
    clusters = [order]
    while clusters:
        new = []
        for c in clusters:
            if len(c) > 1:
                h = len(c) // 2
                new += [c[:h], c[h:]]
        for i in range(0, len(new), 2):
            c0, c1 = new[i], new[i + 1]
            v0, v1 = _cluster_var(cov, c0), _cluster_var(cov, c1)
            alpha = 1 - v0 / (v0 + v1)
            w[c0] *= alpha
            w[c1] *= 1 - alpha
        clusters = new
    return w / w.sum()
