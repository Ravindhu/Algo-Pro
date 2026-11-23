"""
ml/models/gradient_boosting.py — the sklearn boosted-trees candidate.

WHY GBM IS THE DEFAULT "SERIOUS" CANDIDATE
------------------------------------------
On tabular financial data (which is what daily bars produce), boosted decision
trees are almost always the strongest classical learner:

  * they handle non-linearities (RSI thresholds, volume spikes) with no feature
    engineering beyond what `ml/features/engineering.py` already builds,
  * they are robust to monotone feature transforms (so a unit change in a
    vendor column, the SchemaDrift scenario, degrades gracefully),
  * they fit interactions that a linear model cannot.

THE OVERFIT WARNING (read before tuning)
----------------------------------------
GBM's failure mode here is not "too weak" — it is "too willing". With 750 rows
(3 years of NIFTY daily bars) and default 100 trees, the model can memorise
noise and score beautifully in-sample. Hence the deliberately CONSERVATIVE
defaults: max_depth=2 (stumps+1), 80 estimators, learning_rate=0.05, and
min_samples_leaf=20. They are not performance-tuned; they are overfit-tuned.
The walk-forward folds are the judge — not the training score.

Availability: this module REQUIRES sklearn. When sklearn is missing it returns
``ok=False`` with a reason (no silent fallback) — the pure-Python ridge in
``ridge.py`` is the always-available baseline instead.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .. import logger
from .base import ModelSpec, has_sklearn

__all__ = ["build", "DEFAULT_PARAMS"]


#: Deliberately conservative — see module docstring. Recorded verbatim in the
#: manifest so a run is always reproducible.
DEFAULT_PARAMS: Dict[str, Any] = {
    "n_estimators": 80,
    "max_depth": 2,
    "learning_rate": 0.05,
    "min_samples_leaf": 20,
    "subsample": 0.8,
    "random_state": 0,
}


def build(**over: Any) -> ModelSpec:
    """Build the GradientBoostingClassifier candidate (sklearn required)."""
    params = {**DEFAULT_PARAMS, **{k: v for k, v in over.items()
                                   if k in DEFAULT_PARAMS}}
    if not has_sklearn():
        return ModelSpec(
            name="gbm", family="boosted_trees", ok=False,
            reason="scikit-learn not installed — install it, or use the "
                   "pure-Python ridge candidate instead",
            params=params)

    def _fit(X: Sequence[Sequence[float]], y: Sequence[Optional[int]]) -> Any:
        from sklearn.ensemble import GradientBoostingClassifier

        # Walk-forward guarantees labels; a None label here means the caller
        # built the matrix wrong, so fail LOUDLY rather than guessing.
        clean = [(xi, yi) for xi, yi in zip(X, y) if yi is not None]
        if len(clean) < 20:
            raise ValueError(f"gbm.fit: {len(clean)} usable rows < 20")
        Xc = [xi for xi, _ in clean]
        yc = [int(yi) for _, yi in clean]
        model = GradientBoostingClassifier(**params)
        model.fit(Xc, yc)
        return model

    def _predict(fitted: Any, X: Sequence[Sequence[float]]) -> List[float]:
        # predict_proba -> [n, 2]; we want the P(class=1) column.
        probs = fitted.predict_proba([list(map(float, r)) for r in X])
        return [float(p[1]) for p in probs]

    return ModelSpec(
        name="gbm", family="boosted_trees",
        fit_fn=_fit, predict_fn=_predict, params=params,
        n_params_estimate=params["n_estimators"] * (2 ** params["max_depth"]),
    )
