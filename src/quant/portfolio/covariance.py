"""Shrunk covariance estimate (constant-correlation target). Local code (no Blue import).

Flow stage: Covariance, shrinkage
Status: PHASE 2 IMPLEMENTED (stage 3 port 2026-11-06: estimator is local; no Blue import)
Ported from: src/quant/blue_engine/portfolio/risk/covariance.py (sample_cov, ledoit_wolf_cov with a
supplied delta, nearest_psd). Parity: tests/parity/test_stage3_parity.py

Notes
-----
- The intensity ``delta`` is supplied by the caller, so the data-driven LW intensity is not used.
- Pairs with a missing value (NaN/inf) are dropped per pair, as Blue does.
- The PSD guard clips eigenvalues at 1e-12 (Blue's rule). Blue's eigenvector bug was fixed in 9b0bff4.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence

import numpy as np


def _clean(x) -> Optional[float]:
    """Finite float or None (Blue's safe_float rule)."""
    if x is None or x == "":
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _sample_cov(panel: List[List[Optional[float]]], ddof: int = 1) -> Optional[List[List[float]]]:
    """Pairwise-complete sample covariance, asset-major panel. None when undefined."""
    n = len(panel)
    if n < 2:
        return None
    tmin = min((len(row) for row in panel), default=0)
    if tmin < 2:
        return None
    panel = [row[-tmin:] for row in panel]
    means = [(_mean([x for x in row if x is not None]) or 0.0) for row in panel]
    cov = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i, n):
            s, cnt = 0.0, 0
            for t in range(tmin):
                xi, xj = panel[i][t], panel[j][t]
                if xi is None or xj is None:
                    continue
                s += (xi - means[i]) * (xj - means[j])
                cnt += 1
            c = s / (cnt - ddof) if cnt > ddof else 0.0
            cov[i][j] = c
            cov[j][i] = c
    return cov


def _nearest_psd(m: np.ndarray, floor: float = 1e-12) -> np.ndarray:
    """Symmetric eigenvalue clipping: V max(L, floor) V^T."""
    a = (m + m.T) / 2.0
    vals, vecs = np.linalg.eigh(a)
    vals = np.maximum(vals, floor)
    out = (vecs * vals) @ vecs.T
    return (out + out.T) / 2.0


def shrink_cov(returns, delta: float = 0.5):
    """Blend the sample covariance with a constant-correlation target. delta in [0, 1].

    ``returns`` is a time-by-asset array. Returns an asset-by-asset numpy array.
    Target: keeps each asset's own variance; cross terms = average correlation x vols.
    """
    if not 0 <= delta <= 1:
        raise ValueError("delta must be in [0, 1]")
    rows = np.asarray(returns, float)
    panel = [[_clean(x) for x in row] for row in rows.T.tolist()]  # one row per asset
    sample = _sample_cov(panel)
    n = len(panel)
    if sample is None:
        raise ValueError("shrinkage undefined for this panel (need >= 2 assets, >= 2 observations)")

    vols = [math.sqrt(max(sample[i][i], 0.0)) for i in range(n)]
    offs = []
    for i in range(n):
        for j in range(i + 1, n):
            if vols[i] > 0 and vols[j] > 0:
                offs.append(sample[i][j] / (vols[i] * vols[j]))
    rho_bar = _mean(offs) or 0.0

    d = float(delta)
    shrunk = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            target = (vols[i] * vols[j] * rho_bar) if i != j else sample[i][i]
            shrunk[i, j] = (1.0 - d) * sample[i][j] + d * target
    shrunk = _nearest_psd(shrunk)
    return np.asarray(shrunk, float)
