"""
risk/contribution.py - risk attribution (pure).

Answering "which sleeve is actually driving my risk?" is the difference between
an allocator and a *portfolio manager*. This module decomposes total portfolio
risk into per-asset contributions.

Definitions (portfolio volatility ``sigma_p = sqrt(w' S w)``)
-------------------------------------------------------------
* **Marginal contribution to risk (MCTR)**:  ``d sigma_p / d w_i
  = (S w)_i / sigma_p`` — the change in portfolio vol per unit change in weight.
* **Component (absolute) contribution**:  ``C_i = w_i * MCTR_i`` — the sleeve's
  share of the *vol itself*. By Euler's theorem ``sum_i C_i = sigma_p``.
* **Percentage contribution (PCR)**:  ``C_i / sigma_p`` — sums to 1; the number
  the risk tab shows as "who owns the risk".
* **Variance contribution**:  ``w_i (S w)_i`` — sums to the portfolio variance.

The plan's headline insight: a naive inverse-vol book can have a PCR that is
wildly uneven because it ignores correlation. This module exposes that.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence

from ...core.results import safe_float

logger = logging.getLogger(__name__)

__all__ = [
    "portfolio_vol_from_cov", "marginal_contributions",
    "component_contributions", "percentage_contributions",
    "variance_contributions", "analyse", "herfindahl",
]


def portfolio_vol_from_cov(weights: Sequence[float],
                           cov: Sequence[Sequence[float]]
                           ) -> Optional[float]:
    """``sqrt(w' S w)`` for a covariance matrix (annualised if ``cov`` is)."""
    w = [safe_float(x) or 0.0 for x in (weights or [])]
    n = len(w)
    if n == 0 or len(cov) < n:
        return None
    var = 0.0
    for i in range(n):
        for j in range(n):
            if j < len(cov[i]):
                var += w[i] * w[j] * float(cov[i][j])
    return math.sqrt(var) if var >= 0 else None


def _s_w(weights: Sequence[float], cov: Sequence[Sequence[float]]
         ) -> List[float]:
    """Compute ``S w`` (the covariance-times-weights vector)."""
    w = [safe_float(x) or 0.0 for x in weights]
    n = len(w)
    out = [0.0] * n
    for i in range(n):
        s = 0.0
        for j in range(n):
            if i < len(cov) and j < len(cov[i]):
                s += float(cov[i][j]) * w[j]
        out[i] = s
    return out


def marginal_contributions(weights: Sequence[float],
                           cov: Sequence[Sequence[float]]
                           ) -> Optional[List[float]]:
    """MCTR_i = (S w)_i / sigma_p. None when sigma_p is zero/degenerate."""
    vol = portfolio_vol_from_cov(weights, cov)
    if vol is None or vol <= 0:
        return None
    sw = _s_w(weights, cov)
    return [x / vol for x in sw]


def component_contributions(weights: Sequence[float],
                            cov: Sequence[Sequence[float]]
                            ) -> Optional[List[float]]:
    """Absolute risk contribution C_i = w_i * MCTR_i (sums to sigma_p)."""
    mctr = marginal_contributions(weights, cov)
    if mctr is None:
        return None
    w = [safe_float(x) or 0.0 for x in weights]
    return [w[i] * mctr[i] for i in range(len(w))]


def percentage_contributions(weights: Sequence[float],
                             cov: Sequence[Sequence[float]]
                             ) -> Optional[List[float]]:
    """PCR_i = C_i / sum_j C_j (sums to 1; the "who owns the risk" number)."""
    comp = component_contributions(weights, cov)
    if comp is None:
        return None
    total = sum(comp)
    if total <= 0:
        return None
    return [c / total for c in comp]


def variance_contributions(weights: Sequence[float],
                           cov: Sequence[Sequence[float]]
                           ) -> Optional[List[float]]:
    """Variance contribution w_i (S w)_i (sums to portfolio variance)."""
    w = [safe_float(x) or 0.0 for x in weights]
    sw = _s_w(w, cov)
    return [w[i] * sw[i] for i in range(len(w))]


def herfindahl(weights: Sequence[float], *, normalise: bool = True
               ) -> Optional[float]:
    """Herfindahl-Hirschman concentration index of weights.

    ``HHI = sum_i (w_i / sum w)^2`` when ``normalise`` (so it is scale-free and
    ranges ``1/N`` for equal weights to 1 for a single name). This matches the
    convention the baseline ``portfolio_engine._risk`` already uses.
    """
    ws = [safe_float(x) or 0.0 for x in (weights or [])]
    total = sum(ws)
    if total <= 0:
        return None
    if normalise:
        return sum((x / total) ** 2 for x in ws)
    return sum(x ** 2 for x in ws)


def analyse(weights: Sequence[float], cov: Sequence[Sequence[float]],
            names: Sequence[str], *, corr: Optional[Sequence[Sequence[float]]]
            = None) -> Dict[str, Any]:
    """Full risk-attribution bundle for the risk block / dashboard.

    Returns per-asset MCTR / component / PCR / variance contribution plus the
    totals and the dominant contributor. Degrades to a partial dict (with a
    warning) when the covariance is degenerate.
    """
    names = list(names)
    warnings: List[str] = []
    vol = portfolio_vol_from_cov(weights, cov)
    if vol is None or vol <= 0:
        return {"ok": False, "portfolio_vol": None, "contributions": {},
                "pcr": {}, "mctr": {}, "dominant": None, "hhi": None,
                "warnings": ["degenerate covariance; no risk attribution"]}

    mctr = marginal_contributions(weights, cov) or [0.0] * len(names)
    comp = component_contributions(weights, cov) or [0.0] * len(names)
    pcr = percentage_contributions(weights, cov) or [0.0] * len(names)
    vc = variance_contributions(weights, cov) or [0.0] * len(names)
    n = min(len(names), len(mctr))
    contrib = {names[i]: round(abs(comp[i]), 8) for i in range(n)}
    dominant = max(contrib, key=contrib.get) if contrib else None
    return {
        "ok": True,
        "portfolio_vol": round(vol, 6),
        "mctr": {names[i]: round(mctr[i], 8) for i in range(n)},
        "contributions": contrib,
        "pcr": {names[i]: round(pcr[i], 6) for i in range(n)},
        "variance_contributions": {names[i]: round(vc[i], 8) for i in range(n)},
        "dominant": dominant,
        "hhi": None if herfindahl(weights) is None
        else round(herfindahl(weights), 6),
        "warnings": warnings,
    }