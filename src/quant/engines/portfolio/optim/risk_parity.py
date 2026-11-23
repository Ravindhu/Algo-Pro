"""
optim/risk_parity.py - true Equal Risk Contribution (ERC) via cyclic descent.

The baseline engine used *inverse-volatility* weights, which equalise standalone
vol but NOT risk contribution when sleeves are correlated. **True risk parity**
solves for weights where each sleeve contributes the same amount to portfolio
risk:

    RC_i = w_i * (S w)_i  equal  across i,  with sum(|w|) = 1.

We use **cyclical coordinate descent** (Griveau-Billion et al., "A Fast Algorithm
for Computing High Dimensional Risk Parity Portfolios"): for each asset in turn,
solve a 1-D quadratic for the ``w_i`` that equalises its risk contribution given
the others fixed. It converges in a handful of sweeps and needs no solver.

Also provided: ``inverse_vol_weights`` (the old behaviour, kept as a baseline for
A/B backtests) so the desk can measure the improvement ERC delivers.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import BaseOptimiser, ConstraintSpec, normalise_weights
from ...core.results import safe_float

logger = logging.getLogger(__name__)

__all__ = ["erc_weights", "inverse_vol_weights", "RiskParity", "InverseVol"]


def _sw(cov: Sequence[Sequence[float]], w: Sequence[float]) -> List[float]:
    n = len(w)
    return [sum(float(cov[i][j]) * w[j] for j in range(n)) for i in range(n)]


def _portfolio_var(cov: Sequence[Sequence[float]], w: Sequence[float]) -> float:
    sw = _sw(cov, w)
    return sum(w[i] * sw[i] for i in range(len(w)))


def erc_weights(cov: Sequence[Sequence[float]], *, iterations: int = 100,
                tol: float = 1e-9) -> Optional[List[float]]:
    """Equal-Risk-Contribution weights via cyclical coordinate descent.

    Returns a long-only, fully-invested (``sum = 1``) weight vector, or None on a
    degenerate covariance. Converges monotonically; ``iterations`` is a hard cap.
    """
    n = len(cov or [])
    if n == 0:
        return None
    # Work on a diagonal-stabilised copy.
    c = [[float(cov[i][j]) for j in range(n)] for i in range(n)]
    for i in range(n):
        if c[i][i] <= 0:
            c[i][i] = 1e-10

    w = [1.0 / n] * n
    for _ in range(iterations):
        max_delta = 0.0
        for i in range(n):
            # a = c[i][i]; b = sum_{j!=i} w_j c[i][j]; solve a w_i^2 + b w_i - ...
            a = c[i][i]
            b = sum(w[j] * c[i][j] for j in range(n) if j != i)
            # Target: w_i (a w_i + b) == common RC. Cyclical descent sets the new
            # w_i so its RC matches the current average RC of the other assets
            # (an implicit target), solved as a 1-D quadratic.
            var = _portfolio_var(c, w)
            target_rc = var / n if var > 0 else 0.0
            # Solve a w_i^2 + b w_i - target_rc = 0 for the positive root.
            if a <= 0:
                continue
            disc = b * b + 4.0 * a * target_rc
            if disc < 0:
                continue
            new_w = (-b + math.sqrt(disc)) / (2.0 * a)
            if new_w <= 0:
                new_w = 1e-12
            max_delta = max(max_delta, abs(new_w - w[i]))
            w[i] = new_w
        if max_delta < tol:
            break
    total = sum(w)
    if total <= 0:
        return None
    return [x / total for x in w]


def inverse_vol_weights(vols: Sequence[float]) -> Optional[List[float]]:
    """Inverse-volatility (naive risk parity) weights — the old baseline.

    Kept explicitly so the benchmark can compare ERC/HRP against the status quo.
    """
    clean = [safe_float(v) for v in vols]
    if not clean or any(v is None or v <= 0 for v in clean):
        return None
    inv = [1.0 / v for v in clean]
    return normalise_weights(inv)


def risk_contributions(cov: Sequence[Sequence[float]],
                       w: Sequence[float]) -> Optional[List[float]]:
    """Absolute risk contributions ``w_i (S w)_i / sqrt(w'Sw)`` (component vol)."""
    var = _portfolio_var(cov, w)
    if var <= 0:
        return None
    vol = math.sqrt(var)
    sw = _sw(cov, w)
    return [w[i] * sw[i] / vol for i in range(len(w))]


class RiskParity(BaseOptimiser):
    """True ERC optimiser (correlation-aware risk parity)."""

    name = "risk_parity"

    def _optimise(self, problem: Dict[str, Any], spec: ConstraintSpec
                  ) -> Optional[List[float]]:
        universe = list(problem.get("universe") or [])
        n = len(universe)
        cov = problem.get("cov")
        if cov and len(cov) >= n:
            c = [[float(cov[i][j]) for j in range(n)] for i in range(n)]
            iters = int(self.config.get("iterations", 100))
            return erc_weights(c, iterations=iters)
        # No covariance -> fall back to inverse-vol from the vols dict.
        vols = problem.get("vols") or {}
        iv = inverse_vol_weights([safe_float(vols.get(u)) or 0.2
                                  for u in universe])
        self.log("no covariance; inverse-vol fallback for ERC")
        return iv


class InverseVol(BaseOptimiser):
    """Inverse-volatility optimiser (legacy baseline, kept for A/B)."""

    name = "inverse_vol"

    def _optimise(self, problem: Dict[str, Any], spec: ConstraintSpec
                  ) -> Optional[List[float]]:
        universe = list(problem.get("universe") or [])
        vols = problem.get("vols") or {}
        return inverse_vol_weights([safe_float(vols.get(u)) or 0.2
                                    for u in universe])