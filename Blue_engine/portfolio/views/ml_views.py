"""
views/ml_views.py - supervised ML forecasts -> Black-Litterman views (pure).

Turns a history of sleeve returns into a forward-looking expected-return view per
sleeve. Two engines, chosen automatically:

* **Gradient boosting / RandomForest** via scikit-learn, when installed — learns
  a non-linear mapping from lagged features to the forward return sign/magnitude.
* **Pure-Python ridge regression** (normal equations) as the always-available
  fallback — a linear model on the same features, no dependency.

Both are validated with **walk-forward** (never a single in-sample fit) — this is
non-negotiable per the plan (§9). The out-of-sample hit-rate becomes the view
``confidence``; the predicted return becomes ``view_return``.

Features (all computed from past data only, avoiding look-ahead):
    momentum_5, momentum_20, vol_20, zscore_20, reversal_1
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from .base import make_view
from ...core.results import clamp, mean, safe_float, stdev

logger = logging.getLogger(__name__)

__all__ = ["build_features", "forecast_sleeve", "views_from_history",
           "walk_forward_hit_rate"]


# --------------------------------------------------------------------------- #
# Feature engineering (past-only)
# --------------------------------------------------------------------------- #

def build_features(returns: Sequence[float]) -> List[List[float]]:
    """Feature rows for each time ``t`` using only data up to ``t``.

    Row ``t`` = ``[mom5, mom20, vol20, z20, rev1]`` computed from ``returns[:t+1]``.
    Early rows (insufficient history) are skipped.
    """
    rs = [safe_float(r) for r in (returns or [])]
    rs = [r for r in rs if r is not None]
    rows: List[List[float]] = []
    for t in range(20, len(rs)):
        hist = rs[:t]
        mom5 = sum(hist[-5:]) if len(hist) >= 5 else 0.0
        mom20 = sum(hist[-20:]) if len(hist) >= 20 else 0.0
        vol20 = stdev(hist[-20:]) or 0.0
        m = mean(hist[-20:]) or 0.0
        sd = stdev(hist[-20:]) or 0.0
        z20 = ((hist[-1] - m) / sd) if sd > 0 else 0.0
        rev1 = -hist[-1]
        rows.append([mom5, mom20, vol20, z20, rev1])
    return rows


def _forward_targets(returns: Sequence[float], *, horizon: int = 20
                     ) -> List[float]:
    """Forward ``horizon``-day return at each ``t`` (the label)."""
    rs = [safe_float(r) for r in (returns or [])]
    rs = [r for r in rs if r is not None]
    out: List[float] = []
    for t in range(20, len(rs)):
        fwd = rs[t:t + horizon]
        prod = 1.0
        for r in fwd:
            prod *= (1.0 + r)
        out.append(prod - 1.0 if fwd else 0.0)
    return out


def _ridge_fit(X: List[List[float]], y: List[float], *, ridge: float = 1e-4
               ) -> Optional[List[float]]:
    """Ridge regression via the normal equations (pure; no numpy)."""
    n = len(X)
    if n < 2 or not X:
        return None
    p = len(X[0]) + 1  # + intercept
    xtx = [[0.0] * p for _ in range(p)]
    xty = [0.0] * p
    for k in range(n):
        row = [1.0] + X[k]
        for i in range(p):
            xty[i] += row[i] * y[k]
            for j in range(p):
                xtx[i][j] += row[i] * row[j]
    for i in range(p):
        xtx[i][i] += ridge
    from ..risk.beta import _solve
    return _solve(xtx, xty)


def _ridge_predict(beta: List[float], x: List[float]) -> float:
    return beta[0] + sum(beta[i + 1] * x[i] for i in range(len(x)))


def _has_sklearn() -> bool:
    try:
        import sklearn  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def walk_forward_hit_rate(returns: Sequence[float], *, horizon: int = 20,
                          min_train: int = 40) -> Dict[str, Any]:
    """Out-of-sample hit-rate of the return-sign forecast (walk-forward).

    The honest skill measure: train on ``[0, t)``, predict ``t``, roll forward.
    A hit-rate meaningfully above 0.5 is the only evidence a view deserves
    confidence; near 0.5 the view confidence collapses to the floor.
    """
    rs = [safe_float(r) for r in (returns or [])]
    rs = [r for r in rs if r is not None]
    X = build_features(rs)
    y = _forward_targets(rs, horizon=horizon)
    n = min(len(X), len(y))
    if n < min_train + 5:
        return {"ok": False, "hit_rate": None, "n": n,
                "warnings": ["insufficient history for walk-forward"]}
    hits = 0
    total = 0
    for t in range(min_train, n):
        beta = _ridge_fit(X[:t], y[:t])
        if beta is None:
            continue
        pred = _ridge_predict(beta, X[t])
        if (pred > 0) == (y[t] > 0):
            hits += 1
        total += 1
    if total == 0:
        return {"ok": False, "hit_rate": None, "n": n,
                "warnings": ["no walk-forward folds"]}
    return {"ok": True, "hit_rate": round(hits / total, 4), "n": total,
            "warnings": []}


def forecast_sleeve(returns: Sequence[float], *, horizon: int = 20,
                    min_train: int = 40) -> Dict[str, Any]:
    """Forecast the next ``horizon``-day return for one sleeve.

    Uses sklearn's GradientBoostingRegressor when available (trained on the whole
    available history for the *final* prediction — the walk-forward hit-rate is
    the out-of-sample evidence), else ridge. Returns
    ``{"ok", "expected_return", "confidence", "method", "hit_rate"}``.
    """
    rs = [safe_float(r) for r in (returns or [])]
    rs = [r for r in rs if r is not None]
    X = build_features(rs)
    y = _forward_targets(rs, horizon=horizon)
    n = min(len(X), len(y))
    if n < min_train:
        return {"ok": False, "expected_return": None, "confidence": 0.0,
                "method": "insufficient_history", "hit_rate": None}
    wf = walk_forward_hit_rate(rs, horizon=horizon, min_train=min_train)
    hit = wf.get("hit_rate")
    method = "ridge"
    pred = None
    if _has_sklearn():
        try:
            from sklearn.ensemble import GradientBoostingRegressor
            model = GradientBoostingRegressor(n_estimators=80, max_depth=2,
                                              learning_rate=0.05, random_state=0)
            model.fit(X[:n], y[:n])
            pred = float(model.predict([X[-1]])[0])
            method = "gbm"
        except Exception as exc:  # noqa: BLE001
            logger.debug("sklearn GBM failed (%s); ridge fallback", exc)
    if pred is None:
        beta = _ridge_fit(X[:n], y[:n])
        if beta is None:
            return {"ok": False, "expected_return": None, "confidence": 0.0,
                    "method": "fit_failed", "hit_rate": hit}
        pred = _ridge_predict(beta, X[-1])
    # Confidence from out-of-sample skill: 0.5 hit -> floor confidence.
    conf = 0.0
    if hit is not None:
        conf = clamp((hit - 0.5) * 1.6, 0.0, 0.6)
    return {"ok": True, "expected_return": round(clamp(pred, -0.2, 0.2), 6),
            "confidence": round(conf, 4), "method": method, "hit_rate": hit,
            "n": n}


def views_from_history(history: Dict[str, Sequence[float]], *, horizon: int = 20,
                       universe: Optional[Sequence[str]] = None,
                       min_train: int = 40) -> Dict[str, Any]:
    """Produce one ML view per sleeve from a ``{sleeve: returns}`` history.

    Returns ``{"views": [...], "forecasts": {sleeve: {...}}, "warnings": [...]}``.
    A sleeve with low walk-forward skill yields a low-confidence (near-neutral)
    view — exactly the behaviour we want from text/model input.
    """
    universe = list(universe or history.keys())
    views: List[Dict[str, Any]] = []
    forecasts: Dict[str, Any] = {}
    warnings: List[str] = []
    for sleeve in universe:
        series = history.get(sleeve) or []
        f = forecast_sleeve(series, horizon=horizon, min_train=min_train)
        forecasts[sleeve] = f
        if not f.get("ok"):
            continue
        if abs(f["expected_return"]) < 1e-6 or f["confidence"] <= 0:
            continue
        v = make_view([sleeve], view_return=f["expected_return"],
                      confidence=f["confidence"], horizon_days=horizon,
                      source="ml",
                      label=f"{sleeve} ML forecast",
                      rationale=(f"method={f['method']} "
                                 f"hit={f.get('hit_rate')}"))
        if v:
            views.append(v)
    if not views:
        warnings.append("no sleeve passed the ML confidence floor")
    return {"views": views, "forecasts": forecasts, "warnings": warnings}