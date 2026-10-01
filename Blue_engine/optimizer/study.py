"""
optimizer/study.py - the trial record, parameter importance, and the verdict.

WHAT A STUDY IS (and why it is not just a list of trials)
---------------------------------------------------------
A study is the *complete, auditable record* of one search: what was searched,
under which objective version, what each trial scored, and what the honest
verdict is. The auditability is the point. A "best params" number without the
trial list is an answer you cannot interrogate, and an answer you cannot
interrogate is the one that eventually gets traded by accident.

TWO VERDICTS, ALWAYS PAIRED
---------------------------
Every study produces:
    best trial       = argmax over trials (the raw answer)
    deflated Sharpe  = the same number corrected for the search (the honest one)

If the deflated probability is below the bar, the recommendation is shown with
the verdict the operator must not be allowed to miss: NOT ESTABLISHED. This
mirrors the dashboard's existing colour rule in ``pages/ml_models.py``:
grey/amber is a first-class state, not a rendering failure.

PARAMETER IMPORTANCE (how "driving the ship" is measured)
---------------------------------------------------------
Optuna's fANOVA needs its own library and is heavy for a few hundred trials.
The fallback here is a variance decomposition that is honest about being a
proxy:

For each parameter, bin the trials along that parameter, compute the mean score
per bin, and measure how much of the total variance of ``score`` is explained
by the between-bin differences (a correlation-ratio / eta-squared, exactly the
``R`` of a one-way ANOVA on binned data).

    importance(p) = Var_binned_mean(p) / Var(score)

* 0.0 - the score does not move with this parameter (it is not driving anything)
* 1.0 - the score is a deterministic function of this parameter alone
* NOT causality. A parameter correlated with the true driver will inherit
  importance. That is the same caveat Optuna's fANOVA has, not less.
* Needs enough trials. Under ``config.MIN_TRIALS_FOR_IMPORTANCE`` (50) the
  ranking is marked "under-budget" rather than silently shown.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from . import config, logger

__all__ = ["TrialRecord", "Study", "StudyVerdict"]


@dataclass
class TrialRecord:
    """One evaluated parameter set, in the study's journal."""

    number: int
    params: Dict[str, Any]
    value: Optional[float] = None          # the objective's score (may be None)
    ok: bool = False
    reason: Optional[str] = None
    mean_sharpe: Optional[float] = None
    std_sharpe: Optional[float] = None
    min_sharpe: Optional[float] = None
    n_folds: int = 0
    #: Total per-fold return observations behind the score. Used as ``n_obs``
    #: for the deflated-Sharpe correction - the formula needs the real sample
    #: size, not a multiple of the fold count (audit finding C4).
    n_obs: int = 0
    seconds: float = 0.0
    cached: bool = False
    #: AUDIT FINDING F2 (2026-10-01). The cost-adjusted-edge gate computed by
    #: ``Objective.evaluate`` (gross bps vs cost bps, with the Indian-market
    #: 10 bps assumption as the default). Populated by the engine from
    #: ``ObjectiveResult.cost_gate`` and round-tripped through the JSONL
    #: journal. ``report._best_cost_gate`` reads it here, not from
    #: ``study.meta`` (which the engine does not write), so a future migration
    #: to a different storage layout must keep this field in step.
    cost_gate: Optional[Dict[str, Any]] = None
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "number": self.number, "params": dict(self.params),
            "value": self.value, "ok": self.ok, "reason": self.reason,
            "mean_sharpe": self.mean_sharpe, "std_sharpe": self.std_sharpe,
            "min_sharpe": self.min_sharpe, "n_folds": self.n_folds,
            "n_obs": self.n_obs,
            "cost_gate": (dict(self.cost_gate) if self.cost_gate else None),
            "seconds": round(self.seconds, 4), "cached": self.cached,
            "ts": self.ts,
        }


