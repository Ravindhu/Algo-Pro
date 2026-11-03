"""Covariance with constant-correlation shrinkage.

Flow stage: Covariance, shrinkage
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/ml/features/cross_section.py
NOTE: shrinkage intensity is a fixed input, not estimated. Estimation is a later upgrade.
"""
from __future__ import annotations

import numpy as np


def shrink_cov(returns, delta: float = 0.5) -> np.ndarray:
    """Blend sample covariance with a constant-correlation target. delta in [0, 1]."""
    if not 0 <= delta <= 1:
        raise ValueError("delta must be in [0, 1]")
    R = np.asarray(returns, float)
    S = np.cov(R, rowvar=False, ddof=1)
    sd = np.sqrt(np.diag(S))
    corr = S / np.outer(sd, sd)
    n = S.shape[0]
    rho = (corr.sum() - n) / (n * (n - 1)) if n > 1 else 0.0
    F = rho * np.outer(sd, sd)
    np.fill_diagonal(F, sd**2)
    return delta * F + (1 - delta) * S
