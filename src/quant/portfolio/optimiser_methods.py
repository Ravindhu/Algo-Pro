"""Optimiser methods, phases 1 and 2: min variance, max Sharpe, ERC, inverse vol, HRP class, NCO.

Flow stage: Optimiser (portfolio sizing)
Status: PHASE 2 IMPLEMENTED (stage 6.4 phase 1-2, 2026-11-09: local port; no Blue import)
Ported from: Blue_engine/portfolio/optim/mean_variance.py, risk_parity.py, hrp.py (HRP and NCO
classes), and Blue_engine/portfolio/risk/beta.py (_solve).
HRP weights come from src/quant/portfolio/optimizer.py (hrp_weights, _corr_from_cov, _cluster_order).
Parity: tests/parity/test_optimiser_parity.py.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from quant.portfolio.optimiser_base import (
    BaseOptimiser, ConstraintSpec, normalise_weights, safe_float,
)
from quant.portfolio.optimizer import _cluster_order, _corr_from_cov, hrp_weights

__all__ = ["min_variance_weights", "max_sharpe_weights", "erc_weights", "inverse_vol_weights",
           "risk_contributions", "MinVariance", "MaxSharpe", "RiskParity", "InverseVol", "HRP", "NCO"]


def _solve(a: List[List[float]], b: List[float]) -> Optional[List[float]]:
    """Gaussian elimination with partial pivoting. None on singular."""
    n = len(a)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-15:
            return None
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for j in range(col, n + 1):
            m[col][j] /= pv
        for r in range(n):
            if r == col:
                continue
            f = m[r][col]
            if f == 0:
                continue
            for j in range(col, n + 1):
                m[r][j] -= f * m[col][j]
    return [m[i][n] for i in range(n)]


def _cov_from_problem(problem: Dict[str, Any], n: int) -> Optional[List[List[float]]]:
    cov = problem.get("cov")
    if cov and len(cov) >= n:
        return [[float(cov[i][j]) for j in range(n)] for i in range(n)]
    return None


def _mu_vector(problem: Dict[str, Any], universe: List[str]) -> List[float]:
    mu = problem.get("mu") or {}
    return [safe_float(mu.get(u)) or 0.0 for u in universe]


# --------------------------------------------------------------------------- mean-variance

def min_variance_weights(cov: List[List[float]], *, ridge: float = 1e-8) -> Optional[List[float]]:
    """Global minimum-variance weights: ``S^{-1} 1`` normalised, long-only (clip negatives)."""
    n = len(cov)
    if n == 0:
        return None
    a = [[cov[i][j] + (ridge if i == j else 0.0) for j in range(n)] for i in range(n)]
    x = _solve(a, [1.0] * n)
    if x is None:
        return None
    x = [max(v, 0.0) for v in x]
    if sum(x) <= 0:
        return [1.0 / n] * n
    return normalise_weights(x)


def max_sharpe_weights(cov: List[List[float]], mu: List[float], *,
                       rf: float = 0.0, ridge: float = 1e-8) -> Optional[List[float]]:
    """Max-Sharpe (tangency) weights: ``S^{-1} (mu - rf)`` normalised; falls back to GMV."""
    n = len(cov)
    if n == 0 or len(mu) != n:
        return None
    excess = [mu[i] - rf for i in range(n)]
    if all(abs(e) < 1e-12 for e in excess):
        return min_variance_weights(cov, ridge=ridge)
    a = [[cov[i][j] + (ridge if i == j else 0.0) for j in range(n)] for i in range(n)]
    x = _solve(a, excess)
    if x is None:
        return None
    if all(v <= 1e-9 for v in x):
        return min_variance_weights(cov, ridge=ridge)
    x = [max(v, 0.0) for v in x]
    if sum(x) <= 0:
        return min_variance_weights(cov, ridge=ridge)
    return normalise_weights(x)


class MinVariance(BaseOptimiser):
    """Global minimum-variance optimiser (no expected returns needed)."""

    name = "min_variance"

    def _optimise(self, problem, spec):
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

    def _optimise(self, problem, spec):
        universe = list(problem.get("universe") or [])
        n = len(universe)
        cov = _cov_from_problem(problem, n)
        if cov is None:
            self.log("no covariance in problem; equal weight fallback")
            return [1.0 / n] * n
        mu = _mu_vector(problem, universe)
        rf = safe_float(problem.get("risk_free")) or safe_float(self.config.get("risk_free")) or 0.0
        return max_sharpe_weights(cov, mu, rf=rf)


# --------------------------------------------------------------------------- risk parity

def _sw(cov: Sequence[Sequence[float]], w: Sequence[float]) -> List[float]:
    n = len(w)
    return [sum(float(cov[i][j]) * w[j] for j in range(n)) for i in range(n)]


def _portfolio_var(cov: Sequence[Sequence[float]], w: Sequence[float]) -> float:
    sw = _sw(cov, w)
    return sum(w[i] * sw[i] for i in range(len(w)))


def erc_weights(cov: Sequence[Sequence[float]], *, iterations: int = 100,
                tol: float = 1e-9) -> Optional[List[float]]:
    """Equal-Risk-Contribution weights via cyclical coordinate descent (long-only, sum 1)."""
    n = len(cov or [])
    if n == 0:
        return None
    c = [[float(cov[i][j]) for j in range(n)] for i in range(n)]
    for i in range(n):
        if c[i][i] <= 0:
            c[i][i] = 1e-10

    w = [1.0 / n] * n
    for _ in range(iterations):
        max_delta = 0.0
        for i in range(n):
            a = c[i][i]
            b = sum(w[j] * c[i][j] for j in range(n) if j != i)
            var = _portfolio_var(c, w)
            target_rc = var / n if var > 0 else 0.0
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
    """Inverse-volatility weights (naive risk parity, the legacy baseline)."""
    clean = [safe_float(v) for v in vols]
    if not clean or any(v is None or v <= 0 for v in clean):
        return None
    inv = [1.0 / v for v in clean]
    return normalise_weights(inv)


def risk_contributions(cov: Sequence[Sequence[float]], w: Sequence[float]) -> Optional[List[float]]:
    """Absolute risk contributions ``w_i (S w)_i / sqrt(w'Sw)``."""
    var = _portfolio_var(cov, w)
    if var <= 0:
        return None
    vol = math.sqrt(var)
    sw = _sw(cov, w)
    return [w[i] * sw[i] / vol for i in range(len(w))]


class RiskParity(BaseOptimiser):
    """True ERC optimiser (correlation-aware risk parity)."""

    name = "risk_parity"

    def _optimise(self, problem, spec):
        universe = list(problem.get("universe") or [])
        n = len(universe)
        cov = problem.get("cov")
        if cov and len(cov) >= n:
            c = [[float(cov[i][j]) for j in range(n)] for i in range(n)]
            iters = int(self.config.get("iterations", 100))
            return erc_weights(c, iterations=iters)
        vols = problem.get("vols") or {}
        iv = inverse_vol_weights([safe_float(vols.get(u)) or 0.2 for u in universe])
        self.log("no covariance; inverse-vol fallback for ERC")
        return iv


class InverseVol(BaseOptimiser):
    """Inverse-volatility optimiser (legacy baseline, kept for A/B)."""

    name = "inverse_vol"

    def _optimise(self, problem, spec):
        universe = list(problem.get("universe") or [])
        vols = problem.get("vols") or {}
        return inverse_vol_weights([safe_float(vols.get(u)) or 0.2 for u in universe])


# --------------------------------------------------------------------------- HRP and NCO

class HRP(BaseOptimiser):
    """Hierarchical Risk Parity optimiser (weights from optimizer.hrp_weights)."""

    name = "hrp"

    def _optimise(self, problem, spec):
        universe = list(problem.get("universe") or [])
        n = len(universe)
        cov = problem.get("cov")
        if cov and len(cov) >= n:
            c = [[float(cov[i][j]) for j in range(n)] for i in range(n)]
            return list(hrp_weights(c))
        vols = problem.get("vols") or {}
        self.log("no covariance; inverse-vol fallback for HRP")
        return inverse_vol_weights([safe_float(vols.get(u)) or 0.2 for u in universe])


class NCO(BaseOptimiser):
    """Nested Clustered Optimisation: HRP cluster order, ERC within each half."""

    name = "nco"

    def _optimise(self, problem, spec):
        universe = list(problem.get("universe") or [])
        n = len(universe)
        cov = problem.get("cov")
        if not cov or len(cov) < n:
            vols = problem.get("vols") or {}
            return inverse_vol_weights([safe_float(vols.get(u)) or 0.2 for u in universe])
        c = [[float(cov[i][j]) for j in range(n)] for i in range(n)]
        corr = _corr_from_cov(c) or []
        order = _cluster_order(corr) if corr else list(range(n))
        mid = len(order) // 2
        left, right = order[:mid], order[mid:] or order[:mid]
        w = [0.0] * n
        for half in (left, right):
            sub = [[c[i][j] for j in half] for i in half]
            w_sub = erc_weights(sub) or normalise_weights([1.0] * len(half))
            for pos, i in enumerate(half):
                w[i] = w_sub[pos]
        total = sum(w)
        return [x / total for x in w] if total > 0 else None
