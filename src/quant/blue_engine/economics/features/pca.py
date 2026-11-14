"""
features/pca.py - de-collinearise the macro block (factor extraction).

The nine macro signals are heavily correlated (GDP/employment co-move; debt/FX
reserves reflect the same external position). Feeding all nine to a model
inflates variance and destabilises fitted weights. This module reduces them to
a few orthogonal **factors** via PCA.

Unlike the other feature modules this one *needs* numeric linear algebra, so it
uses ``numpy`` when available and degrades to ``method='unavailable'`` (the
engine then keeps the raw signals). Pure: no I/O, no randomness (signs are
canonicalised for reproducibility).

Explained-variance output lets the desk see how much structure a single "risk
factor" carries - a useful sanity check that the composite is not noise.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ...core.results import safe_float

try:  # optional dependency
    import numpy as np  # type: ignore
    _HAS_NUMPY = True
except Exception:  # noqa: BLE001
    np = None  # type: ignore
    _HAS_NUMPY = False


def _matrix(rows: Sequence[Sequence[float]]) -> Optional[Any]:
    """Build a float matrix with no NaNs (rows with any NaN are dropped)."""
    clean = [[safe_float(v) for v in row] for row in rows]
    clean = [r for r in clean if all(v is not None for v in r)]
    if not clean:
        return None
    return np.asarray(clean, dtype=float)  # type: ignore[union-attr]


def pca(rows: Sequence[Sequence[float]], n_components: int = 2
        ) -> Dict[str, Any]:
    """Principal component analysis via SVD (no sklearn required).

    Parameters
    ----------
    rows : observations x features (each row one time-point, each column one
           macro signal - already standardised or not; we centre internally).
    n_components : number of factors to return.

    Returns::

        {"method": "pca"|"unavailable", "loadings": [[...]], "scores": [[...]],
         "explained_variance_ratio": [...], "n_features": int}
    """
    if not _HAS_NUMPY:
        return {"method": "unavailable", "loadings": None, "scores": None,
                "explained_variance_ratio": None}
    X = _matrix(rows)
    if X is None or X.shape[0] < 2:
        return {"method": "insufficient_data", "loadings": None, "scores": None,
                "explained_variance_ratio": None}

    Xc = X - X.mean(axis=0, keepdims=True)
    # SVD: Xc = U S Vt ; columns of Vt are the principal directions.
    U, S, Vt = np.linalg.svd(Xc, full_matrices=False)
    var = (S ** 2) / max(1, Xc.shape[0] - 1)
    total = float(var.sum()) or 1.0
    ratio = (var / total).tolist()
    k = min(n_components, Vt.shape[0])

    loadings = Vt[:k].tolist()
    scores = (Xc @ Vt[:k].T).tolist()

    # Canonicalise sign (largest-magnitude entry of each component positive)
    # so repeated runs are byte-identical and reports are reproducible.
    for i, comp in enumerate(loadings):
        j = max(range(len(comp)), key=lambda t: abs(comp[t]))
        if comp[j] < 0:
            loadings[i] = [-v for v in comp]
            scores_i = [row[i] for row in scores]
            for r in range(len(scores)):
                scores[r][i] = -scores_i[r]

    return {"method": "pca", "loadings": loadings, "scores": scores,
            "explained_variance_ratio": [round(r, 6) for r in ratio[:k]],
            "n_features": int(X.shape[1])}


def first_factor_value(rows: Sequence[Sequence[float]]) -> Dict[str, Any]:
    """Convenience: the latest first-factor score + explained variance.

    A common desk read: if PC1 explains, say, >45% of variance and its latest
    score is strongly positive, the macro block is sending a *coherent* signal;
    a low explained-variance ratio means the signals are disagreeing.
    """
    res = pca(rows, n_components=1)
    if res.get("method") != "pca" or not res.get("scores"):
        return {"method": res.get("method"), "value": None,
                "explained_variance_ratio": None}
    return {"method": "pca",
            "value": round(res["scores"][-1][0], 6),
            "explained_variance_ratio": res["explained_variance_ratio"][0]}


def available() -> bool:
    return _HAS_NUMPY


__all__ = ["pca", "first_factor_value", "available"]