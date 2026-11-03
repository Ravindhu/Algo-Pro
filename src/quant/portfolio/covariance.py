"""Shrunk covariance estimate (constant-correlation target). Thin wrapper over Blue_engine.

Flow stage: Covariance, shrinkage
Status: PHASE 2 IMPLEMENTED (wrapper: estimator delegated to Blue_engine)
Existing code to reuse or wrap (do not copy): Blue_engine/portfolio/risk/covariance.py (ledoit_wolf_cov)

Notes
-----
- The intensity ``delta`` is supplied by the caller, so Blue does not estimate it.
- Blue's ``nearest_psd`` eigenvector bug was fixed in commit 9b0bff4. Before that fix,
  this wrapper's output differed from the sample-covariance identity at delta=0.
- Agreement with the pre-wrap implementation: 240 random panels, max diff 6e-13.
"""
from __future__ import annotations

from quant.core.blue_bridge import load


def shrink_cov(returns, delta: float = 0.5):
    """Blend the sample covariance with a constant-correlation target. delta in [0, 1].

    ``returns`` is a time-by-asset array. Returns an asset-by-asset numpy array.
    """
    import numpy as np

    if not 0 <= delta <= 1:
        raise ValueError("delta must be in [0, 1]")
    rows = np.asarray(returns, float)
    panel = rows.T.tolist()  # Blue expects one row per asset
    out, _ = load("portfolio.risk.covariance").ledoit_wolf_cov(
        panel, annualise=False, shrink=float(delta))
    if out is None:
        raise ValueError("shrinkage undefined for this panel (need >= 2 assets, >= 2 observations)")
    return np.asarray(out, float)
