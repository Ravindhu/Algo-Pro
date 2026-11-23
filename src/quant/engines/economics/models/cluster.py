"""
models/cluster.py - unsupervised macro-state discovery (k-means, numpy-only).

The HMM in :mod:`.hmm_regime` is *temporal* (it models persistence). This module
is *spatial*: it groups observations by similarity in macro-feature space, a
label-free way to discover "what kind of environment is this?" Clusters are
labelled post-hoc by their mean risk-on score, mirroring the HMM's convention.

Pure-``numpy`` k-means (deterministic seeding) so it needs no sklearn. Degrades
to ``method='unavailable'`` without numpy.
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
    clean = [[safe_float(v) for v in row] for row in rows]
    clean = [r for r in clean if all(v is not None for v in r)]
    if not clean:
        return None
    return np.asarray(clean, dtype=float)  # type: ignore[union-attr]


def kmeans(features: Sequence[Sequence[float]], *, n_clusters: int = 3,
           n_iter: int = 50, tol: float = 1e-6) -> Dict[str, Any]:
    """Deterministic k-means (quantile seeding, no RNG).

    Returns cluster assignments, centroids and inertia. Deterministic seeding
    (k-means++-style from sorted quantiles) means the same input always yields
    the same clusters - a requirement for reproducible reports.
    """
    if not _HAS_NUMPY:
        return {"method": "unavailable", "labels": None, "centroids": None}
    X = _matrix(features)
    if X is None or X.shape[0] < n_clusters:
        return {"method": "insufficient_data", "labels": None, "centroids": None}

    # Deterministic seed: evenly spaced rows in sorted order of feature 0.
    order = np.argsort(X[:, 0])
    idx = np.linspace(0, len(order) - 1, n_clusters).astype(int)
    centroids = X[order[idx]].copy()

    labels = np.zeros(X.shape[0], dtype=int)
    for _ in range(n_iter):
        # Assign.
        d = ((X[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2)
        new_labels = d.argmin(axis=1)
        # Update.
        new_centroids = centroids.copy()
        for k in range(n_clusters):
            members = X[new_labels == k]
            if len(members) > 0:
                new_centroids[k] = members.mean(axis=0)
        shift = float(np.linalg.norm(new_centroids - centroids))
        labels = new_labels
        centroids = new_centroids
        if shift < tol:
            break
    inertia = float(((X - centroids[labels]) ** 2).sum())
    return {"method": "kmeans", "labels": labels.tolist(),
            "centroids": centroids.tolist(), "inertia": round(inertia, 6),
            "n_clusters": n_clusters}


def cluster_states(features: Sequence[Sequence[float]], *,
                   score_column: int = 0, n_clusters: int = 3
                   ) -> Dict[str, Any]:
    """Cluster observations and label the current one by regime semantics.

    ``score_column`` is which feature (column) carries the risk-on score, used
    to rank clusters into RISK_OFF / NEUTRAL / RISK_ON. The last row of
    ``features`` is assumed to be "now".
    """
    res = kmeans(features, n_clusters=n_clusters)
    if res.get("method") != "kmeans":
        return {"method": res.get("method"), "label": None,
                "probabilities": {}, "cluster": None}

    centroids = np.asarray(res["centroids"])
    score_means = centroids[:, score_column]
    order = list(np.argsort(score_means))
    labels_map = {}
    if n_clusters == 2:
        labels_map = {order[0]: "RISK_OFF", order[1]: "RISK_ON"}
    else:
        labels_map = {order[0]: "RISK_OFF", order[-1]: "RISK_ON"}
        for mid in order[1:-1]:
            labels_map[int(mid)] = "NEUTRAL"

    current = res["labels"][-1]
    counts: Dict[str, int] = {}
    for lab in res["labels"]:
        lab_name = labels_map.get(lab, "NEUTRAL")
        counts[lab_name] = counts.get(lab_name, 0) + 1
    total = sum(counts.values()) or 1
    probabilities = {k: round(v / total, 4) for k, v in counts.items()}

    return {"method": "kmeans", "cluster": current,
            "label": labels_map.get(current, "NEUTRAL"),
            "probabilities": probabilities,
            "inertia": res["inertia"],
            "state_means": [round(float(m), 6) for m in score_means]}


def available() -> bool:
    return _HAS_NUMPY


__all__ = ["kmeans", "cluster_states", "available"]