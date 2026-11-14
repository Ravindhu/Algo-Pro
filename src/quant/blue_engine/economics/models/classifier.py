"""
models/classifier.py - supervised macro->market classifier with walk-forward CV.

The baseline engine uses **hand-set weights** (weakness #2). This module fits a
model that predicts the *sign of the forward N-day index return* from macro
features, and - critically - validates it the only way that is honest for
financial time series: **walk-forward cross-validation** (train on a trailing
window, test on the next block, roll forward). A single full-sample fit would
leak the future and overstate skill.

Dependencies are optional:
    * numpy            -> single-feature logistic regression (pure implementation)
    * scikit-learn     -> LogisticRegression / GradientBoosting (preferred)
    * pandas           -> convenient frame handling (optional)
If none are present, :func:`walk_forward` returns ``method='unavailable'`` and
the engine keeps the hand-set weights.

Anti-look-ahead (see plan section 9)
------------------------------------
    * features are aligned to observation time ``t``;
    * the label is the forward return from ``t`` to ``t+horizon``;
    * the last ``horizon`` rows are dropped (their label is unknown);
    * no shuffling - folds are strictly chronological.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ...core.results import mean, safe_float

try:  # optional
    import numpy as np  # type: ignore
    _HAS_NUMPY = True
except Exception:  # noqa: BLE001
    np = None  # type: ignore
    _HAS_NUMPY = False

try:  # optional, preferred
    from sklearn.linear_model import LogisticRegression  # type: ignore
    from sklearn.ensemble import GradientBoostingClassifier  # type: ignore
    from sklearn.metrics import accuracy_score, roc_auc_score  # type: ignore
    _HAS_SKLEARN = True
except Exception:  # noqa: BLE001
    LogisticRegression = GradientBoostingClassifier = None  # type: ignore
    accuracy_score = roc_auc_score = None  # type: ignore
    _HAS_SKLEARN = False


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #
def forward_return_labels(prices: Sequence[float], *, horizon: int = 20
                          ) -> List[Optional[int]]:
    """Sign of the forward ``horizon``-day return for each point.

    Returns a list the same length as ``prices``; the last ``horizon`` entries
    are ``None`` (their future is unknown -> no label, no leakage).
    """
    clean = [safe_float(p) for p in prices]
    n = len(clean)
    labels: List[Optional[int]] = [None] * n
    for i in range(n - horizon):
        p0, p1 = clean[i], clean[i + horizon]
        if p0 is None or p1 is None or p0 == 0:
            continue
        labels[i] = 1 if (p1 - p0) > 0 else 0
    return labels


# --------------------------------------------------------------------------- #
# Pure logistic regression (numpy) - fallback when sklearn is absent
# --------------------------------------------------------------------------- #
class _NumpyLogistic:
    """Tiny batch-gradient logistic regression (L2). Not production-grade; a
    dependency-free fallback so the walk-forward harness still runs."""

    def __init__(self, lr: float = 0.1, epochs: int = 200, l2: float = 1e-3):
        self.lr, self.epochs, self.l2 = lr, epochs, l2
        self.w = None

    def fit(self, X: Any, y: Any) -> "_NumpyLogistic":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        n, d = X.shape
        self.w = np.zeros(d)
        for _ in range(self.epochs):
            z = X @ self.w
            p = 1.0 / (1.0 + np.exp(-z))
            grad = X.T @ (p - y) / n + self.l2 * self.w
            self.w -= self.lr * grad
        return self

    def predict_proba(self, X: Any) -> Any:
        z = np.asarray(X, dtype=float) @ self.w
        p = 1.0 / (1.0 + np.exp(-z))
        return np.vstack([1 - p, p]).T

    def predict(self, X: Any) -> Any:
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


def _make_model(kind: str):
    """Instantiate a classifier, preferring sklearn when available."""
    if _HAS_SKLEARN:
        if kind == "gbm":
            return GradientBoostingClassifier(n_estimators=100, max_depth=2,
                                              random_state=0)
        return LogisticRegression(max_iter=1000, C=1.0)
    if _HAS_NUMPY:
        return _NumpyLogistic()
    return None


# --------------------------------------------------------------------------- #
# Walk-forward harness
# --------------------------------------------------------------------------- #
def walk_forward(features: Sequence[Sequence[float]],
                 labels: Sequence[Optional[int]],
                 *, train_size: int = 250, test_size: int = 50,
                 kind: str = "logit") -> Dict[str, Any]:
    """Chronological walk-forward evaluation of a macro classifier.

    Parameters
    ----------
    features : T x D rows, aligned to time.
    labels   : length T, ``None`` where the forward return is unknown.
    train_size / test_size : fold geometry (rows).
    kind : 'logit' or 'gbm'.

    Returns aggregate accuracy / AUC over all out-of-sample folds, plus the
    per-fold detail and **feature coefficients/importances** the desk can read.
    Never raises; returns ``method='unavailable'`` without numpy/sklearn.
    """
    if not _HAS_NUMPY:
        return {"method": "unavailable", "accuracy": None, "auc": None}
    model_proto = _make_model(kind)
    if model_proto is None:
        return {"method": "unavailable", "accuracy": None, "auc": None}

    X = np.asarray([[safe_float(v) for v in row] for row in features],
                   dtype=float)
    y = np.asarray([(-1 if v is None else int(v)) for v in labels], dtype=int)
    T = X.shape[0]
    if T < train_size + test_size:
        return {"method": "insufficient_data", "accuracy": None, "auc": None,
                "n": T}

    folds: List[Dict[str, Any]] = []
    all_true: List[int] = []
    all_pred: List[int] = []
    all_score: List[float] = []

    start = 0
    while start + train_size + test_size <= T:
        tr = slice(start, start + train_size)
        te = slice(start + train_size, start + train_size + test_size)
        # Drop rows with unknown label from train and test.
        tr_idx = [i for i in range(tr.start, tr.stop) if y[i] >= 0]
        te_idx = [i for i in range(te.start, te.stop) if y[i] >= 0]
        start += test_size
        if len(tr_idx) < 20 or len(te_idx) < 5:
            continue
        Xtr, ytr = X[tr_idx], y[tr_idx]
        Xte, yte = X[te_idx], y[te_idx]
        if len(set(ytr.tolist())) < 2:
            continue
        model = _make_model(kind)
        try:
            model.fit(Xtr, ytr)
            pred = model.predict(Xte)
            proba = model.predict_proba(Xte)[:, 1]
        except Exception as exc:  # noqa: BLE001
            folds.append({"error": repr(exc), "test": [te.start, te.stop]})
            continue
        acc = float((pred == yte).mean())
        auc = _safe_auc(yte, proba)
        folds.append({"test": [int(te.start), int(te.stop)],
                      "n_test": int(len(te_idx)),
                      "accuracy": round(acc, 4),
                      "auc": None if auc is None else round(auc, 4)})
        all_true.extend(yte.tolist())
        all_pred.extend(pred.tolist())
        all_score.extend(proba.tolist())

    if not folds:
        return {"method": "no_folds", "accuracy": None, "auc": None}

    agg_acc = float(sum(t == p for t, p in zip(all_true, all_pred))
                    / max(1, len(all_true)))
    agg_auc = _safe_auc(np.asarray(all_true), np.asarray(all_score))

    # Final in-sample-free coefficients from a model on the most recent train
    # window (for desk interpretation only, NOT for the accuracy figure).
    coefficients = _final_coefficients(X, y, train_size, kind)

    return {
        "method": "walk_forward",
        "model": kind,
        "accuracy": round(agg_acc, 4),
        "auc": None if agg_auc is None else round(agg_auc, 4),
        "n_folds": len(folds),
        "n_oos": len(all_true),
        "folds": folds,
        "coefficients": coefficients,
    }


def _safe_auc(y_true: Any, y_score: Any) -> Optional[float]:
    try:
        if len(set(np.asarray(y_true).tolist())) < 2:
            return None
    except Exception:  # noqa: BLE001
        return None
    try:
        if _HAS_SKLEARN:
            return float(roc_auc_score(y_true, y_score))
        return _numpy_auc(np.asarray(y_true), np.asarray(y_score))
    except Exception:  # noqa: BLE001
        return None


def _numpy_auc(y_true: Any, y_score: Any) -> Optional[float]:
    """Rank-based AUC (Mann-Whitney U) without sklearn."""
    pos = y_score[y_true == 1]
    neg = y_score[y_true == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    order = np.argsort(np.concatenate([pos, neg]))
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(order) + 1)
    rank_pos = ranks[:len(pos)].sum()
    auc = (rank_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))
    return float(auc)


def _final_coefficients(X: Any, y: Any, train_size: int, kind: str
                        ) -> Optional[List[float]]:
    idx = [i for i in range(len(y)) if y[i] >= 0]
    if len(idx) < 20:
        return None
    tail = idx[-train_size:] if len(idx) > train_size else idx
    model = _make_model(kind)
    if model is None:
        return None
    try:
        model.fit(X[tail], y[tail])
        if hasattr(model, "coef_"):
            coef = np.asarray(model.coef_).ravel()
            return [round(float(c), 6) for c in coef]
        if hasattr(model, "feature_importances_"):
            imp = np.asarray(model.feature_importances_).ravel()
            return [round(float(c), 6) for c in imp]
    except Exception:  # noqa: BLE001
        return None
    return None


def available() -> Dict[str, bool]:
    return {"numpy": _HAS_NUMPY, "sklearn": _HAS_SKLEARN}


__all__ = ["walk_forward", "forward_return_labels", "available"]