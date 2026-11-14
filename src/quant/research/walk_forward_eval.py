"""Fitted, scored walk-forward evaluation. Wraps the engine's runner.

Flow stage: Walk-forward + purged CV, Trial logger + DSR/PBO (research validation lane)
Status: PHASE 5 IMPLEMENTED (wrap; research only, no trade path)
Existing code to reuse or wrap (do not copy):
    src/quant/blue_engine/ml/evaluation/walk_forward.py  (run_walk_forward,
        FoldResult, WalkForwardReport, strategy_returns_from_probs)

WHY THIS IS A SEPARATE MODULE FROM ``research/walk_forward.py``
--------------------------------------------------------------
``research/walk_forward.py`` is the SPLITTER: it yields index arrays and nothing
else (pure geometry, ported with a parity test). The engine module here is the
RUNNER: it fits a model per fold, scores it, deflates the Sharpe for the number
of trials, and applies a cost check. Two different jobs; merging them would make
the splitter depend on a model library, which is exactly the coupling the engine
author avoided on purpose.

WHAT THIS WRAPPER ADDS
----------------------
1. A **verdict vocabulary** the pipeline can branch on, instead of a free-text
   string: ``passed`` / ``failed`` / ``inconclusive`` / ``unavailable``.
2. A **cost-aware significance gate** (`is_significant`) that requires the
   deflated Sharpe to survive multiple-testing, not the raw Sharpe. A strategy
   that "wins" only because we tried 200 variants is refused.
3. An **honest unavailable state** when the engine or a dependency is missing.
   Research never silently reports an empty pass.

WHAT IT DOES NOT DO
-------------------
- No model import. ``fit_fn`` / ``predict_fn`` are callables you supply, exactly
  as the engine expects, so ridge, GBM or a torch net all plug in the same way.
- No writes. Persisting a result is `research/trial_log.py`'s job, and only after
  `require_passed` says the evidence is real.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = [
    "WalkForwardEvalUnavailable",
    "PASSED",
    "FAILED",
    "INCONCLUSIVE",
    "UNAVAILABLE",
    "evaluate",
    "is_significant",
    "require_passed",
]

PASSED = "passed"
FAILED = "failed"
INCONCLUSIVE = "inconclusive"
UNAVAILABLE = "unavailable"

#: A deflated Sharpe must beat this to count as evidence. 0.0 means "the observed
#: Sharpe is better than the best you would expect from random trials".
MIN_DEFLATED_SHARPE = 0.0


class WalkForwardEvalUnavailable(RuntimeError):
    """The engine's walk-forward runner cannot be loaded."""


def _module():
    try:
        return load("ml.evaluation.walk_forward")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise WalkForwardEvalUnavailable(f"engine walk-forward unavailable: {exc}") from exc


def evaluate(
    rows: Sequence[Dict[str, Any]],
    X: Sequence[Sequence[float]],
    y: Sequence[Optional[int]],
    *,
    fit_fn: Callable[..., Any],
    predict_fn: Callable[..., Any],
    forward_returns: Sequence[Optional[float]],
    horizon: int = 5,
    n_splits: int = 5,
    embargo: Optional[int] = None,
    min_train: int = 250,
    mode: str = "expanding",
    prob_threshold: float = 0.5,
    n_trials: int = 1,
    cost_bps_per_turnover: float = 10.0,
    turnover_per_period: float = 1.0,
    ts_col: str = "as_of_ts",
) -> Dict[str, Any]:
    """Run the engine's walk-forward and return its report with a verdict.

    ``horizon`` is also the purge length. Passing a horizon that does not match
    the label silently re-introduces leakage, so the caller must take it from the
    label config, never hand-type it.

    ``n_trials`` must be the TRUE number of variants tried. Deflation is a no-op
    at 1; under-reporting it is how a curve-fit strategy gets promoted.

    Returns the engine report plus::

        {"verdict": passed|failed|inconclusive|unavailable,
         "detail": str, "engine": <report dict> or None}
    """
    try:
        mod = _module()
    except WalkForwardEvalUnavailable as exc:
        return {"verdict": UNAVAILABLE, "detail": str(exc), "engine": None}

    report = mod.run_walk_forward(
        rows, X, y,
        fit_fn=fit_fn,
        predict_fn=predict_fn,
        forward_returns=forward_returns,
        horizon=horizon,
        n_splits=n_splits,
        embargo=embargo,
        min_train=min_train,
        mode=mode,
        prob_threshold=prob_threshold,
        n_trials=n_trials,
        cost_bps_per_turnover=cost_bps_per_turnover,
        turnover_per_period=turnover_per_period,
        ts_col=ts_col,
    )
    engine = report.to_dict() if hasattr(report, "to_dict") else dict(report)
    verdict, detail = _classify(engine)
    return {"verdict": verdict, "detail": detail, "engine": engine}


def _classify(report: Dict[str, Any]) -> "tuple[str, str]":
    """Map an engine report onto one verdict. Never guesses a pass."""
    n_folds = int(report.get("n_folds") or 0)
    n_errors = int(report.get("n_errors") or 0)

    if n_folds == 0:
        return INCONCLUSIVE, "no folds produced (dataset too short for the requested splits)"

    verdict_text = str(report.get("verdict") or "").strip()
    deflation = report.get("deflation") or {}
    deflated = deflation.get("deflated_sharpe") if isinstance(deflation, dict) else None

    # The engine already writes a one-line verdict; trust a FAIL before a pass.
    if verdict_text.upper().startswith("FAIL"):
        return FAILED, verdict_text

    if deflated is None:
        return INCONCLUSIVE, "deflated Sharpe not computed; cannot certify significance"

    if float(deflated) <= MIN_DEFLATED_SHARPE:
        return FAILED, f"deflated Sharpe {float(deflated):.3f} does not beat random trials"

    if n_errors:
        return INCONCLUSIVE, f"{n_errors} fold(s) errored; result is partial"

    return PASSED, verdict_text or f"deflated Sharpe {float(deflated):.3f} survives {n_folds} folds"


def is_significant(report: Dict[str, Any]) -> bool:
    """True only when the verdict is PASSED. Anything else is not evidence."""
    return str(report.get("verdict") or "") == PASSED


def require_passed(report: Dict[str, Any]) -> Dict[str, Any]:
    """Return the report when significant; raise ValueError otherwise.

    Call before writing a trial-log row. An inconclusive run recorded as evidence
    is how a system ends up trading a strategy that was never proven.
    """
    if is_significant(report):
        return report
    raise ValueError(
        f"walk-forward refused (verdict={report.get('verdict') or 'unknown'}): "
        f"{report.get('detail') or 'no detail'}")
