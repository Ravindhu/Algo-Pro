"""
attribution/factor_attr.py - factor return/risk decomposition (pure).

Line up the *portfolio's* return against a set of factor returns to answer "how
much of my P&L was just market beta, and how much was my actual skill?".

Two related outputs
-------------------
* ``return_attribution`` — regress portfolio returns on factor returns
  (via ``risk.beta.factor_loadings``) and decompose the mean return into
  ``sum(beta_f * mean_factor_return_f) + alpha``.
* ``risk_attribution`` — using the sleeves' factor betas and the factor
  covariance, decompose portfolio variance into factor contributions plus
  idiosyncratic risk.

This is the arithmetic behind the dashboard "Factor Exposure" panel.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from ..risk.beta import factor_loadings
from ...core.results import mean, safe_float

logger = logging.getLogger(__name__)

__all__ = ["return_attribution", "risk_attribution", "DEFAULT_FACTORS"]


#: Sleeve-level factor names a first version uses (interpretable, not exhaustive).
DEFAULT_FACTORS = ["market", "size", "value", "momentum", "quality", "low_vol"]


def return_attribution(portfolio_returns: Sequence[float],
                       factor_returns: Dict[str, Sequence[float]],
                       factor_names: Optional[List[str]] = None
                       ) -> Dict[str, Any]:
    """Decompose portfolio return into factor contributions + alpha.

    Returns ``{"ok", "alpha", "loadings", "contributions", "r2",
    "factor_return_total", "explained_pct"}``.
    """
    names = factor_names or list(factor_returns.keys())
    if not names:
        return {"ok": False, "alpha": None, "loadings": {}, "contributions": {},
                "r2": None, "warnings": ["no factors"]}
    series = [factor_returns[n] for n in names]
    reg = factor_loadings(portfolio_returns, series)
    if not reg.get("ok"):
        return {"ok": False, "alpha": None, "loadings": {}, "contributions": {},
                "r2": None, "warnings": reg.get("warnings", [])}
    loadings = {names[i]: reg["loadings"][i] for i in range(len(names))}
    contributions = {}
    for i, n in enumerate(names):
        fm = mean([safe_float(x) for x in factor_returns[n]
                   if safe_float(x) is not None]) or 0.0
        contributions[n] = round(loadings[n] * fm, 8)
    explained = sum(contributions.values())
    p_mean = mean([safe_float(x) for x in portfolio_returns
                   if safe_float(x) is not None]) or 0.0
    return {
        "ok": True,
        "alpha": reg["alpha"],
        "loadings": loadings,
        "contributions": contributions,
        "factor_return_total": round(explained, 8),
        "r2": reg["r2"],
        "portfolio_mean": round(p_mean, 8),
        "explained_pct": (round(explained / p_mean * 100, 2)
                          if p_mean not in (0, None) else None),
        "warnings": [],
    }


def risk_attribution(weights: Dict[str, float],
                     sleeve_factor_betas: Dict[str, Dict[str, float]],
                     factor_cov: Sequence[Sequence[float]],
                     factor_names: Sequence[str],
                     idiosyncratic_var: Optional[Dict[str, float]] = None
                     ) -> Dict[str, Any]:
    """Decompose portfolio variance into factor contributions.

    ``sleeve_factor_betas[sleeve][factor]`` and a factor covariance matrix give
    the systematic variance ``b' F b`` where ``b = sum_i w_i beta_i``. Remaining
    variance (from ``idiosyncratic_var`` or unexplained) is the residual.

    Returns ``{"ok", "factor_variance", "factor_contributions", "idiosyncratic",
    "total_variance", "systematic_pct"}``.
    """
    fnames = list(factor_names)
    k = len(fnames)
    if k == 0 or len(factor_cov) < k:
        return {"ok": False, "warnings": ["no factor covariance"]}
    w_tot = sum(abs(safe_float(v) or 0.0) for v in weights.values()) or 1.0
    # Aggregate sleeve betas to portfolio betas.
    pbeta = [0.0] * k
    for sleeve, w in weights.items():
        wn = (safe_float(w) or 0.0) / w_tot
        betas = sleeve_factor_betas.get(sleeve) or {}
        for j, fn in enumerate(fnames):
            pbeta[j] += wn * (safe_float(betas.get(fn)) or 0.0)
    # Factor variance contributions: c_j = pbeta_j * (F pbeta)_j.
    Fp = [sum(float(factor_cov[j][t]) * pbeta[t] for t in range(k))
          for j in range(k)]
    contributions = {fnames[j]: round(pbeta[j] * Fp[j], 10) for j in range(k)}
    factor_var = sum(contributions.values())
    idio = sum((safe_float(v) or 0.0) for v in (idiosyncratic_var or {}).values())
    total = factor_var + idio
    return {
        "ok": True,
        "portfolio_betas": {fnames[j]: round(pbeta[j], 6) for j in range(k)},
        "factor_contributions": contributions,
        "factor_variance": round(factor_var, 10),
        "idiosyncratic": round(idio, 10),
        "total_variance": round(total, 10),
        "systematic_pct": (round(factor_var / total * 100, 2)
                           if total > 0 else None),
    }