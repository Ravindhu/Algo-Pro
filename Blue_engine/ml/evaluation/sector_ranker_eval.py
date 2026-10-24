"""
sector_ranker_eval.py - does a cross-sectional ranker BEAT the HM baseline?

WHY THIS EXISTS
---------------
`configs/sector_dataset.json` now holds 15 years of labelled cross-sections
(53,130 rows). That removes the data objection to machine learning here, but it
does not justify the model. The only question worth asking is empirical:

    On out-of-sample data, does a learned ranker beat HM at picking sectors?

This module answers it by running BOTH through the SAME walk-forward splits,
with the same purge and the same costs, and reporting them side by side. If the
ranker does not win, HM stays - that is a valid and useful result, not a
failure of the exercise.

THE BASELINE (HM) IS A REAL COMPETITOR
---------------------------------------
The HM baseline gets one feature (`hm_score`) and no training. It is the thing
the project already ships, so any model must earn its place against it, not
against a coin flip. Comparing a model only to a naive majority class flatters
it for free.

NO LOOK-AHEAD
-------------
`run_walk_forward` owns the split, purge and embargo. This module only supplies
rows, a feature matrix and labels, so it cannot smuggle in a leak that the
evaluator does not also apply to the baseline.

WHAT "BEAT" MEANS
-----------------
Not accuracy. Threshold accuracy on a tercile label is easy to fake by
predicting the majority, and the whole point to maximise is risk-adjusted
return. The comparison therefore reports:

  * out-of-sample Sharpe of the traded strategy (flat-when-not-long),
  * per-fold dispersion (a mean driven by one lucky fold is not skill),
  * deflated Sharpe, because the feature set is one of many we could have tried.

The verdict is deliberately conservative: a model must clear the baseline on
Sharpe AND show across-fold consistency.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = ["FEATURES", "BASELINE_FEATURES", "load_dataset", "evaluate",
           "compare"]

#: Full feature set for the learned ranker.
FEATURES = ["rsi", "ema_rsi", "hm_score", "rising", "below_level",
            "ret_5", "ret_20", "ret_60", "vol_20", "dist_ema20"]

#: The HM baseline sees exactly one column: its own score.
BASELINE_FEATURES = ["hm_score"]


def load_dataset(path: Optional[str] = None) -> Dict[str, Any]:
    """Load the persisted dataset document."""
    import json
    from pathlib import Path

    if path is None:
        from ...sectors.sector_dataset import DEFAULT_DATASET_PATH
        path = str(DEFAULT_DATASET_PATH)
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    doc["_path"] = str(path)
    return doc


def _rows_for_horizon(doc: Dict[str, Any], horizon: int
                      ) -> Tuple[List[Dict[str, Any]], List[List[float]],
                                 List[Optional[int]], List[Optional[float]]]:
    """Shape the document into (rows, X, y, forward_returns).

    Rows are kept ONLY where the label exists: the middle tercile (`None`) is
    excluded from training on purpose - it is neither a buy nor an avoid, and
    including it would teach the model to predict a class we never trade.

    The tail rows (no forward return) are excluded too: they are exactly what a
    live model would predict on, and they must never be fitted.
    """
    rows: List[Dict[str, Any]] = []
    X: List[List[float]] = []
    y: List[Optional[int]] = []
    fwd: List[Optional[float]] = []
    for r in doc.get("rows") or []:
        label = r.get(f"label_{horizon}d")
        if label is None:
            continue
        values = [float(r.get(f) or 0.0) for f in FEATURES]
        rows.append(r)
        X.append(values)
        y.append(int(label))
        fwd.append(r.get(f"fwd_{horizon}d"))
    return rows, X, y, fwd


def _select(X: List[List[float]], names: Sequence[str]) -> List[List[float]]:
    idx = [FEATURES.index(n) for n in names]
    return [[row[i] for i in idx] for row in X]


def _gbm_fit(Xtr, ytr):
    """A small, deliberately shallow tree ensemble.

    Shallow (depth 2, few estimators) because the cross-section is ~14 names:
    a deep model would memorise the sectors themselves rather than learn a
    relationship. This is the same reasoning as the repo's existing trainer.
    """
    from sklearn.ensemble import GradientBoostingClassifier

    if len(set(ytr)) < 2:
        raise ValueError("training fold has a single class")
    return GradientBoostingClassifier(
        n_estimators=60, max_depth=2, learning_rate=0.05,
        subsample=0.8, random_state=42).fit(Xtr, ytr)


def _gbm_predict(model, Xte):
    # Probability of the positive (top-tercile) class. The trainer guarantees
    # both classes are present in every training fold, so class order is
    # stable; index 1 is the label-1 probability.
    return [float(p[1]) for p in model.predict_proba(Xte)]


def evaluate(doc: Dict[str, Any], horizon: int = 5, *,
             features: Optional[Sequence[str]] = None,
             baseline: bool = False,
             n_splits: int = 5, min_train: int = 2000,
             cost_bps: float = 10.0,
             threshold: float = 0.5) -> Dict[str, Any]:
    """Walk-forward one configuration. Returns a report dict, never raises."""
    from .walk_forward import run_walk_forward

    rows, X, y, fwd = _rows_for_horizon(doc, horizon)
    if len(rows) < min_train + 5:
        return {"ok": False, "reason": f"only {len(rows)} labelled rows",
                "horizon": horizon}

    names = list(features or FEATURES)
    Xsel = _select(X, names)

    if baseline:
        # HM as a rule, not a model: long when the sector is in the bottom
        # `threshold` of the HM score. No fitting, so no fold can overfit.
        def fit_fn(Xtr, ytr):
            return "hm"

        def predict_fn(model, Xte):
            # hm_score is (100 - RSI); a HIGH score means a LOWER RSI, i.e.
            # more discounted. Rank within the test block, as a live run does.
            vals = [row[0] for row in Xte]
            if not vals:
                return []
            order = sorted(range(len(vals)), key=lambda i: -vals[i])
            cut = max(1, int(len(vals) * threshold))
            keep = set(order[:cut])
            return [1.0 if i in keep else 0.0 for i in range(len(vals))]
    else:
        fit_fn, predict_fn = _gbm_fit, _gbm_predict

    report = run_walk_forward(
        rows, Xsel, y, fit_fn=fit_fn, predict_fn=predict_fn,
        forward_returns=fwd, horizon=horizon, n_splits=n_splits,
        min_train=min_train, prob_threshold=threshold,
        cost_bps_per_turnover=cost_bps, n_trials=1)

    summary = report.summary or {}

    def stat(key: str) -> Optional[float]:
        # fold_summary returns {mean,std,min,max} per metric, not a scalar.
        entry = summary.get(key)
        if isinstance(entry, dict):
            return entry.get("mean")
        return entry if isinstance(entry, (int, float)) else None
    sharpe_entry = summary.get("sharpe")
    if isinstance(sharpe_entry, dict):
        sharpe = sharpe_entry.get("mean")
        sharpe_std = sharpe_entry.get("std")
        sharpe_min = sharpe_entry.get("min")
    else:
        sharpe = sharpe_entry if isinstance(sharpe_entry, (int, float)) else None
        sharpe_std = sharpe_min = None
    return {
        "ok": True,
        "horizon": horizon,
        "features": names,
        "n_rows": len(rows),
        "n_folds": report.n_folds,
        "n_errors": report.n_errors,
        "summary_keys": sorted(summary),
        "sharpe_mean": sharpe,
        "sharpe_std": sharpe_std,
        "sharpe_min": sharpe_min,
        "skill_mean": stat("skill"),
        "verdict": report.verdict,
        "deflation": report.deflation,
        "cost_check": report.cost_check,
        "folds": report.folds,
    }


def compare(doc: Optional[Dict[str, Any]] = None, horizon: int = 5,
            **kwargs: Any) -> Dict[str, Any]:
    """Run baseline and ranker on identical splits and pick a winner."""
    doc = doc or load_dataset()
    base = evaluate(doc, horizon, features=BASELINE_FEATURES, baseline=True,
                    **kwargs)
    model = evaluate(doc, horizon, features=FEATURES, baseline=False, **kwargs)

    verdict = _decide(base, model)
    return {"baseline": base, "ranker": model, "decision": verdict,
            "horizon": horizon}


def _decide(base: Dict[str, Any], model: Dict[str, Any]) -> Dict[str, Any]:
    """A conservative comparison. Default is: keep HM."""
    b = base.get("sharpe_mean")
    m = model.get("sharpe_mean")
    if b is None or m is None:
        return {"winner": "undecided",
                "reason": "one side produced no Sharpe; cannot compare",
                "keep": "HM (the shipped baseline)"}
    if model.get("sharpe_std") is not None and b is not None:
        # Require the model to clear the baseline by more than its own fold
        # dispersion, otherwise a single lucky fold can win the comparison.
        need = b + abs(model.get("sharpe_std") or 0.0)
    else:
        need = b
    if m > need:
        return {"winner": "ranker",
                "baseline_sharpe": round(b, 4), "ranker_sharpe": round(m, 4),
                "required": round(need, 4),
                "reason": "ranker beat HM by more than its own fold dispersion",
                "keep": "ranker (still requires paper trading first)"}
    return {"winner": "hm",
            "baseline_sharpe": round(b, 4), "ranker_sharpe": round(m, 4),
            "required": round(need, 4),
            "reason": ("ranker did not clear HM plus its own dispersion; "
                       "HM is simpler and already shipped"),
            "keep": "HM"}
