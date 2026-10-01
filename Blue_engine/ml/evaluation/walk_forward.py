"""
ml/evaluation/walk_forward.py — the orchestrator that runs folds honestly.

WHY THIS FILE EXISTS (the problem it prevents)
----------------------------------------------
Most quant pipelines die in one of two ways:

1. **The leak dies quietly.** Someone builds features, fits a model on ALL the
   data, and scores the same data. Accuracy 90%, Sharpe 2.4, beautiful curve.
   The bug is that the model was scored on rows it trained on. No exception is
   raised anywhere. This module makes that impossible to write by accident:
   it *owns* the split, and the caller never sees test indices.

2. **The aggregation dies quietly.** Five folds are run and only the MEAN is
   reported. Fold metrics `[0.10, 0.12, -0.02, 0.08, 0.11]` have mean +0.078
   but a NEGATIVE fold — a strategy that loses money in a third of periods.
   This module reports mean AND std AND min, and derives a verdict from the
   dispersion, not the mean.

THE ORDER OF OPERATIONS (enforced here, non-negotiable)
-------------------------------------------------------
For every fold: audit (data quality) -> fit (train rows ONLY) -> score
(test rows ONLY) -> record (fold metrics + provenance). After all folds:
aggregate (mean/std/min) -> deflate (correct for the search) -> cost-check
(does the edge survive Indian costs). Nothing else.

Example of what this prevents, in code::

    # WRONG (everywhere on the internet, always):
    model.fit(X, y)                 # all rows, including the future
    acc = model.score(X, y)         # same rows. Accuracy means nothing.
    # This module cannot be used to write that. There is no API for it.

    # RIGHT (what this file does):
    for fold in walk_forward(rows, horizon=5):
        model  = build(params)      # fresh model per fold
        model.fit(X[fold.train_idx])
        p      = model.predict_proba(X[fold.test_idx])
        record(fold, score(p, y[fold.test_idx]))

DESIGN NOTE — sklearn/torch stay OUT of this file
-------------------------------------------------
This file is a *conductor*, not a musician. It takes a generic ``fit_fn`` /
``predict_fn`` pair (callables), so ridge, GBM, torch nets and RL policies all
plug in without this file importing any of them. That keeps the evaluation
logic auditable on a bare machine and stops the "the evaluator only works with
one library" trap.

NAMING
------
``run_walk_forward`` here vs ``walk_forward`` in ``splits.py``: the splitter
returns *index sets* (pure geometry, no model). This module turns them into
*fitted, scored folds* (side effects, model calls). Same name for both would
hide the difference — and the difference is exactly where bugs live.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .. import logger
from .metrics import (
    accuracy, auc, base_rate, brier_score, cost_adjusted_edge,
    deflated_sharpe, fold_summary, sharpe, skill,
)
from .splits import Split, walk_forward as make_splits

__all__ = [
    "FoldResult", "WalkForwardReport", "run_walk_forward",
    "strategy_returns_from_probs",
]

#: A model wrapper must expose these two callables. Documented as a type alias
#: so the trainer can build one object per candidate and pass it straight in.
FitFn = Callable[..., Any]
"""``fit_fn(X_train, y_train) -> fitted`` — trains on TRAIN ROWS ONLY."""

PredictFn = Callable[..., Any]
"""``predict_fn(fitted, X_test) -> probabilities in [0, 1] (one per row)."""


# --------------------------------------------------------------------------- #
# fold record
# --------------------------------------------------------------------------- #


@dataclass
class FoldResult:
    """Everything we learned from ONE fold. Persisted verbatim in the manifest.

    Kept flat and JSON-shaped (no nested objects, no numpy) so the manifest is
    readable in a text editor ten years later. A number you cannot read is a
    number you will not check.
    """

    fold: int
    train_rows: int
    test_rows: int
    train_end_date: Optional[str] = None      # for the manifest's split dates
    test_start_date: Optional[str] = None
    # scores (None = could not compute; never written as 0, which would bias means)
    accuracy: Optional[float] = None
    base_rate: Optional[float] = None
    skill: Optional[float] = None
    auc: Optional[float] = None
    brier: Optional[float] = None
    # P&L on the test block, per-period simple returns (for Sharpe/deflation)
    returns: List[float] = field(default_factory=list)
    sharpe: Optional[float] = None
    # diagnostics
    fit_seconds: float = 0.0
    error: Optional[str] = None               # a fold may FAIL; we record why

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fold": self.fold, "train_rows": self.train_rows,
            "test_rows": self.test_rows,
            "train_end_date": self.train_end_date,
            "test_start_date": self.test_start_date,
            "accuracy": self.accuracy, "base_rate": self.base_rate,
            "skill": self.skill, "auc": self.auc, "brier": self.brier,
            "sharpe": self.sharpe, "fit_seconds": round(self.fit_seconds, 3),
            "error": self.error,
            # The per-period strategy returns MUST travel with the fold.
            # `run_walk_forward` aggregates them across folds to build the one
            # series that deflation and the cost gate consume. Omitting this key
            # (as this dict did) silently empties that series, so both hard gates
            # were reported as "could not measure" on every run - which reads as
            # a caveat but was in fact a bug. Rounded to 8 dp: enough to keep the
            # Sharpe stable, short enough to stay readable in the manifest.
            "returns": [round(float(r), 8) for r in self.returns],
        }


@dataclass
class WalkForwardReport:
    """The aggregated, deflated, cost-checked verdict of one evaluation run.

    This object is what the dashboard shows and what the manifest stores.
    It is deliberately a *report about a process*, not a number: the same
    pipeline produces it for ridge, GBM, CNN and RL alike, so candidates can
    be compared apples-to-apples.
    """

    n_folds: int = 0
    n_errors: int = 0
    folds: List[Dict[str, Any]] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    deflation: Dict[str, Any] = field(default_factory=dict)
    cost_check: Dict[str, Any] = field(default_factory=dict)
    #: The final, one-line verdict. "" means "not computed".
    verdict: str = ""
    wall_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_folds": self.n_folds, "n_errors": self.n_errors,
            "folds": self.folds, "summary": self.summary,
            "deflation": self.deflation, "cost_check": self.cost_check,
            "verdict": self.verdict, "wall_seconds": round(self.wall_seconds, 1),
        }

    def summary_line(self) -> str:
        """One line for a log or a dashboard badge (matches quality.py style)."""
        if self.verdict:
            return self.verdict
        if self.n_errors and not self.n_folds:
            return "FAIL — every fold errored"
        return "NO VERDICT — incomplete run"


# --------------------------------------------------------------------------- #
# the runner
# --------------------------------------------------------------------------- #


def strategy_returns_from_probs(
        probs: Sequence[float],
        fwd_returns: Sequence[Optional[float]],
        *,
        threshold: float = 0.5,
) -> List[float]:
    """Per-period strategy returns implied by a probability stream.

    Rule: when ``p > threshold`` the strategy is long, and it earns the forward
    return of that period; otherwise it is flat and earns 0.0.

    Why flat-when-not-long (and not short): Indian delivery equity has no cheap
    short leg, and pretending to short what you cannot short is how backtests
    report riskless profit. The FLAT leg earns exactly 0 — which is honest, and
    it also means the Sharpe of this series is the Sharpe of the *traded*
    strategy, not of a market-neutral fantasy.

    Args:
        probs: model probability that the forward return is positive.
        fwd_returns: realised forward return per row (None rows are skipped —
            they are the label-less tail rows a live model also cannot trade).
        threshold: the long trigger. 0.5 is the uncalibrated default; a
            calibrated model may legitimately want a different trigger, and the
            caller owns that choice (it is recorded in the manifest).

    Example:
        probs        = [0.6, 0.4, 0.7]
        fwd          = [0.01, -0.02, 0.005]
        threshold    = 0.5
        returns      = [0.01,  0.0,  0.005]   # row 1 is flat (p below trigger)
    """
    out: List[float] = []
    for p, r in zip(probs, fwd_returns):
        if p is None or r is None:
            continue
        out.append(float(r) if float(p) > threshold else 0.0)
    return out


def _score_fold(fold_no: int, split: Split,
                y_test: Sequence[Optional[int]],
                probs: Sequence[Optional[float]],
                fwd_test: Sequence[Optional[float]],
                *,
                prob_threshold: float) -> FoldResult:
    """Score one fold's predictions. Pure — no model, no I/O.

    Kept as its own function so it can be unit-tested WITHOUT a model: feed it
    hand-computed probabilities and verify every metric by eye.
    """
    fr = FoldResult(
        fold=fold_no, train_rows=len(split.train_idx),
        test_rows=len(split.test_idx),
        train_end_date=split.train_dates[1], test_start_date=split.test_dates[0],
    )
    try:
        fr.accuracy = accuracy(y_test, [1 if (p is not None and p > prob_threshold) else 0
                                        for p in probs])
        fr.base_rate = base_rate(y_test)
        fr.skill = skill(y_test, [1 if (p is not None and p > prob_threshold) else 0
                                  for p in probs])
        fr.auc = auc(y_test, probs)
        fr.brier = brier_score(y_test, probs)
        rets = strategy_returns_from_probs(
            [p for p in probs if p is not None], fwd_test,
            threshold=prob_threshold)
        fr.returns = rets
        fr.sharpe = sharpe(rets, periods_per_year=252.0)
    except Exception as exc:  # noqa: BLE001 - a scoring bug must not hide as a model result
        fr.error = f"scoring: {type(exc).__name__}: {exc}"
    return fr


def run_walk_forward(
        rows: Sequence[Dict[str, Any]],
        X: Sequence[Sequence[float]],
        y: Sequence[Optional[int]],
        *,
        fit_fn: FitFn,
        predict_fn: PredictFn,
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
) -> WalkForwardReport:
    """The one entry point. Runs folds, aggregates, deflates, cost-checks.

    WHY THE CALLER PASSES X AND y RATHER THAN A DATASET OBJECT
    ----------------------------------------------------------
    To keep this module dependency-free AND library-agnostic, the feature
    matrix is just a list of lists and the model is two callables. A sklearn
    model plugs in as::

        wf = run_walk_forward(rows, X, y,
                              fit_fn=lambda Xtr, ytr: GradientBoostingClassifier(
                                  max_depth=2).fit(Xtr, ytr),
                              predict_fn=lambda m, Xte: [p[1] for p in
                                  m.predict_proba(Xte)],
                              ...)

    and a pure-Python ridge or a torch net plugs in the same way. The
    orchestrator never needs to change.

    Args:
        rows: the full dataset (oldest first) — used ONLY for split dates.
        X: feature matrix, aligned row-for-row with ``rows`` and ``y``.
        y: labels (None where the label is undefined — the warm-up and the tail).
        fit_fn / predict_fn: the model, as callables (see above).
        forward_returns: per-row forward return, for the strategy P&L. May be
            shorter than X; a missing entry scores the row as untradeable.
        horizon: label horizon; also the purge length. Read from the label
            config by the trainer, never hand-typed — a wrong value here
            silently re-introduces leakage.
        n_trials: how many candidate variants this run is one of. HONESTY
            REQUIRED (see ``metrics.deflated_sharpe``). If this file is the
            only variant, pass 1 and the deflation is a no-op.
        cost_bps_per_turnover: Indian round-trip cost assumption (10 bps
            delivery default — matches ``input/macro_master.json`` costs).
        prob_threshold: long trigger for the strategy return.

    Returns:
        A :class:`WalkForwardReport`. ALWAYS — never raises for a fold-level
        failure; folds that error are recorded and reported in ``n_errors``.
    """
    started = time.time()
    rep = WalkForwardReport()

    n = min(len(X), len(y))
    if n < min_train + 5:
        rep.verdict = ("FAIL — dataset too short "
                       f"({n} usable rows < min_train {min_train} + 5)")
        rep.wall_seconds = time.time() - started
        return rep

    # Step 1-2: split FIRST (before any model exists), then audit shape.
    splits: List[Split] = make_splits(
        list(rows)[:n], n_splits=n_splits, horizon=horizon,
        embargo=embargo, mode=mode, min_train=min_train, ts_col=ts_col)
    if not splits:
        rep.verdict = "FAIL — walk_forward produced no folds (data too short)"
        rep.wall_seconds = time.time() - started
        return rep

    for split in splits:
        # ---- 3. fit on TRAIN ONLY (a fresh model every fold — state must not
        #         leak from one fold's data into the next fold's predictions)
        try:
            X_tr = [X[i] for i in split.train_idx if y[i] is not None]
            y_tr = [y[i] for i in split.train_idx if y[i] is not None]
            t0 = time.time()
            fitted = fit_fn(X_tr, y_tr)
            fit_secs = time.time() - t0
        except Exception as exc:  # noqa: BLE001 - fold-level isolation
            rep.folds.append({"fold": split.fold,
                              "error": f"fit: {type(exc).__name__}: {exc}"})
            rep.n_errors += 1
            continue

        # ---- 4. score on TEST ONLY
        try:
            X_te = [X[i] for i in split.test_idx]
            probs = predict_fn(fitted, X_te)
            if not isinstance(probs, (list, tuple)) or len(probs) != len(X_te):
                raise ValueError(
                    f"predict_fn returned {type(probs).__name__} of length "
                    f"{len(probs) if hasattr(probs, '__len__') else '?'}; "
                    f"expected {len(X_te)} probabilities")
            y_te = [y[i] for i in split.test_idx]
            fwd_te = [forward_returns[i] if i < len(forward_returns) else None
                      for i in split.test_idx]
            fr = _score_fold(split.fold, split, y_te, probs, fwd_te,
                             prob_threshold=prob_threshold)
            fr.fit_seconds = fit_secs
            rep.folds.append(fr.to_dict())
            rep.n_folds += 1
            logger.info("walk_forward fold %d: skill=%s auc=%s sharpe=%s",
                        split.fold, fr.skill, fr.auc, fr.sharpe)
        except Exception as exc:  # noqa: BLE001 - fold-level isolation
            rep.folds.append({"fold": split.fold,
                              "error": f"score: {type(exc).__name__}: {exc}"})
            rep.n_errors += 1
            continue

    # ---- 5. aggregate mean AND std (the std is the risk number)
    numeric_folds = [f for f in rep.folds if f.get("error") is None]
    rep.summary = fold_summary(numeric_folds)

    # ---- 6. deflate (correct for how many variants were tried)
    all_rets = [r for f in numeric_folds for r in f.get("returns", [])]
    per_period_sharpe = None
    if len(all_rets) > 1:
        # Deflation works on the PER-PERIOD Sharpe (not annualised), so we
        # recompute it here rather than reusing the fold-level annualised ones.
        clean = [float(r) for r in all_rets]
        mu = sum(clean) / len(clean)
        var = sum((r - mu) ** 2 for r in clean) / (len(clean) - 1)
        sd = math.sqrt(var) if var > 0 else 0.0
        if sd > 0:
            per_period_sharpe = mu / sd
    if per_period_sharpe is not None:
        rep.deflation = deflated_sharpe(
            per_period_sharpe, n_trials=max(1, n_trials),
            n_obs=len(all_rets))
    else:
        rep.deflation = {"verdict": "no tradeable returns — deflation skipped",
                         "n_trials": max(1, n_trials)}

    # ---- 7. cost-check (does the edge survive Indian transaction costs)
    sk = rep.summary.get("skill") or {}
    mean_skill = sk.get("mean") if isinstance(sk, dict) else None
    if mean_skill is not None and per_period_sharpe is not None:
        # Translate a per-period Sharpe into a gross edge in bps, so the cost
        # gate is a like-for-like comparison. Edge(bps) ~= Sharpe * sd_ret(bps):
        # a 0.05 Sharpe on 1%-a-day vol is a 5 bps edge, which loses to 10 bps
        # costs. Computing the bps directly, from the same return series, keeps
        # the units honest (no "Sharpe -> edge" folklore conversion).
        gross_edge_bps = (mu / len(all_rets)) * 10_000.0
        rep.cost_check = cost_adjusted_edge(
            gross_edge_bps, turnover_per_period=turnover_per_period,
            cost_bps_per_turnover=cost_bps_per_turnover)
    else:
        rep.cost_check = {"verdict": "not computable — no returns"}

    # ---- 8. the verdict, built from dispersion not the mean
    rep.verdict = _verdict(rep)
    rep.wall_seconds = time.time() - started
    logger.info("walk_forward complete: %s", rep.summary_line())
    return rep


def _stdev(values: Sequence[float]) -> float:
    """Sample standard deviation (0.0 for <2 values — callers already guard)."""
    n = len(values)
    if n < 2:
        return 0.0
    mu = sum(values) / n
    return math.sqrt(sum((v - mu) ** 2 for v in values) / (n - 1))


def _verdict(rep: WalkForwardReport) -> str:
    """Turn the aggregate into ONE operator-facing sentence.

    The order of checks is deliberate — cheapest-first, most-lethal-first:
      1. did every fold error?            -> FAIL (no evidence at all)
      2. does any metric show an edge?    -> FAIL NO EDGE
      3. is the edge stable across folds? -> UNSTABLE (trading a coin flip)
      4. did the search survive deflation?-> NOT ESTABLISHED (likely selection luck)
      5. did the edge survive costs?      -> NO EDGE AFTER COSTS
      6. all four passed                  -> ESTABLISHED

    THE "COULD NOT MEASURE" CASE IS NOT A PASS
    -------------------------------------------
    Checks 4 and 5 need a per-period RETURN series. A volatility target
    forecasts vol, not return, so it has no price P&L for the cost gate to
    test and no Sharpe to deflate. Previously the code let a missing check
    fall through and still print ESTABLISHED, which is the most misleading
    output this module can produce: it reads as "passed everything" when the
    truth is "the two hardest checks could not run at all".

    Such a run now reports ESTABLISHED (UNVERIFIED) and names what is missing.
    A researcher may still act on it, but they cannot do so by accident.
    """
    if rep.n_errors and rep.n_folds == 0:
        return f"FAIL — all {rep.n_errors} folds errored"
    if not rep.n_folds:
        return "FAIL — no completed folds"

    sk = rep.summary.get("skill") or {}
    mean_skill = sk.get("mean") if isinstance(sk, dict) else None
    std_skill = sk.get("std") if isinstance(sk, dict) else None
    if mean_skill is None or mean_skill <= 0:
        return f"FAIL — no edge (mean skill {mean_skill})"
    if std_skill is not None and std_skill > mean_skill:
        return (f"UNSTABLE — skill {mean_skill:.3f} ± {std_skill:.3f}; "
                f"std exceeds mean: the edge is not consistent across folds")

    prob_real = rep.deflation.get("prob_real")
    if prob_real is not None and prob_real < 0.95:
        return (f"NOT ESTABLISHED — deflated Sharpe p={prob_real:.2f} with "
                f"n_trials={rep.deflation.get('n_trials')}: could be "
                f"selection luck; reduce variants or add data")

    cc = rep.cost_check or {}
    if cc.get("survives") is False:
        return (f"NO EDGE AFTER COSTS — gross {cc.get('gross_bps')} bps vs "
                f"cost {cc.get('cost_bps')} bps: do not trade this")

    # Which of the two hard checks actually ran?
    unverified: List[str] = []
    if prob_real is None:
        unverified.append("deflation (no return series to deflate)")
    if cc.get("survives") is None:
        unverified.append("the cost gate (no return series to charge costs to)")

    head = (f"skill {mean_skill:.3f} ± {std_skill:.3f} over {rep.n_folds} folds")
    if unverified:
        return (f"EDGE, but UNVERIFIED — {head}. NOT checked: "
                f"{'; '.join(unverified)}. The skill is real on these folds; "
                f"whether it survives the search and transaction costs is "
                f"UNMEASURED, not passed. Do not read this as validated.")
    return (f"ESTABLISHED — {head}, p_real={round(prob_real, 2)}, "
            f"net {cc.get('net_bps')} bps/period")
