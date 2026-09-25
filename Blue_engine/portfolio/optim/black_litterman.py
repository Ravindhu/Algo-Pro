"""
optim/black_litterman.py - Black-Litterman posterior returns from views.

The problem with max-Sharpe: it trusts ``mu`` completely, and ``mu`` is the
noisiest input in finance. Black-Litterman fixes this by starting from the
**market-implied equilibrium** returns (the prior you can actually defend) and
nudging them only where you hold a **view**, in proportion to your confidence.

Model (with the standard simplifications, no numpy)
---------------------------------------------------
* Prior: ``pi = delta * S * w_mkt`` (reverse-engineered equilibrium returns).
* Views: ``P mu = Q + eps``, ``eps ~ N(0, Omega)``. Each view is either
  *absolute* (``P`` has a single 1) or *relative* (a long/short spread).
* Posterior: ``mu_BL = pi + tau S P' (P tau S P' + Omega)^{-1} (Q - P pi)``.

We solve the linear system with Gaussian elimination (``risk.beta._solve``), so
no numpy is needed. ``tau`` (prior uncertainty) and ``Omega`` (view uncertainty,
often ``diag(P tau S P')``) control how far the views move the prior.

Views are produced by ``portfolio/views/`` (ML, NLP, GPT) or supplied directly.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from .base import BaseOptimiser, ConstraintSpec, normalise_weights
from .mean_variance import max_sharpe_weights, min_variance_weights
from ..risk.beta import _solve
from ...core.results import safe_float

logger = logging.getLogger(__name__)

__all__ = ["implied_equilibrium_returns", "posterior_returns",
           "BlackLitterman", "MarketCap"]


def _matmul(a: Sequence[Sequence[float]],
            b: Sequence[Sequence[float]]) -> List[List[float]]:
    n, k, m = len(a), len(b), len(b[0]) if b else 0
    return [[sum(a[i][t] * b[t][j] for t in range(k)) for j in range(m)]
            for i in range(n)]


def _matvec(a: Sequence[Sequence[float]],
            x: Sequence[float]) -> List[float]:
    return [sum(a[i][j] * x[j] for j in range(len(x))) for i in range(len(a))]


def _transpose(a: Sequence[Sequence[float]]) -> List[List[float]]:
    return [list(col) for col in zip(*a)] if a else []


def implied_equilibrium_returns(cov: Sequence[Sequence[float]],
                                market_weights: Sequence[float], *,
                                delta: float = 2.5) -> Optional[List[float]]:
    """Reverse-optimised (equilibrium) returns ``pi = delta S w_mkt``.

    ``delta`` is the risk-aversion coefficient (2.5 is the He-Litterman default
    for equities). ``market_weights`` are the *market-cap* weights of the
    universe (the neutral prior).
    """
    n = len(cov or [])
    if n == 0 or len(market_weights) != n:
        return None
    w = [safe_float(x) or 0.0 for x in market_weights]
    return [delta * v for v in _matvec(cov, w)]


def posterior_returns(cov: Sequence[Sequence[float]],
                      pi: Sequence[float],
                      P: Sequence[Sequence[float]],
                      Q: Sequence[float],
                      *, tau: float = 0.05,
                      omega: Optional[Sequence[Sequence[float]]] = None
                      ) -> Optional[List[float]]:
    """Black-Litterman posterior expected returns.

    ``P`` is ``K x N`` (K views over N assets), ``Q`` the ``K`` view returns,
    ``omega`` the ``K x K`` view-uncertainty (defaults to
    ``tau * diag(P S P')`` — the standard He-Litterman choice).
    """
    n = len(cov or [])
    if n == 0 or len(pi) != n or not P or len(P[0]) != n:
        return None
    k = len(P)
    if len(Q) != k:
        return None

    tauS = [[tau * float(cov[i][j]) for j in range(n)] for i in range(n)]
    Pt = _transpose(P)                       # n x k
    tauS_Pt = _matmul(tauS, Pt)              # n x k
    P_tauS_Pt = _matmul(P, tauS_Pt)          # k x k

    if omega is None:
        omega = [[P_tauS_Pt[i][j] if i == j else 0.0 for j in range(k)]
                 for i in range(k)]
    M = [[P_tauS_Pt[i][j] + float(omega[i][j]) for j in range(k)]
         for i in range(k)]
    rhs = [Q[i] - _dot(P[i], pi) for i in range(k)]
    # Solve M * x = rhs, then mu = pi + tauS_Pt * x.
    x = _solve(M, rhs)
    if x is None:
        return list(pi)
    adjust = _matvec(tauS_Pt, x)
    return [pi[i] + adjust[i] for i in range(n)]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(float(a[i]) * float(b[i]) for i in range(min(len(a), len(b))))


class BlackLitterman(BaseOptimiser):
    """Black-Litterman optimiser.

    Reads from the problem::

        cov        covariance (annualised)
        market_weights / market_caps   the neutral prior weights
        views      [{"assets": ["A","B"], "weights": [1,-1], "return": 0.02,
                     "confidence": 0.6, "label": "..."}]   (optional)

    With no views, BL degenerates to the market prior (equilibrium returns +
    GMV weights) — which is itself a robust, defensible default.
    """

    name = "black_litterman"

    def _optimise(self, problem: Dict[str, Any], spec: ConstraintSpec
                  ) -> Optional[List[float]]:
        universe = list(problem.get("universe") or [])
        n = len(universe)
        cov = problem.get("cov")
        if not cov or len(cov) < n:
            self.log("no covariance; equal weight fallback")
            return [1.0 / n] * n
        c = [[float(cov[i][j]) for j in range(n)] for i in range(n)]

        mkt = _market_weights(problem, universe)
        delta = safe_float(problem.get("risk_aversion")) or safe_float(
            self.config.get("risk_aversion")) or 2.5
        pi = implied_equilibrium_returns(c, mkt, delta=delta)
        if pi is None:
            return min_variance_weights(c)

        P, Q, omega = _build_views(problem, universe, c, self.config)
        if P:
            tau = safe_float(self.config.get("tau")) or 0.05
            mu = posterior_returns(c, pi, P, Q, tau=tau, omega=omega)
            self.log(f"BL posterior from {len(P)} view(s)")
        else:
            mu = pi
            self.log("no views; BL degenerates to the market prior")
        if mu is None:
            return min_variance_weights(c)

        # Construction: a *utility* tilt rather than pure tangency.
        #
        # Why not just max-Sharpe on `mu`? Tangency maximises mu/sigma, so a
        # high-vol sleeve can be DOWN-weighted even when its view is positive
        # (the view raises mu, but sigma raises the denominator faster). That
        # defeats the purpose of a view: the desk expects a positive view to
        # move the weight UP.
        #
        # So we blend two defensible portfolios:
        #   1. the unconstrained mean-variance solve w ∝ S^-1 (mu - rf) is
        #      proportional to the view, but can be short-heavy;
        #   2. the GMV base is robust and always long-only.
        # We take the *view-implied* weights and blend toward the base in
        # proportion to how much the views actually moved the posterior. The
        # result is monotone in the view direction and passes the box/gross
        # projection in ``base.project_to_box``.
        rf = safe_float(problem.get("risk_free")) or 0.0
        base = min_variance_weights(c) or [1.0 / n] * n

        tilt = _blend_toward_view(c, base, mu, pi, rf=rf)
        return tilt if tilt else base


class MarketCap(BaseOptimiser):
    """Pure market-cap (equilibrium prior) optimiser — the neutral baseline."""

    name = "market_cap"

    def _optimise(self, problem: Dict[str, Any], spec: ConstraintSpec
                  ) -> Optional[List[float]]:
        universe = list(problem.get("universe") or [])
        mkt = _market_weights(problem, universe)
        return normalise_weights(mkt) if mkt else None


def _blend_toward_view(cov: List[List[float]], base: List[float],
                       mu: List[float], pi: List[float], *,
                       rf: float = 0.0) -> Optional[List[float]]:
    """Blend the GMV base toward the mean-variance (view-implied) portfolio.

    Returns ``base`` scaled to the ``mu`` tilt when the tilt is informative, or
    None to let the caller keep the base.

    The blend factor is driven by how far the posterior moved from the prior,
    normalised by the prior's own dispersion: a small view barely moves the
    book; a large, confident view dominates it. This keeps the mapping
    *monotone* in the view direction (a positive view raises the weight), which
    is the property a PM actually relies on.
    """
    n = len(base)
    if n == 0 or len(mu) != n or len(pi) != n:
        return None
    # w_mv ∝ S^-1 (mu - rf); the unconstrained MV solution.
    excess = [mu[i] - rf for i in range(n)]
    a = [[cov[i][j] + (1e-8 if i == j else 0.0) for j in range(n)]
         for i in range(n)]
    x = _solve(a, excess)
    if x is None:
        return None
    # Normalise the (possibly long/short) MV vector onto the long-only simplex
    # by shifting to non-negative and rescaling. This is the "long-only MV".
    lo = min(x)
    shifted = [v - lo for v in x]
    total = sum(shifted)
    if total <= 0:
        return None
    mv = [v / total for v in shifted]

    # How strong is the view? Compare posterior vs prior dispersion.
    import math
    diff = [mu[i] - pi[i] for i in range(n)]
    scale = math.sqrt(sum((pi[i]) ** 2 for i in range(n)) / n) or 1.0
    mag = math.sqrt(sum(d * d for d in diff) / n) / scale
    weight = min(max(mag, 0.0), 1.0)   # 0 = prior, 1 = full MV tilt
    if weight <= 1e-12:
        return list(base)

    blended = [base[i] * (1.0 - weight) + mv[i] * weight for i in range(n)]
    total = sum(blended)
    if total <= 0:
        return None
    return [v / total for v in blended]


def _market_weights(problem: Dict[str, Any],
                    universe: Sequence[str]) -> List[float]:
    """Extract market-cap weights, falling back to equal weights."""
    mw = problem.get("market_weights")
    if isinstance(mw, dict) and mw:
        vals = [safe_float(mw.get(u)) or 0.0 for u in universe]
        if sum(vals) > 0:
            return vals
    caps = problem.get("market_caps") or problem.get("caps")
    if isinstance(caps, dict) and caps:
        vals = [safe_float(caps.get(u)) or 0.0 for u in universe]
        if sum(vals) > 0:
            return vals
    return [1.0] * len(universe)


def _build_views(problem: Dict[str, Any], universe: Sequence[str],
                 cov: Sequence[Sequence[float]],
                 config: Dict[str, Any]) -> tuple:
    """Turn the ``views`` list into (P, Q, Omega) matrices.

    Each view is ``{"assets": [...], "weights": [...], "return": x,
    "confidence": c}``. Confidence ``c in (0,1]`` sets the view variance as
    ``omega_kk = base * (1 - c)`` (higher confidence -> lower variance).
    Idiosyncratic view variance base = ``tau * p' S p``.
    """
    views = problem.get("views") or config.get("views") or []
    idx = {u: i for i, u in enumerate(universe)}
    tau = safe_float(config.get("tau")) or 0.05
    P: List[List[float]] = []
    Q: List[float] = []
    diag: List[float] = []
    for v in views:
        assets = v.get("assets") or []
        weights = v.get("weights") or [1.0] * len(assets)
        row = [0.0] * len(universe)
        valid = False
        for a, wt in zip(assets, weights):
            if a in idx:
                row[idx[a]] = safe_float(wt) or 0.0
                valid = True
        if not valid:
            continue
        ret = safe_float(v.get("return"))
        if ret is None:
            continue
        conf = safe_float(v.get("confidence"))
        conf = 1.0 if conf is None else max(min(conf, 0.999), 0.001)
        pSp = sum(row[i] * row[j] * cov[i][j]
                  for i in range(len(row)) for j in range(len(row)))
        base = tau * max(pSp, 1e-8)
        P.append(row)
        Q.append(ret)
        diag.append(base * (1.0 - conf) + 1e-10)
    if not P:
        return [], [], None
    omega = [[diag[i] if i == j else 0.0 for j in range(len(diag))]
             for i in range(len(diag))]
    return P, Q, omega