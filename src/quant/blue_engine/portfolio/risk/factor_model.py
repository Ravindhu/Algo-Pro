"""
risk/factor_model.py - factor-model / PCA decomposition of a covariance (pure).

A covariance matrix estimated from N sleeves has N(N+1)/2 numbers, but most of
the *systematic* variation lives in only a handful of factors (a market factor
plus a few sector/factor tilts). Marchenko-Pastur theory says eigenvalues below
a noise threshold are indistinguishable from sampling noise and should be
*squashed* — this both denoises the matrix and reveals the true risk structure.

What this module provides
-------------------------
* ``eigen_decomposition``  symmetric Jacobi eig (values + vectors).
* ``marchenko_pastur``     the noise threshold given T observations, N assets.
* ``clean_covariance``     eigenvalue clipping toward the Marchenko-Pastur floor
                           (a "denoised" covariance a la Laloux/Cizeau).
* ``variance_explained``   the scree/explained-variance table for the dashboard.
* ``systematic_share``     how much risk is *market/factor* vs idiosyncratic.

All pure; uses the Jacobi eig in ``risk/covariance.py`` so there is one eigen
implementation in the package.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from .covariance import _jacobi_eig, _symmetrise, _zeros
from ...core.results import safe_float

logger = logging.getLogger(__name__)

__all__ = [
    "eigen_decomposition", "marchenko_pastur", "clean_covariance",
    "variance_explained", "systematic_share", "decompose",
]


def eigen_decomposition(cov: Sequence[Sequence[float]]
                        ) -> Optional[Dict[str, Any]]:
    """Symmetric eigen-decomposition of a covariance matrix.

    Returns ``{"values", "vectors"}`` (vectors as rows) sorted by descending
    eigenvalue. None on a degenerate input.
    """
    n = len(cov or [])
    if n == 0:
        return None
    m = [[float(cov[i][j]) for j in range(n)] for i in range(n)]
    _symmetrise(m)
    vals, vecs = _jacobi_eig(m)
    order = sorted(range(n), key=lambda i: vals[i], reverse=True)
    return {
        "values": [round(vals[i], 10) for i in order],
        "vectors": [[round(vecs[i][k], 8) for k in range(n)] for i in order],
    }


def marchenko_pastur(n_assets: int, n_obs: int, *, sigma2: float = 1.0
                     ) -> Dict[str, float]:
    """Marchenko-Pastur noise band for the eigenvalues of a sample cov.

    For a ``q = T/N`` ratio, the eigenvalues of a pure-noise sample covariance
    are bounded within ``[lambda_-, lambda_+]``::

        lambda_pm = sigma2 * (1 +/- sqrt(N/T))^2

    Eigenvalues below ``lambda_-`` carry no reliable signal.
    """
    n = max(int(n_assets), 1)
    t = max(int(n_obs), 1)
    ratio = n / t
    root = ratio ** 0.5
    return {
        "lambda_minus": sigma2 * (1.0 - root) ** 2,
        "lambda_plus": sigma2 * (1.0 + root) ** 2,
        "q": ratio,
    }


def clean_covariance(cov: Sequence[Sequence[float]], *, n_obs: int,
                     sigma2: Optional[float] = None) -> Dict[str, Any]:
    """Denoise a covariance by clipping sub-Marchenko-Pastur eigenvalues.

    Eigenvalues below the MP lower bound are collapsed to that bound (all noise
    eigenvalues set equal), so the cleaned matrix is the *systematic* component
    plus a flat idiosyncratic floor. Returns ``{"cov", "values", "n_clipped"}``.
    """
    n = len(cov or [])
    if n == 0:
        return {"ok": False, "cov": None, "n_clipped": 0}
    dec = eigen_decomposition(cov)
    vals, vecs = dec["values"], dec["vectors"]
    avg_var = sum(vals) / n
    mp = marchenko_pastur(n, n_obs, sigma2=sigma2 if sigma2 else avg_var)
    floor = max(mp["lambda_minus"], 1e-12)
    clipped = 0
    new_vals = []
    for v in vals:
        if v < floor:
            new_vals.append(floor)
            clipped += 1
        else:
            new_vals.append(v)
    out = _zeros(n, n)
    for k in range(n):
        vk = vecs[k]
        for i in range(n):
            for j in range(n):
                out[i][j] += new_vals[k] * vk[i] * vk[j]
    _symmetrise(out)
    return {"ok": True, "cov": out, "n_clipped": clipped,
            "floor": round(floor, 8), "mp": mp}


def variance_explained(cov: Sequence[Sequence[float]], *,
                       names: Optional[Sequence[str]] = None
                       ) -> List[Dict[str, Any]]:
    """Scree table: eigenvalue, % of total variance, cumulative %.

    This is what the "Optimiser / Factors" dashboard tab charts — it shows how
    many independent bets the universe really contains.
    """
    dec = eigen_decomposition(cov)
    if dec is None:
        return []
    vals = [max(v, 0.0) for v in dec["values"]]
    total = sum(vals) or 1.0
    rows: List[Dict[str, Any]] = []
    cum = 0.0
    for i, v in enumerate(vals):
        share = v / total
        cum += share
        rows.append({
            "factor": i + 1,
            "eigenvalue": round(v, 8),
            "pct": round(share * 100, 3),
            "cumulative_pct": round(cum * 100, 3),
        })
    return rows


def systematic_share(cov: Sequence[Sequence[float]], *, n_factors: int = 1
                     ) -> Optional[float]:
    """Fraction of total variance explained by the top ``n_factors``.

    The first principal component of a sleeve covariance is almost always the
    market factor; its share is the "how much is just beta?" answer.
    """
    dec = eigen_decomposition(cov)
    if dec is None:
        return None
    vals = [max(v, 0.0) for v in dec["values"]]
    total = sum(vals)
    if total <= 0:
        return None
    return round(sum(vals[:max(1, n_factors)]) / total, 6)


def decompose(cov: Sequence[Sequence[float]], *, n_obs: int,
              names: Optional[Sequence[str]] = None
              ) -> Dict[str, Any]:
    """One-call factor decomposition for the risk/optimiser report."""
    dec = eigen_decomposition(cov)
    if dec is None:
        return {"ok": False, "eigenvalues": [], "variance_explained": [],
                "systematic_share": None, "cleaned_cov": None}
    cleaned = clean_covariance(cov, n_obs=n_obs)
    return {
        "ok": True,
        "eigenvalues": dec["values"],
        "variance_explained": variance_explained(cov, names=names),
        "systematic_share": systematic_share(cov, n_factors=1),
        "n_significant": sum(1 for r in variance_explained(cov)
                             if r["eigenvalue"] > 0),
        "mp_band": marchenko_pastur(len(cov), n_obs),
        "cleaned_cov": cleaned.get("cov"),
        "n_clipped": cleaned.get("n_clipped", 0),
    }