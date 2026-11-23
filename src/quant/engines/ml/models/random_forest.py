"""
ml/models/random_forest.py — the sklearn bagged-trees candidate.

WHY KEEP RF WHEN GBM IS USUALLY BETTER
--------------------------------------
Because they fail DIFFERENTLY. GBM reduces bias by fitting trees sequentially
and can overfit a small sample into confident nonsense. RF reduces variance by
averaging deep trees — it is much harder to make it overfit, but it can
underfit an interaction. In a candidate search, keeping both families means an
"both score the same" result is itself information (the signal is linear-ish),
whereas keeping only one leaves that unknown.

DEFAULTS — deep trees, MANY of them (the opposite of GBM, on purpose)
---------------------------------------------------------------------
RF wants *unbiased, high-variance* trees averaged away: max_depth=None with
min_samples_leaf=20 (the leaf floor is the overfit brake, not the depth), and
100 trees. Setting max_depth=5 here (a common beginner tune) would break the
bagging logic and produce a worse model than GBM while looking like "RF is bad".

Availability: sklearn required; ``ok=False`` with a reason when missing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .base import ModelSpec, has_sklearn

__all__ = ["build", "DEFAULT_PARAMS"]


DEFAULT_PARAMS: Dict[str, Any] = {
    "n_estimators": 100,
    "max_depth": None,
    "min_samples_leaf": 20,
    "max_features": "sqrt",
    "random_state": 0,
    "n_jobs": 1,        # reproducible + parallelism happens at the fold level
}


def build(**over: Any) -> ModelSpec:
    params = {**DEFAULT_PARAMS, **{k: v for k, v in over.items()
                                   if k in DEFAULT_PARAMS}}
    if not has_sklearn():
        return ModelSpec(
            name="rf", family="bagged_trees", ok=False,
            reason="scikit-learn not installed — install it, or use the "
                   "pure-Python ridge candidate instead",
            params=params)

    def _fit(X: Sequence[Sequence[float]], y: Sequence[Optional[int]]) -> Any:
        from sklearn.ensemble import RandomForestClassifier

        clean = [(xi, yi) for xi, yi in zip(X, y) if yi is not None]
        if len(clean) < 20:
            raise ValueError(f"rf.fit: {len(clean)} usable rows < 20")
        model = RandomForestClassifier(**params)
        model.fit([xi for xi, _ in clean], [int(yi) for _, yi in clean])
        return model

    def _predict(fitted: Any, X: Sequence[Sequence[float]]) -> List[float]:
        probs = fitted.predict_proba([list(map(float, r)) for r in X])
        return [float(p[1]) for p in probs]

    return ModelSpec(
        name="rf", family="bagged_trees",
        fit_fn=_fit, predict_fn=_predict, params=params,
        n_params_estimate=params["n_estimators"],
    )
