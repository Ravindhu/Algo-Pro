"""
ml/models/ridge.py — the always-available baseline (pure Python, no deps).

WHY RIDGE IS NOT OPTIONAL
-------------------------
Every fancier model must be compared against something. A gradient-boosting
model that beats a ridge baseline by 0.001 skill has not earned its complexity;
one that beats it by 0.03 has. Without a cheap, unkillable baseline you cannot
make that call — so ridge is implemented in pure Python and runs even on a
machine with no sklearn, no torch and no network.

THE MATHS (open, inspectable, verifiable)
-----------------------------------------
Ridge solves::

    minimise  ||y - Xb||^2  +  lambda * ||b||^2

with the intercept absorbed by centering. Closed form::

    b = (X'X + lambda*I)^-1  X'y

solved here with Gaussian elimination (pure Python; p is ~15 features, so the
cost is trivial and it removes the numpy dependency).

WHY RIDGE AND NOT OLS
---------------------
OLS is a special case (lambda=0). With p small and rows possibly near-collinear
(momentum and mean-reversion features correlate), OLS coefficients can explode
to ±1000 and the model becomes noise. The lambda (default 1.0) shrinks exactly
the kind of overfit that kills small-sample trading models.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import logger
from .base import ModelSpec

__all__ = ["build", "fit_ridge", "ridge_predict"]


def _transpose(X: Sequence[Sequence[float]]) -> List[List[float]]:
    return [list(col) for col in zip(*X)]


def _matmul(A: List[List[float]], B: List[List[float]]) -> List[List[float]]:
    Bt = _transpose(B)
    return [[sum(a * b for a, b in zip(row, col)) for col in Bt] for row in A]


def _solve_linear(A: List[List[float]], b: List[float]) -> Optional[List[float]]:
    """Gaussian elimination with partial pivoting. None when singular."""
    n = len(A)
    if n == 0 or any(len(r) != n for r in A):
        return None
    M = [list(row) + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            return None
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        for r in range(n):
            if r == col:
                continue
            f = M[r][col] / pv
            if f == 0.0:
                continue
            for c in range(col, n + 1):
                M[r][c] -= f * M[col][c]
    return [M[i][n] / M[i][i] for i in range(n)]


def fit_ridge(X: Sequence[Sequence[float]], y: Sequence[Optional[int]], *,
              ridge_lambda: float = 1.0) -> Optional[Dict[str, Any]]:
    """Fit ridge on (X, y). Returns a plain dict (coefficients + centre).

    Returns None when there is not enough data — the CALLER records that
    rather than training a model on 5 rows.
    """
    clean, dropped = [], 0
    for xi, yi in zip(X, y):
        if yi is None:
            continue
        try:
            vals = [float(v) for v in xi]
            if all(v == v and abs(v) != float("inf") for v in vals):
                clean.append((vals, float(yi)))
                continue
        except (TypeError, ValueError):
            pass
        dropped += 1
    if dropped:
        logger.debug("ridge: dropped %d unusable rows", dropped)
    n = len(clean)
    if n < 20 or not clean[0][0]:
        return None
    p = len(clean[0][0])

    # Center so the intercept is implicit (the mean of y).
    centre = [sum(row[i] for row, _ in clean) / n for i in range(p)]
    y_mu = sum(t for _, t in clean) / n
    Xc = [[v - c for v, c in zip(row, centre)] for row, _ in clean]
    yc = [t - y_mu for _, t in clean]

    XtX = _matmul(_transpose(Xc), Xc)
    Xty = [sum(a * b for a, b in zip(col, yc)) for col in _transpose(Xc)]
    # Add lambda to the diagonal AFTER a final column of ones is absent — the
    # intercept is handled by centring, so lambda does not shrink it.
    A = [[XtX[i][j] + (ridge_lambda if i == j else 0.0) for j in range(p)]
         for i in range(p)]
    beta = _solve_linear(A, Xty)
    if beta is None:
        logger.warning("ridge: singular system (p=%d, n=%d); try larger lambda",
                       p, n)
        return None
    return {"beta": beta, "centre": centre, "y_mu": y_mu, "n": n,
            "ridge_lambda": ridge_lambda}


def ridge_predict(fitted: Dict[str, Any], X: Sequence[Sequence[float]]
                  ) -> List[float]:
    """Predict a PROBABILITY in [0, 1] via a logistic squash of the linear score.

    Why logistic and not raw output: the walk-forward runner's contract is
    "probabilities", and downstream (position sizing, calibration) assumes
    [0, 1]. The squash is monotonic, so ranking (AUC) is unchanged — it only
    rescales the numbers into probability-like units.
    """
    beta, centre, y_mu = fitted["beta"], fitted["centre"], fitted["y_mu"]
    out: List[float] = []
    for row in X:
        try:
            score = sum(b * (float(v) - c) for b, v, c in zip(beta, row, centre)) + y_mu
        except (TypeError, ValueError):
            score = y_mu                      # unknown row -> predict the base rate
        # logistic squash into [0, 1]
        p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, score))))
        out.append(p)
    return out


def build(*, ridge_lambda: float = 1.0, **_ignored: Any) -> ModelSpec:
    """Build the pure-Python ridge candidate."""
    return ModelSpec(
        name=f"ridge_l{ridge_lambda:g}", family="linear",
        fit_fn=lambda X, y: fit_ridge(X, y, ridge_lambda=ridge_lambda),
        predict_fn=ridge_predict,
        params={"ridge_lambda": ridge_lambda},
        n_params_estimate=15,
    )
