"""
optim/mean_variance.py - min-variance and max-Sharpe (closed form).

Markowitz's mean-variance problem has a closed-form solution along the frontier
once you drop the inequality constraints (box/turnover are re-applied by the
base class). This module uses those closed forms — no QP solver, no dependency.

Two canonical portfolios
------------------------
* **Global minimum-variance (GMV)**: ``w ∝ S^{-1} 1``. Needs NO expected
  returns, so it is the most robust "smart" portfolio and a great baseline.
* **Tangency / max-Sharpe**: ``w ∝ S^{-1} (mu - rf*1)``. Uses expected returns,
  so it is sensitive to ``mu`` — which is exactly why Black-Litterman exists.

Both are computed by solving ``S x = b`` with Gaussian elimination (the
covariance is small; one solve is cheap and exact enough).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base import BaseOptimiser, ConstraintSpec, normalise_weights
from ..risk.beta import _solve
from ...core.results import safe_float

logger = logging.getLogger(__name__)

__all__ = ["MinVariance", "MaxSharpe", "min_variance_weights",
           "max_sharpe_weights"]


def _cov_from_problem(problem: Dict[str, Any], n: int
                      ) -> Optional[List[List[float]]]:
    cov = problem.get("cov")
    if cov and len(cov) >= n:
        return [[float(cov[i][j]) for j in range(n)] for i in range(n)]
    return None


def _mu_vector(problem: Dict[str, Any], universe: List[str]) -> List[float]:
    mu = problem.get("mu") or {}
    return [safe_float(mu.get(u)) or 0.0 for u in universe]


def min_variance_weights(cov: List[List[float]], *, ridge: float = 1e-8
                         ) -> Optional[List[float]]:
    """Global minimum-variance weights: ``S^{-1} 1`` normalised, long-only.

    A small ridge stabilises a near-singular ``S`` (two identical sleeves).
    """
    n = len(cov)
    if n == 0:
        return None
    a = [[cov[i][j] + (ridge if i == j else 0.0) for j in range(n)]
         for i in range(n)]
    x = _solve(a, [1.0] * n)
    if x is None:
        return None
    # Long-only projection: clip negatives and renormalise (a pragmatic repair
    # for the unconstrained solution; the base class enforces it again).
    x = [max(v, 0.0) for v in x]
    if sum(x) <= 0:
        return [1.0 / n] * n
    return normalise_weights(x)


def max_sharpe_weights(cov: List[List[float]], mu: List[float], *,
                       rf: float = 0.0, ridge: float = 1e-8
                       ) -> Optional[List[float]]:
    """Max-Sharpe (tangency) weights: ``S^{-1} (mu - rf)`` normalised."""
    n = len(cov)
    if n == 0 or len(mu) != n:
        return None
    excess = [mu[i] - rf for i in range(n)]
    if all(abs(e) < 1e-12 for e in excess):
        return min_variance_weights(cov, ridge=ridge)
    a = [[cov[i][j] + (ridge if i == j else 0.0) for j in range(n)]
         for i in range(n)]
    x = _solve(a, excess)
    if x is None:
        return None
    # If the unconstrained solve is a short-heavy vector, fall back to GMV.
    if all(v <= 1e-9 for v in x):
        return min_variance_weights(cov, ridge=ridge)
    x = [max(v, 0.0) for v in x]
    if sum(x) <= 0:
        return min_variance_weights(cov, ridge=ridge)
    return normalise_weights(x)


class MinVariance(BaseOptimiser):
    """Global minimum-variance optimiser (no expected returns needed)."""

    name = "min_variance"

    def _optimise(self, problem: Dict[str, Any], spec: ConstraintSpec
                  ) -> Optional[List[float]]:
        universe = list(problem.get("universe") or [])
        n = len(universe)
        cov = _cov_from_problem(problem, n)
        if cov is None:
            self.log("no covariance in problem; equal weight fallback")
            return [1.0 / n] * n
        return min_variance_weights(cov)


class MaxSharpe(BaseOptimiser):
    """Tangency (max Sharpe) optimiser. Uses ``mu``; falls back to GMV."""

    name = "max_sharpe"

    def _optimise(self, problem: Dict[str, Any], spec: ConstraintSpec
                  ) -> Optional[List[float]]:
        universe = list(problem.get("universe") or [])
        n = len(universe)
        cov = _cov_from_problem(problem, n)
        if cov is None:
            self.log("no covariance in problem; equal weight fallback")
            return [1.0 / n] * n
        mu = _mu_vector(problem, universe)
        rf = safe_float(problem.get("risk_free")) or safe_float(
            self.config.get("risk_free")) or 0.0
        return max_sharpe_weights(cov, mu, rf=rf)