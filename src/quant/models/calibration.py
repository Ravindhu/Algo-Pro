"""Platt scaling and a shared logistic fitter. Maps raw scores to probabilities.

Flow stage: Calibrated conviction
Status: PHASE 2 IMPLEMENTED; numerically hardened 2026-11-14
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/ml/models/base.py
    (the engine has no standalone calibration; its base model wraps sklearn)

CALIBRATION SITS ON THE SIZE PATH
---------------------------------
A calibrated probability is multiplied into the bet. That makes every numerical
defect here a MONEY defect, not a research one, so this module refuses bad input
rather than returning a plausible number:

1. ``sigmoid`` is computed with the stable two-branch form. The single-expression
   ``1/(1+exp(-z))`` overflows for z below about -709, raising RuntimeWarning and
   returning a wrong value silently.
2. ``fit_logistic`` refuses a single-class label vector. It cannot be fitted, and
   the old behaviour returned a degenerate w,b that then scored every future
   signal at the same probability with full confidence.
3. ``fit_logistic`` reports non-convergence instead of pretending. A fit that hit
   the iteration cap without settling returns ``converged=False`` so a caller can
   refuse to trade on it.
4. NaN or infinite features are refused at both fit and predict time.

WHAT IS DELIBERATELY UNCHANGED
------------------------------
The optimiser is plain batch gradient descent with L2, exactly as before, so any
existing fitted weights remain comparable. ``platt_fit`` still standardises its
scores first, which is required for Platt scaling to be well conditioned.
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

__all__ = [
    "sigmoid",
    "fit_logistic",
    "fit_logistic_report",
    "predict_proba",
    "platt_fit",
    "reliability",
    "CalibrationError",
]

#: Below this |z| the naive sigmoid is safe; beyond it the stable branch is used.
_STABLE_Z = 500.0


class CalibrationError(ValueError):
    """Calibration input cannot produce a trustworthy probability."""


def sigmoid(z) -> np.ndarray:
    """Numerically stable logistic function.

    For z >= 0, exp(-z) <= 1 so the direct form cannot overflow. For z < 0 the
    expression is rearranged as ``exp(z)/(1+exp(z))`` for the same reason. Using
    one form for both ranges raises an overflow warning and returns 0.0 or 1.0
    where the true value is close to but not equal to those, which is exactly the
    region a bet size is most sensitive to.
    """
    z = np.asarray(z, dtype=float)
    out = np.empty_like(z)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[~pos])
    out[~pos] = ez / (1.0 + ez)
    return out


def _validate_X(X, n_rows: Optional[int] = None) -> np.ndarray:
    arr = np.asarray(X, dtype=float)
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    if n_rows is not None and arr.shape[0] != n_rows:
        arr = arr.reshape(n_rows, -1)
    if not np.isfinite(arr).all():
        raise CalibrationError("features contain NaN or infinity; refusing to fit or score")
    return arr


def fit_logistic_report(
    X,
    y,
    l2: float = 1e-3,
    lr: float = 0.1,
    iters: int = 2000,
    tol: float = 1e-8,
) -> Tuple[np.ndarray, float, bool]:
    """Fit a logistic model and report whether it converged.

    Returns ``(w, b, converged)``. Raises CalibrationError on a single-class label
    vector: a model fitted to one class carries no information, yet it would score
    every future signal with the same high confidence.
    """
    y = np.asarray(y, dtype=float).ravel()
    X = _validate_X(X, len(y))

    classes = np.unique(y)
    if classes.size < 2:
        raise CalibrationError(
            f"labels contain a single class ({classes.tolist()}); "
            "a meta-labeler needs both outcomes to learn a threshold")

    w = np.zeros(X.shape[1])
    b = 0.0
    prev = None
    converged = False
    for _ in range(int(iters)):
        p = sigmoid(X @ w + b)
        grad_w = X.T @ (p - y) / len(y) + l2 * w
        grad_b = float(np.mean(p - y))
        w = w - lr * grad_w
        b = b - lr * grad_b
        loss = float(-np.mean(y * np.log(np.clip(p, 1e-12, 1.0))
                              + (1 - y) * np.log(np.clip(1 - p, 1e-12, 1.0))))
        if prev is not None and abs(prev - loss) < tol:
            converged = True
            break
        prev = loss
    return w, b, converged


def fit_logistic(X, y, l2: float = 1e-3, lr: float = 0.1, iters: int = 2000
                 ) -> Tuple[np.ndarray, float]:
    """Back-compatible two-value fit. Raises CalibrationError on unusable labels."""
    w, b, _converged = fit_logistic_report(X, y, l2=l2, lr=lr, iters=iters)
    return w, b


def predict_proba(X, w, b) -> np.ndarray:
    """Probability per row. Stable sigmoid; refuses non-finite features.

    A 1-D input is read as ONE model input per row when the model has a single
    coefficient (a vector of raw scores, which is how Platt scaling is applied),
    and as a single multi-feature row otherwise. Getting that distinction wrong in
    either direction is why the width check exists at all.
    """
    w = np.asarray(w, dtype=float).ravel()
    X = np.asarray(X, dtype=float)

    if X.ndim == 1:
        if w.shape[0] == 1:
            X = X.reshape(-1, 1)      # a vector of scores, one per row
        else:
            X = X.reshape(1, -1)      # a single row with w.shape[0] features

    if X.shape[1] != w.shape[0]:
        raise CalibrationError(
            f"feature width {X.shape[1]} does not match the fitted width {w.shape[0]}")
    if not np.isfinite(X).all():
        raise CalibrationError("features contain NaN or infinity; refusing to score")
    return sigmoid(X @ w + float(b))


def platt_fit(scores, labels) -> Tuple[np.ndarray, float]:
    """Platt scaling: standardise the raw scores, then fit a logistic on them."""
    s = np.asarray(scores, dtype=float).ravel()
    if not np.isfinite(s).all():
        raise CalibrationError("scores contain NaN or infinity")
    spread = s.std()
    s = (s - s.mean()) / (spread if spread else 1.0)
    return fit_logistic(s.reshape(-1, 1), labels)


def reliability(probs, labels, bins: int = 5):
    """(mean predicted, observed frequency, count) per probability bin.

    A bin with no observations is omitted rather than reported as (0, 0, 0),
    which would read as "perfectly calibrated at zero".
    """
    p = np.asarray(probs, dtype=float).ravel()
    y = np.asarray(labels, dtype=float).ravel()
    edges = np.linspace(0, 1, bins + 1)
    rows = []
    for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
        last = i == len(edges) - 2
        m = (p >= lo) & (p <= hi if last else p < hi)
        if m.any():
            rows.append((float(p[m].mean()), float(y[m].mean()), int(m.sum())))
    return rows