class Study:
    """Ordered trial journal for one optimisation run."""

    def __init__(self, *, name: str, space_name: str, strategy: str,
                 objective_version: str, seed: int = 0,
                 meta: Optional[Dict[str, Any]] = None) -> None:
        self.name = str(name)
        self.space_name = space_name
        self.strategy = strategy
        self.objective_version = objective_version
        self.seed = int(seed)
        self.meta: Dict[str, Any] = dict(meta or {})
        self.trials: List[TrialRecord] = []
        self.started = time.time()
        self.wall_seconds: float = 0.0

    # -- recording --------------------------------------------------------- #

    def add(self, rec: TrialRecord) -> TrialRecord:
        self.trials.append(rec)
        return rec

    @property
    def n_ok(self) -> int:
        return sum(1 for t in self.trials if t.ok)

    @property
    def n_failed(self) -> int:
        return sum(1 for t in self.trials if not t.ok)

    def best(self) -> Optional[TrialRecord]:
        """The trial with the highest score. Ties -> the EARLIER one.

        Earlier-on-tie is deliberate: a later trial that ties an earlier one
        adds no information, and preferring the first keeps the recommendation
        stable when two runs are compared.
        """
        best: Optional[TrialRecord] = None
        for t in self.trials:
            if not t.ok or t.value is None:
                continue
            if best is None or (t.value > best.value):
                best = t
        return best

    def top(self, n: int = 10) -> List[TrialRecord]:
        ok = [t for t in self.trials if t.ok and t.value is not None]
        return sorted(ok, key=lambda t: t.value, reverse=True)[:max(0, n)]

    # -- importance -------------------------------------------------------- #

    def param_importance(self, *, n_bins: int = 6) -> Dict[str, Any]:
        """Variance-based importance per parameter.

        Returns ``{"ok", "importance": {name: score}, "ranking": [names],
        "under_budget": bool, "n_trials_used": int, "method": str}``.

        Method: one-way ANOVA correlation ratio (eta-squared) on binned trials.
        For each parameter, bin the trials along it, compute the mean score per
        bin, and measure the share of the total score variance that is *between*
        bins rather than within them. 0.0 = the score does not move with the
        parameter; 1.0 = the score is a deterministic function of it.

        Failed trials are EXCLUDED: a rejected trial carries no score, and
        pretending it scored 0 would put a spurious cliff into the bin means
        exactly where the space's constraint is - inventing importance for the
        constraint rather than for the market.
        """
        usable = [t for t in self.trials if t.ok and t.value is not None]
        under_budget = len(usable) < config.MIN_TRIALS_FOR_IMPORTANCE
        if len(usable) < 8:
            return {"ok": False, "importance": {}, "ranking": [],
                    "under_budget": True, "n_trials_used": len(usable),
                    "method": "variance_ratio",
                    "reason": f"only {len(usable)} scoreable trials; need >= 8"}

        names = sorted({k for t in usable for k in t.params})
        scores = [float(t.value) for t in usable]
        total_mean = sum(scores) / len(scores)
        total_var = sum((s - total_mean) ** 2 for s in scores) / len(scores)
        imp: Dict[str, float] = {}
        for name in names:
            xs = [(t.params.get(name), float(t.value)) for t in usable
                  if t.params.get(name) is not None]
            if not xs:
                imp[name] = 0.0
                continue
            # Group the trials along this parameter. Numeric -> quantile bins
            # (equal counts per bin, so a dense region does not dominate);
            # categorical -> one group per category value.
            is_numeric = all(isinstance(x, (int, float)) for x, _ in xs)
            if is_numeric and len({x for x, _ in xs}) > n_bins:
                bins: Dict[int, List[float]] = {}
                edges = self._quantile_edges([x for x, _ in xs], n_bins)
                for x, y in xs:
                    bins.setdefault(self._bin_of(float(x), edges), []).append(y)
            else:
                bins = {}
                for x, y in xs:
                    bins.setdefault(x, []).append(y)

            if len(bins) < 2:
                imp[name] = 0.0
                continue

            # eta-squared = SS_between / SS_total, computed the textbook way.
            grand = sum(scores) / len(scores)
            ss_between = 0.0
            for ys in bins.values():
                m = sum(ys) / len(ys)
                ss_between += len(ys) * (m - grand) ** 2
            ss_total = sum((s - grand) ** 2 for s in scores)
            imp[name] = round(ss_between / ss_total, 4) if ss_total > 0 else 0.0

        ranking = sorted(imp, key=lambda k: imp[k], reverse=True)
        return {"ok": True, "importance": imp, "ranking": ranking,
                "under_budget": under_budget, "n_trials_used": len(usable),
                "method": "variance_ratio"}

    @staticmethod
    def _quantile_edges(values: List[float], n_bins: int) -> List[float]:
        """Internal bin edges splitting ``values`` into n_bins equal-count bins."""
        vs = sorted(float(v) for v in values)
        edges = [vs[int(i * (len(vs) - 1) / n_bins)] for i in range(1, n_bins)]
        uniq: List[float] = []
        for e in edges:
            if not uniq or e > uniq[-1]:
                uniq.append(e)
        return uniq

    @staticmethod
    def _bin_of(x: float, edges: List[float]) -> int:
        """Which quantile bin ``x`` falls into (edges are internal boundaries)."""
        for b, e in enumerate(edges):
            if x <= e:
                return b
        return len(edges)

    # -- the verdict ------------------------------------------------------- #

    def verdict(self, *, prob_real_bar: float = 0.95) -> Dict[str, Any]:
        """Best trial + deflation, in one honest package. Never raises.

        THE GUARD THAT MATTERS (added after audit finding C1)
        ----------------------------------------------------
        Deflation alone is NOT a sufficient bar, and treating it as one produced
        a confident false positive: with the search returning the baseline as its
        best trial, the engine still reported ESTABLISHED with prob_real=1.0.
        The deflated Sharpe answers "could this number be the lucky maximum of N
        draws?" - it does NOT answer "did the search actually find anything?".

        Two additional gates are therefore applied before any verdict is
        promoted above NO EDGE FOUND:

        * BASELINE GATE. Trial 0 is always the production defaults (engine.py
          guarantees this). If the winner IS trial 0, the search found no
          parameter set better than what is already running. Calling that
          ESTABLISHED is indefensible regardless of what the Sharpe formula says.
        * IMPROVEMENT GATE. If the best trial's score is not strictly greater
          than the baseline's, there is no improvement to recommend.

        Both gates require a *scoreable* baseline. If trial 0 failed to evaluate
        there is no honest comparison to make, so the gates do not fire and the
        run is flagged ``baseline_missing`` instead - the operator needs to know
        the before/after the report is built on does not exist.
        """
        best = self.best()
        if best is None or best.value is None:
            return {"ok": False, "verdict": "NO EDGE FOUND",
                    "reason": "no scoreable trial",
                    "n_trials": len(self.trials), "n_ok": self.n_ok}

        # --- the baseline gate (trial 0 is the production defaults) -------- #
        baseline = next((t for t in self.trials if t.number == 0 and t.ok
                         and t.value is not None), None)
        baseline_missing = baseline is None
        no_improvement = (baseline is not None
                          and best.value is not None
                          and best.value <= baseline.value)

        if not baseline_missing and (best.number == 0 or no_improvement):
            return {"ok": False, "verdict": "NO EDGE FOUND",
                    "reason": ("the best trial is the production baseline - the "
                               "search found no parameter set better than the "
                               "current configuration"),
                    "best_trial": best.number,
                    "best_params": dict(best.params),
                    "best_score": best.value,
                    "best_mean_sharpe": best.mean_sharpe,
                    "baseline_trial": baseline.number,
                    "baseline_score": baseline.value,
                    "baseline_missing": False,
                    "n_trials": len(self.trials), "n_ok": self.n_ok,
                    "n_failed": self.n_failed}

        from ..ml.evaluation.metrics import deflated_sharpe
        # C4: ``best.n_folds`` is the number of FOLDS, not observations. Feeding
        # a hardcoded multiple of it fabricated the observation count and pushed
        # prob_real to 1.0. Use the folds count as-is; the objective stores the
        # real per-fold series length in ``n_obs`` when it is available.
        n_obs = int(getattr(best, "n_obs", 0) or 0) or max(2, (best.n_folds or 1))
        # The objective's score is an ANNUALISED-per-fold-mean quantity, so we
        # deflate the *mean* fold Sharpe (the reported number), not the raw
        # internal score - the score carries the stability penalty and is not a
        # Sharpe in the deflation formula's sense.
        observed = best.mean_sharpe if best.mean_sharpe is not None else best.value
        d = deflated_sharpe(float(observed), n_trials=max(1, self.n_ok),
                            n_obs=max(2, n_obs))
        prob = d.get("prob_real")
        if prob is None:
            verdict = "NOT COMPUTABLE"
        elif prob >= prob_real_bar:
            verdict = "ESTABLISHED"
        elif prob >= 0.80:
            verdict = "SUGGESTIVE"
        else:
            verdict = "NOT ESTABLISHED"
        out = {"ok": True, "verdict": verdict,
               "best_trial": best.number,
               "best_params": dict(best.params),
               "best_score": best.value,
               "best_mean_sharpe": best.mean_sharpe,
               "deflation": d,
               "prob_real_bar": prob_real_bar,
               "baseline_missing": baseline_missing,
               "n_trials": len(self.trials), "n_ok": self.n_ok,
               "n_failed": self.n_failed}
        if baseline is not None:
            out["baseline_trial"] = baseline.number
            out["baseline_score"] = baseline.value
            out["improvement_vs_baseline"] = round(best.value - baseline.value, 6)
        return out

    # -- persistence ------------------------------------------------------- #

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "space_name": self.space_name,
            "strategy": self.strategy,
            "objective_version": self.objective_version,
            "seed": self.seed, "meta": dict(self.meta),
            "started": self.started, "wall_seconds": round(self.wall_seconds, 2),
            "objective_stats": dict(self.meta.get("objective_stats", {})),
            "verdict": self.verdict(),
            "importance": self.param_importance(),
            "trials": [t.to_dict() for t in self.trials],
        }
