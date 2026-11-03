"""Hierarchical Risk Parity (Lopez de Prado). Weights from the cluster tree, no matrix inversion.

Flow stage: HRP optimizer
Status: PHASE 2 IMPLEMENTED (HRP delegated to Blue_engine after its traversal fix, commit 8986b65)
Existing code to reuse or wrap (do not copy): Blue_engine/portfolio/optim/hrp.py (hrp_weights)

WHAT THIS FILE KEEPS
--------------------
- The n == 1 shortcut (weight 1.0).
- Fail-closed policy: if Blue returns None (no valid correlation or bisection result),
  this raises ValueError instead of passing None on.
- Zero-variance assets: Blue's correlation helper treats them without NaN and returns
  equal weights for an all-zero covariance. This is Blue's behaviour, not changed here.

Agreement: tests/unit/test_phase0_hrp_cov.py checks this wrapper against an independent
scipy reference on random covariances (decision record docs/decisions/0003-hrp-and-shrinkage.md).
"""
from __future__ import annotations

import numpy as np

from quant.core.blue_bridge import load


def hrp_weights(cov) -> np.ndarray:
    cov = np.asarray(cov, float)
    n = cov.shape[0]
    if n == 1:
        return np.array([1.0])
    weights = load("portfolio.optim.hrp").hrp_weights(cov.tolist())
    if weights is None:
        raise ValueError("HRP: no valid weights from covariance (Blue returned None)")
    return np.asarray(weights, float)
