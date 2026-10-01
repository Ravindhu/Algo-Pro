"""
optimizer/objective.py - one parameter set in, one honest Sharpe out.

WHAT "THE OBJECTIVE" MEANS HERE
-------------------------------
The optimiser maximises a single number per trial. Everything about whether the
final answer is trustworthy is decided in this file, because this is where the
number is produced. Three decisions, each with a known failure mode:

1. **Walk-forward, not one fit.** A parameter set scored on the whole sample is
   scored on data it was chosen against. ``ml/evaluation/splits.py`` provides
   leak-safe fold geometry (purge + embargo) and we reuse it rather than
   re-deriving fold boundaries here — one implementation, one place to audit.

2. **The aggregate is the MEAN MINUS A STABILITY PENALTY, not the mean.**
   A configuration with fold Sharpes ``[1.8, 1.7, 0.1, 1.9]`` and one with
   ``[1.4, 1.3, 1.35, 1.4]`` have the same mean, but only the second is a
   strategy. Optimising the bare mean reliably selects the first one, because
   a single lucky fold is enough to win the search. The penalty makes fold
   *dispersion* cost the trial something, which is the cheapest available proxy
   for "will this survive a regime change".

   ``score = mean - STABILITY_PENALTY * std``

3. **Costs are charged inside the strategy, and the gate is checked here.**
   The strategy callable returns returns that are already net (it is told the
   cost via ``ctx``), so the Sharpe is a net Sharpe. On top of that the mean
   per-period edge is passed through ``metrics.cost_adjusted_edge`` as an
   explicit *gate*, because a Sharpe can stay positive on a negative net edge
   when the wins are large and rare — exactly the profile that dies live.

THE SCORE IS NOT THE REPORTED SHARPE
------------------------------------
``score`` is the optimiser's internal ranking key. The number quoted to a human
is the **deflated** Sharpe of the best trial (see ``study.py`` / ``report.py``).
The two are deliberately different: the search needs a smooth, comparable key;
the decision needs a number corrected for the search itself.

WHY THE OBJECTIVE IS A CLASS AND NOT A FUNCTION
-----------------------------------------------
It holds the fold plan, the strategy callable, the cost assumption and the
cache. A cache matters more than it looks: TPE frequently re-suggests a nearby
parameter set, and re-running a 600-bar strategy for an identical input is pure
waste in a multi-hour study. The cache key is the canonicalised parameter dict,
so two dicts that differ only in key order hit the same entry.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import config, logger
from .strategies import StrategyFn, call_strategy

__all__ = ["ObjectiveResult", "Objective", "STABILITY_PENALTY"]


#: How hard fold dispersion is penalised. 0.5 means "one std of fold-to-fold
#: variation costs half a Sharpe". Chosen so a configuration whose folds straddle
#: zero is not rescued by a single strong fold, without over-penalising the
#: normal sampling noise of a 4-5 fold walk-forward.
STABILITY_PENALTY = 0.5


@dataclass
class ObjectiveResult:
    """Everything one evaluated parameter set produced.

    Fields are named so that a rejected trial and a successful one have the
    same shape - the engine and the plots then never need to special-case
    "trial that failed", which is where most reporting bugs come from.
    """

    params: Dict[str, Any]
    #: The optimiser's ranking key (higher is better). None when not scoreable.
    score: Optional[float] = None
    ok: bool = False
    reason: Optional[str] = None

    # --- the honest numbers (filled when ok) ------------------------------ #
    mean_sharpe: Optional[float] = None
    std_sharpe: Optional[float] = None
    min_sharpe: Optional[float] = None
    median_sharpe: Optional[float] = None
    n_folds: int = 0
    #: Total return observations across all scored folds - the real sample size
    #: for the deflated-Sharpe correction (audit finding C4).
    n_obs: int = 0
    fold_sharpes: List[Optional[float]] = field(default_factory=list)
    total_return: Optional[float] = None
    max_drawdown: Optional[float] = None
    turnover: Optional[float] = None
    cost_gate: Dict[str, Any] = field(default_factory=dict)
    #: Per-fold equity/return diagnostics, kept for the drill-down tables.
    folds: List[Dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0
    cached: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "params": dict(self.params),
            "score": self.score,
            "ok": self.ok,
            "reason": self.reason,
            "mean_sharpe": self.mean_sharpe,
            "std_sharpe": self.std_sharpe,
            "min_sharpe": self.min_sharpe,
            "median_sharpe": self.median_sharpe,
            "n_folds": self.n_folds,
            "n_obs": self.n_obs,
            "fold_sharpes": list(self.fold_sharpes),
            "total_return": self.total_return,
            "max_drawdown": self.max_drawdown,
            "turnover": self.turnover,
            "cost_gate": dict(self.cost_gate),
            "folds": list(self.folds),
            "seconds": round(self.seconds, 4),
            "cached": self.cached,
        }


def _stats(values: Sequence[float]) -> Tuple[Optional[float], Optional[float],
                                             Optional[float], Optional[float]]:
    """mean, std (population), min, median for a small list. Pure Python."""
    xs = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    n = len(xs)
    if n == 0:
        return None, None, None, None
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / n if n > 1 else 0.0
    std = math.sqrt(var)
    ordered = sorted(xs)
    mid = n // 2
    median = (ordered[mid] if n % 2 else
              0.5 * (ordered[mid - 1] + ordered[mid]))
    return mean, std, min(xs), median


class Objective:
    """Turns a parameter dict into a score, using walk-forward folds.

    Args:
        strategy: the callable from ``strategies.resolve_strategy``.
        space: the :class:`~blue_engine.optimizer.search_space.SearchSpace`,
            used to *validate before running*. Rejecting an illegal parameter
            set here costs microseconds; discovering it inside a strategy costs
            a traceback in the middle of a long study.
        periods: bars/periods handed to the strategy per fold.
        n_folds: walk-forward folds. 4 is the floor we allow: 3 folds cannot
            distinguish "consistently mediocre" from "one good, two bad".
        seed: passed into the strategy so a strategy that draws randomness is
            still deterministic per trial. Reproducibility is not optional in
            research - an unreproducible optimum cannot be acted on.
        cost_bps: round-trip cost assumption, in bps.
    """

    def __init__(self, strategy: StrategyFn, space: Any = None, *,
                 periods: int = 600,
                 n_folds: int = 4,
                 seed: int = 7,
                 cost_bps: float = config.COST_BPS_ROUND_TRIP,
                 min_fold_sharpe: Optional[float] = None,
                 cache_size: int = 4096) -> None:
        self.strategy = strategy
        self.space = space
        self.periods = max(60, int(periods))
        self.n_folds = max(2, int(n_folds))
        self.seed = int(seed)
        self.cost_bps = float(cost_bps)
        #: Optional hard floor: any fold below this makes the trial fail rather
        #: than merely score badly. Off by default (None) because a legitimate
        #: strategy can have one bad fold; turning it on is a policy choice.
        self.min_fold_sharpe = min_fold_sharpe
        self._cache: Dict[str, ObjectiveResult] = {}
        self._cache_size = int(cache_size)
        self.n_evaluated = 0
        self.n_rejected = 0
        self.n_cache_hits = 0

    # -- cache ------------------------------------------------------------- #

    @staticmethod
    def _key(params: Dict[str, Any]) -> str:
        """Canonical cache key. Sorted keys so dict order cannot split the cache."""
        return json.dumps(params, sort_keys=True, default=str)

    def _cache_get(self, key: str) -> Optional[ObjectiveResult]:
        hit = self._cache.get(key)
        if hit is not None:
            self.n_cache_hits += 1
            # Return a copy marked as cached, so a caller mutating the result
            # cannot corrupt the cached one.
            import copy
            out = copy.deepcopy(hit)
            out.cached = True
            return out
        return None

    def _cache_put(self, key: str, res: ObjectiveResult) -> None:
        # Simple bound: a study is bounded in trials anyway, and this only
        # guards a pathological long-running process.
        if len(self._cache) >= self._cache_size:
            self._cache.clear()
        self._cache[key] = res

    # -- the evaluation ---------------------------------------------------- #

    def evaluate(self, params: Dict[str, Any],
                  bars: Optional[List[Dict[str, Any]]] = None
                  ) -> ObjectiveResult:
        """Score one parameter set. Never raises; failures come back as ok=False.

        The optional ``bars`` argument (Phase 3 of
        ``Temp/OPTIMIZER_HEADER_PLAN.md``) feeds real cached bars into
        the strategy instead of the synthetic series. When ``bars`` is
        provided, the list is split into ``n_folds`` disjoint windows
        and each window is passed to the strategy callable as its
        third argument. The strategy must declare a ``bars`` parameter
        to receive the data; legacy two-argument strategies ignore it
        (see ``blue_engine.optimizer.strategies.call_strategy``).

        Backward compatible: the existing 37 tests that pass only
        ``params`` keep working - the synthetic series is the default.
        """
        import time
        started = time.time()

        key = self._key(params)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        # --- 1. legality first (cheap) ------------------------------------ #
        if self.space is not None:
            ok, reasons = self.space.validate(params)
            if not ok:
                self.n_rejected += 1
                res = ObjectiveResult(params=dict(params), ok=False,
                                      reason="invalid parameters: "
                                             + "; ".join(reasons),
                                      seconds=time.time() - started)
                self._cache_put(key, res)
                return res

        # --- 2. run the folds -------------------------------------------- #
        fold_returns: List[List[float]] = []
        folds: List[Dict[str, Any]] = []
        errors: List[str] = []

        # We split the *period budget* across folds rather than generating one
        # long series and slicing it. That is deliberate here: strategies in this
        # system are called with a (params, ctx) contract rather than a data
        # frame, so the fold boundary is expressed as a per-fold seed+length.
        # The effect is the same - each fold's returns are produced from a
        # disjoint segment - and it keeps the objective independent of how a
        # given strategy obtains its data.
        per_fold = max(30, self.periods // self.n_folds)

        # When real bars are provided, split them into n_folds disjoint
        # windows of approximately equal size. Each window is passed as
        # the strategy's third argument. ``fold_bars[k]`` is the bar
        # slice for fold ``k``; ``None`` when no real bars are
        # available, in which case the strategy falls back to the
        # synthetic series it derives from ``ctx["seed"]``.
        #
        # Note: an EMPTY list is treated as "bars were provided but the
        # window has no rows" - it is NOT the same as ``bars=None``.
        # The slicing still runs and produces a list of empty
        # per-fold lists, so a 3-arg strategy can decide what to do
        # with an empty window (e.g. fall back to its synthetic
        # series). ``bars is not None`` is the gate, not truthiness.
        fold_bars: List[Optional[List[Dict[str, Any]]]] = [None] * self.n_folds
        if bars is not None:
            n = len(bars)
            chunk = max(1, n // self.n_folds)
            for k in range(self.n_folds):
                start = k * chunk
                end = (k + 1) * chunk if k < self.n_folds - 1 else n
                fold_bars[k] = bars[start:end] if start < n else []

        for k in range(self.n_folds):
            ctx = {
                "seed": self.seed + 1000 * k,
                "periods": per_fold,
                "cost_bps": self.cost_bps,
                "fold": k,
                "n_folds": self.n_folds,
            }
            try:
                out = call_strategy(self.strategy, dict(params), ctx,
                                     bars=fold_bars[k])
            except Exception as exc:  # noqa: BLE001 - a strategy bug is a datum here
                msg = f"fold {k}: {type(exc).__name__}: {exc}"
                errors.append(msg)
                folds.append({"fold": k, "error": msg})
                continue
            if not isinstance(out, dict):
                errors.append(f"fold {k}: strategy returned {type(out).__name__}, "
                              "expected dict")
                folds.append({"fold": k, "error": "bad return type"})
                continue
            if not out.get("ok", False):
                errors.append(f"fold {k}: {out.get('reason', 'strategy reported not ok')}")
                folds.append({"fold": k, "error": out.get("reason", "not ok")})
                continue
            rets = [float(r) for r in (out.get("returns") or [])
                    if r is not None and math.isfinite(float(r))]
            if len(rets) < 20:
                errors.append(f"fold {k}: only {len(rets)} returns (need >= 20)")
                folds.append({"fold": k, "error": "too few returns"})
                continue
            fold_returns.append(rets)

        if not fold_returns:
            self.n_rejected += 1
            res = ObjectiveResult(
                params=dict(params), ok=False,
                reason="no fold produced a scoreable return series: "
                       + ("; ".join(errors[:3]) or "unknown"),
                n_folds=self.n_folds, seconds=time.time() - started)
            self._cache_put(key, res)
            return res

        # --- 3. per-fold metrics ----------------------------------------- #
        from ..ml.evaluation.metrics import max_drawdown, sharpe

        fold_sharpes: List[Optional[float]] = []
        all_rets: List[float] = []
        inactive_folds: List[int] = []
        for k, rets in enumerate(fold_returns):
            periods_per_year = float(len(rets))  # one "period" per fold bar
            sh = sharpe(rets, periods_per_year=max(2.0, periods_per_year))
            fold_sharpes.append(sh)
            all_rets.extend(rets)
            # Equity path for the fold, for drawdown and the drill-down chart.
            eq = [1.0]
            for r in rets:
                eq.append(eq[-1] * (1.0 + r))
            # AUDIT FINDING C8: a fold where the strategy never took a position
            # has zero return variance, so ``sharpe`` returns None. That is NOT
            # the same as "no data" - it is a fold in which the strategy did
            # nothing, and it must be counted as a failure, not skipped.
            traded = any(abs(r) > 0.0 for r in rets)
            if not traded:
                inactive_folds.append(k)
            folds.append({
                "fold": k,
                "n_returns": len(rets),
                "traded": traded,
                "sharpe": None if sh is None else round(sh, 4),
                "total_return": round(eq[-1] - 1.0, 6),
                "max_drawdown": (None if max_drawdown(eq) is None
                                 else round(max_drawdown(eq), 6)),
            })

        # --- C8: an inactive fold is a FAILED fold, not an absent one ------ #
        # The old code did ``usable = [s for s in fold_sharpes if s is not None]``
        # and scored on whatever survived. A parameter set that traded in only 1
        # of 4 folds was therefore scored on that single fold and could win the
        # whole search - while its dispersion was computed from the survivors, so
        # it also LOOKED low-variance. That is an edge-inflation bug: it rewards
        # idleness (a strategy that does not trade cannot lose) and it was the
        # actual reason the search never converged on the synthetic fixture.
        #
        # We keep the mean over the folds that DID score, but the mean is now
        # discounted in proportion to how much of the sample the strategy sat
        # out, and any inactive fold counts as a zero-Sharpe fold for the
        # dispersion and floor tests. The discount is deliberately proportional
        # (not a flat penalty) so a strategy that trades in 3 of 4 folds is only
        # mildly docked, while one that trades in 1 of 4 is heavily docked.
        scored = [s for s in fold_sharpes if s is not None]
        if not scored:
            self.n_rejected += 1
            res = ObjectiveResult(
                params=dict(params), ok=False,
                reason=("no fold produced a scoreable Sharpe"
                        + (f" ({len(inactive_folds)} of {self.n_folds} folds "
                           "took no position)" if inactive_folds else "")),
                n_folds=0, fold_sharpes=fold_sharpes,
                folds=folds, seconds=time.time() - started)
            self._cache_put(key, res)
            return res

        mean_sh, _std_scored, _min_scored, _med_scored = _stats(scored)

        # Activity-weighted aggregate: treat every inactive fold as a 0.0 fold.
        # This is what makes the number comparable ACROSS parameter sets that
        # traded in different numbers of folds.
        activity = len(scored) / float(self.n_folds)
        padded = [s if s is not None else 0.0 for s in fold_sharpes]
        mean_sh, std_sh, min_sh, med_sh = _stats(padded)

        # --- 4. the score: mean minus a dispersion penalty ----------------- #
        score = mean_sh - STABILITY_PENALTY * (std_sh or 0.0)

        #: Number of folds that actually produced a scoreable result. Kept on
        #: the result so a caller can tell "4 of 4" from "2 of 4" without
        #: re-deriving it from the fold list.
        n_scored_folds = len(scored)

        # --- 5. the cost gate -------------------------------------------- #
        # Mean per-period simple return -> bps. Turnover is approximated as 1
        # position change per period, which is the conservative assumption for a
        # strategy that flips; a strategy that holds longer genuinely trades less
        # but reporting the conservative case stops the gate flattering it.
        from ..ml.evaluation.metrics import cost_adjusted_edge
        mean_ret = (sum(all_rets) / len(all_rets)) if all_rets else 0.0
        gate = cost_adjusted_edge(mean_ret * 10000.0,
                                  turnover_per_period=1.0,
                                  cost_bps_per_turnover=self.cost_bps)

        # --- 6. optional hard floor -------------------------------------- #
        # NOTE: ``min_sh`` is now taken over the ACTIVITY-PADDED fold list, so an
        # inactive fold contributes 0.0 and correctly trips a positive floor.
        if self.min_fold_sharpe is not None and min_sh is not None:
            if min_sh < self.min_fold_sharpe:
                self.n_rejected += 1
                res = ObjectiveResult(
                    params=dict(params), ok=False,
                    reason=(f"worst fold Sharpe {min_sh:.3f} < floor "
                            f"{self.min_fold_sharpe}"
                            + (f" ({len(inactive_folds)} inactive fold(s))"
                               if inactive_folds else "")),
                    mean_sharpe=mean_sh, std_sharpe=std_sh, min_sharpe=min_sh,
                    median_sharpe=med_sh, n_folds=n_scored_folds,
                    fold_sharpes=fold_sharpes, cost_gate=gate, folds=folds,
                    seconds=time.time() - started)
                self._cache_put(key, res)
                return res

        eq_all = [1.0]
        for r in all_rets:
            eq_all.append(eq_all[-1] * (1.0 + r))
        dd = max_drawdown(eq_all)

        self.n_evaluated += 1
        res = ObjectiveResult(
            params=dict(params),
            score=round(score, 6),
            ok=True,
            mean_sharpe=round(mean_sh, 6),
            std_sharpe=None if std_sh is None else round(std_sh, 6),
            min_sharpe=None if min_sh is None else round(min_sh, 6),
            median_sharpe=None if med_sh is None else round(med_sh, 6),
            n_folds=n_scored_folds,
            n_obs=len(all_rets),
            fold_sharpes=fold_sharpes,
            total_return=round(eq_all[-1] - 1.0, 6),
            max_drawdown=None if dd is None else round(dd, 6),
            turnover=1.0,
            cost_gate=gate,
            folds=folds,
            seconds=time.time() - started,
        )
        self._cache_put(key, res)
        return res

    def __call__(self, params: Dict[str, Any]) -> ObjectiveResult:
        """Allow the objective to be passed around as a plain callable."""
        return self.evaluate(params)

    # -- introspection ----------------------------------------------------- #

    def stats(self) -> Dict[str, Any]:
        return {
            "evaluated": self.n_evaluated,
            "rejected": self.n_rejected,
            "cache_hits": self.n_cache_hits,
            "cache_size": len(self._cache),
            "n_folds": self.n_folds,
            "periods": self.periods,
            "cost_bps": self.cost_bps,
        }
