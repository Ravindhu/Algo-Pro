"""
optim/cvar.py - mean-CVaR optimisation via subgradient descent (pure).

Minimising *variance* punishes upside as much as downside and is blind to the
fat left tail. **CVaR** (Expected Shortfall) is the coherent downside risk
measure regulators and serious desks use: the average loss in the worst
``(1-level)`` fraction of outcomes. Minimising CVaR while keeping some expected
return is "mean-CVaR".

Implementation
--------------
We use the Rockafellar-Uryasev reformulation, which turns CVaR into a *convex*
objective:
    CVaR_alpha(w) = min_t  t + 1/(n alpha) * sum_s max(-w.r_s - t, 0)
where ``r_s`` are scenario return vectors. We minimise it with **subgradient
descent** on ``(w, t)`` (no LP solver needed), then project back to the simplex.
This is slower than an LP but dependency-free and adequate for sleeve universes
with a few hundred scenarios.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from .base import BaseOptimiser, ConstraintSpec, normalise_weights
from ..risk.volatility import realised_vol
from ...core.results import safe_float

logger = logging.getLogger(__name__)

__all__ = ["cvar_objective", "mean_cvar_weights", "MinCVaR"]


def _scenarios(problem: Dict[str, Any], n: int, universe: Sequence[str]
               ) -> List[List[float]]:
    """Extract scenario return vectors (``S x N``).

    Prefers ``problem['scenarios']`` (a list of per-asset return series or a
    list of scenario vectors). If absent, synthesises scenarios from each asset's
    ``mu`` and ``vol`` as a symmetric grid — a crude but honest proxy so the
    optimiser still runs without history.
    """
    sc = problem.get("scenarios")
    if isinstance(sc, list) and sc:
        # Two accepted shapes: list of rows (scenarios) of length n, or list of
        # per-asset series (then transpose).
        if len(sc[0]) == n and len(sc) >= 2:
            return [[float(x) for x in row] for row in sc]
        if len(sc) == n and all(isinstance(r, (list, tuple)) for r in sc):
            t = min(len(r) for r in sc)
            return [[float(sc[i][k]) for i in range(n)] for k in range(t)]
    # Synthesise from mu/vol.
    mu_map = problem.get("mu") or {}
    vol_map = problem.get("vols") or {}
    mus = [safe_float(mu_map.get(u)) or 0.0 for u in universe]
    vols = [safe_float(vol_map.get(u)) or 0.2 for u in universe]
    out: List[List[float]] = []
    for z in (-2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0):
        out.append([mus[i] + z * vols[i] for i in range(n)])
    return out


def cvar_objective(weights: Sequence[float], scenarios: Sequence[Sequence[float]],
                   *, level: float = 0.95, t: Optional[float] = None
                   ) -> Dict[str, float]:
    """CVaR of a weight vector over scenarios (Rockafellar-Uryasev).

    Returns ``{"cvar", "t", "mean_return"}``.
    """
    n = len(weights)
    n_s = len(scenarios)
    if n_s == 0 or n == 0:
        return {"cvar": 0.0, "t": 0.0, "mean_return": 0.0}
    losses = [-sum(weights[i] * scenarios[s][i] for i in range(n))
              for s in range(n_s)]
    alpha = max(1.0 - level, 1e-6)
    if t is None:
        # Optimal t is the alpha-quantile of losses; use the empirical quantile.
        srt = sorted(losses)
        t = srt[min(int(alpha * n_s), n_s - 1)]
    tail = [max(l - t, 0.0) for l in losses]
    cvar = t + sum(tail) / (alpha * n_s)
    mean_ret = sum(-l for l in losses) / n_s
    return {"cvar": cvar, "t": t, "mean_return": mean_ret}


def mean_cvar_weights(cov_or_scen: Any, *, level: float = 0.95,
                      mu: Optional[Sequence[float]] = None,
                      risk_aversion: float = 1.0, iterations: int = 400,
                      step: float = 0.02) -> Optional[List[float]]:
    """Minimise ``CVaR - lambda * mean_return`` by subgradient descent.

    ``cov_or_scen`` is the scenario matrix (``S x N``). ``mu`` (optional) is a
    return vector used only for the reward term when there are no scenarios'
    mean. Returns a normalised long-only weight vector.
    """
    scen = [[float(x) for x in row] for row in (cov_or_scen or [])]
    if not scen:
        return None
    n = len(scen[0])
    w = [1.0 / n] * n
    t = 0.0
    for it in range(iterations):
        s = scen[it % len(scen)]
        loss = -sum(w[i] * s[i] for i in range(n))
        alpha = max(1.0 - level, 1e-6)
        ind = 1.0 if loss > t else 0.0
        # Subgradients.
        g_w = [(-s[i]) * (1.0 + ind / alpha) / len(scen) for i in range(n)]
        if mu is not None:
            g_w = [g_w[i] - risk_aversion * float(mu[i]) for i in range(n)]
        g_t = 1.0 - ind / alpha
        lr = step / (1.0 + it * 0.001)
        w = [w[i] - lr * g_w[i] for i in range(n)]
        t = t - lr * g_t
        # Project to the simplex (long-only, sum 1).
        w = _project_simplex(w)
    return normalise_weights([max(x, 0.0) for x in w])


def _project_simplex(v: Sequence[float]) -> List[float]:
    """Euclidean projection of a vector onto the probability simplex."""
    n = len(v)
    u = sorted([float(x) for x in v], reverse=True)
    css = 0.0
    rho = 0
    theta = 0.0
    for i in range(n):
        css += u[i]
        if u[i] - (css - 1.0) / (i + 1) > 0:
            rho = i + 1
            theta = (css - 1.0) / (i + 1)
    return [max(float(x) - theta, 0.0) for x in v]


class MinCVaR(BaseOptimiser):
    """Mean-CVaR optimiser (tail-aware construction)."""

    name = "min_cvar"

    def _optimise(self, problem: Dict[str, Any], spec: ConstraintSpec
                  ) -> Optional[List[float]]:
        universe = list(problem.get("universe") or [])
        n = len(universe)
        scen = _scenarios(problem, n, universe)
        level = safe_float(self.config.get("cvar_level")) or 0.95
        mu_map = problem.get("mu") or {}
        mu = [safe_float(mu_map.get(u)) or 0.0 for u in universe]
        w = mean_cvar_weights(scen, level=level, mu=mu,
                              iterations=int(self.config.get("iterations", 400)))
        if w:
            return w
        # Degenerate -> inverse-vol fallback.
        vols = problem.get("vols") or {}
        from .risk_parity import inverse_vol_weights
        return inverse_vol_weights([safe_float(vols.get(u)) or 0.2
                                    for u in universe])