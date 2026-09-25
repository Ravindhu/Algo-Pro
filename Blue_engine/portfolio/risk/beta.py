"""
risk/beta.py - beta and factor-loading estimation (pure).

Beta answers "how much does this sleeve move for a 1% move in the benchmark?"
It drives the hedge sizing (``portfolio_engine._hedge``) and the beta-band
constraint in the optimiser.

Estimators
----------
* ``ols_beta``        classic Cov(r, m) / Var(m). The default.
* ``downside_beta``   beta computed on down-market days only. Investors care
                      more about downside co-movement; a sleeve that is only
                      correlated to the index when it *falls* is not defensive.
* ``rolling_beta``    a beta time series (expanding or fixed window) — the right
                      way to avoid the "full-sample beta applied historically"
                      look-ahead trap flagged in the plan.
* ``factor_loadings`` a simple multi-factor regression via the normal equations
                      (used for the Fama-French / MSCI-style exposure report).
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence

from ...core.results import mean, safe_float, stdev

logger = logging.getLogger(__name__)

__all__ = [
    "ols_beta", "downside_beta", "rolling_beta", "factor_loadings",
    "portfolio_beta", "alpha_jensen",
]


def _aligned(a: Sequence[float], b: Sequence[float]) -> List[tuple]:
    pairs = []
    for x, y in zip(a or [], b or []):
        fx, fy = safe_float(x), safe_float(y)
        if fx is not None and fy is not None:
            pairs.append((fx, fy))
    return pairs


def ols_beta(returns: Sequence[float], market: Sequence[float]
             ) -> Optional[float]:
    """Classic OLS beta = Cov(asset, market) / Var(market)."""
    pairs = _aligned(returns, market)
    if len(pairs) < 2:
        return None
    mx = mean([p[0] for p in pairs])
    my = mean([p[1] for p in pairs])
    if mx is None or my is None:
        return None
    cov = sum((p[0] - mx) * (p[1] - my) for p in pairs) / (len(pairs) - 1)
    var = sum((p[1] - my) ** 2 for p in pairs) / (len(pairs) - 1)
    if var == 0:
        return None
    return cov / var


def downside_beta(returns: Sequence[float], market: Sequence[float]
                  ) -> Optional[float]:
    """Beta measured on the subset of days when the market fell.

    Cov(asset, market | market < 0) / Var(market | market < 0). A defensive
    sleeve should have a *low* downside beta even if its full-sample beta is 1.
    """
    pairs = [p for p in _aligned(returns, market) if p[1] < 0]
    if len(pairs) < 2:
        return None
    mx = mean([p[0] for p in pairs])
    my = mean([p[1] for p in pairs])
    if mx is None or my is None:
        return None
    cov = sum((p[0] - mx) * (p[1] - my) for p in pairs) / (len(pairs) - 1)
    var = sum((p[1] - my) ** 2 for p in pairs) / (len(pairs) - 1)
    if var == 0:
        return None
    return cov / var


def rolling_beta(returns: Sequence[float], market: Sequence[float], *,
                 window: int = 60, expanding: bool = False
                 ) -> List[Optional[float]]:
    """A beta time series aligned to the input (later entries use later data).

    With ``expanding=True`` each beta uses all data up to ``t`` (growing
    window); otherwise a fixed trailing ``window``. The first ``window-1``
    entries (or fewer for expanding) are ``None``. This is the look-ahead-safe
    way to feed beta into any historical simulation.
    """
    pairs = _aligned(returns, market)
    out: List[Optional[float]] = []
    for t in range(len(pairs)):
        lo = 0 if expanding else max(0, t - window + 1)
        seg = pairs[lo:t + 1]
        if len(seg) < max(2, min(window, 6)):
            out.append(None)
            continue
        mx = mean([p[0] for p in seg])
        my = mean([p[1] for p in seg])
        if mx is None or my is None:
            out.append(None)
            continue
        cov = sum((p[0] - mx) * (p[1] - my) for p in seg) / (len(seg) - 1)
        var = sum((p[1] - my) ** 2 for p in seg) / (len(seg) - 1)
        out.append(None if var == 0 else cov / var)
    return out


def portfolio_beta(weights: Dict[str, float],
                   betas: Dict[str, float]) -> Optional[float]:
    """Weighted beta of a portfolio (weights normalised to sum 1 first)."""
    if not weights:
        return None
    total = sum(abs(safe_float(v) or 0.0) for v in weights.values())
    if total <= 0:
        return None
    return round(sum((safe_float(w) or 0.0) / total
                     * (safe_float(betas.get(k)) or 1.0)
                     for k, w in weights.items()), 6)


def factor_loadings(returns: Sequence[float],
                    factors: Sequence[Sequence[float]], *,
                    ridge: float = 1e-6) -> Dict[str, Any]:
    """Multi-factor OLS via the normal equations (pure, no numpy required).

    ``factors`` is a list of factor return series (each aligned to ``returns``).
    Returns ``{"ok", "alpha", "loadings", "r2", "n", "warnings"}``.

    A ridge term is added to the diagonal so a near-collinear factor set (e.g.
    two highly-correlated sector factors) does not make the solve singular.
    """
    n_fac = len(factors)
    if n_fac == 0:
        return {"ok": False, "alpha": None, "loadings": [], "r2": None, "n": 0,
                "warnings": ["no factors supplied"]}
    cols: List[List[float]] = [[1.0] * 0]  # intercept column placeholder
    rows = []
    for t in range(len(returns or [])):
        y = safe_float(returns[t])
        xs = [safe_float(f[t]) if t < len(f) else None for f in factors]
        if y is None or any(x is None for x in xs):
            continue
        rows.append([y] + xs)  # [target, f1, f2, ...]
    if len(rows) < n_fac + 2:
        return {"ok": False, "alpha": None, "loadings": [], "r2": None,
                "n": len(rows),
                "warnings": ["insufficient aligned rows for factor regression"]}
    p = n_fac + 1  # + intercept
    # Design matrix X (n x p) with a leading 1 column; solve (X'X + ridge I) b = X'y.
    xtx = [[0.0] * p for _ in range(p)]
    xty = [0.0] * p
    for r in rows:
        xrow = [1.0] + r[1:]
        y = r[0]
        for i in range(p):
            xty[i] += xrow[i] * y
            for j in range(p):
                xtx[i][j] += xrow[i] * xrow[j]
    for i in range(p):
        xtx[i][i] += ridge
    beta = _solve(xtx, xty)
    if beta is None:
        return {"ok": False, "alpha": None, "loadings": [], "r2": None,
                "n": len(rows), "warnings": ["normal-equation solve failed"]}
    # R^2.
    ybar = mean([r[0] for r in rows]) or 0.0
    ss_tot = sum((r[0] - ybar) ** 2 for r in rows)
    ss_res = 0.0
    for r in rows:
        pred = beta[0] + sum(beta[i + 1] * r[i + 1] for i in range(n_fac))
        ss_res += (r[0] - pred) ** 2
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 0 else None
    return {"ok": True, "alpha": round(beta[0], 8),
            "loadings": [round(b, 6) for b in beta[1:]], "r2": None if r2 is None
            else round(r2, 4), "n": len(rows), "warnings": []}


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


def alpha_jensen(returns: Sequence[float], market: Sequence[float], *,
                 rf: float = 0.0) -> Optional[float]:
    """Jensen's alpha (daily): mean(r) - rf - beta * (mean(m) - rf)."""
    pairs = _aligned(returns, market)
    if len(pairs) < 2:
        return None
    b = ols_beta(returns, market)
    if b is None:
        return None
    m_r = mean([p[0] for p in pairs])
    m_m = mean([p[1] for p in pairs])
    if m_r is None or m_m is None:
        return None
    rf_d = rf / 252.0
    return round(m_r - rf_d - b * (m_m - rf_d), 8)