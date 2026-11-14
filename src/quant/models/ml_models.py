"""ML models wrapper: research-only ridge model and library status, DELEGATED to Blue_engine.

Flow stage: ML models
Status: PHASE 5 IMPLEMENTED (wrap; research only: never a live signal or an order)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/ml/models/ridge.py,
src/quant/blue_engine/ml/models/base.py

Every output here is marked ``research_only``. A model reaches the live path only through
the approval gate in the flow doc (human sign-off and the model registry), not from here.
Only the pure-Python ridge baseline is wrapped. The sklearn and torch candidates are
reported as available or not, and never built here.

FAIL-CLOSED RULES
-----------------
- Too little data or Blue unavailable: ``fit`` returns None. Nothing is predicted from it.
- Predictions are probabilities clamped to [0, 1] by Blue's logistic squash.
- Never raises.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["model_status", "fit_ridge", "predict_probability"]


def model_status() -> Dict[str, Any]:
    """Which optional libraries are installed. Never raises."""
    try:
        base = load("ml.models.base")
    except BlueEngineUnavailable:
        return {"ok": False, "sklearn": False, "torch": False, "research_only": True}
    try:
        return {"ok": True, "sklearn": bool(base.has_sklearn()),
                "torch": bool(base.has_torch()), "research_only": True}
    except Exception:  # noqa: BLE001
        return {"ok": False, "sklearn": False, "torch": False, "research_only": True}


def fit_ridge(X: Sequence[Sequence[float]], y: Sequence[Optional[int]],
              ridge_lambda: float = 1.0) -> Optional[Dict[str, Any]]:
    """Fit the research baseline. None when there is too little data or Blue is missing."""
    try:
        mod = load("ml.models.ridge")
    except BlueEngineUnavailable:
        return None
    try:
        fitted = mod.fit_ridge(X, y, ridge_lambda=ridge_lambda)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(fitted, dict):
        return None
    return {**fitted, "research_only": True}


def predict_probability(fitted: Optional[Dict[str, Any]],
                        X: Sequence[Sequence[float]]) -> Optional[List[float]]:
    """Probabilities in [0, 1] for ``X``. None when there is no fitted model."""
    if not isinstance(fitted, dict):
        return None
    try:
        mod = load("ml.models.ridge")
    except BlueEngineUnavailable:
        return None
    try:
        out = [float(p) for p in mod.ridge_predict(fitted, X)]
    except Exception:  # noqa: BLE001
        return None
    if any(not (0.0 <= p <= 1.0) for p in out):
        return None
    return out
