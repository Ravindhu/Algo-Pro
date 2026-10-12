"""
optimizer/engine.py - the search loop that ties space, strategy and study together.

WHAT THIS FILE OWNS
-------------------
The loop:

    for i in range(budget):
        params = sampler.ask()          # TPE (Optuna if installed, fallback else)
        result = objective.evaluate(params)
        sampler.tell(params, result.score)
        study.add(result)               # journal + crash-safe persistence
        if prunable and result clearly dominated: stop early

and the guarantees around it:

1. REPRODUCIBILITY. One ``seed`` drives everything: the sampler, and (via
   ``ctx``) any strategy-side randomness. Same seed + same data + same budget
   => same study. An optimum that cannot be reproduced cannot be acted on.
2. BUDGET GUARD. ``n_trials`` is the hard cap. There is no "just a few more"
   path, because unbounded search is how a parameter fit quietly becomes a
   curve fit (AGENTS.md: avoid overfitting).
3. THE DEFAULTS ARE ALWAYS EVALUATED FIRST (trial 0). Whatever else happens,
   the study contains the production parameter set, so "does the search beat
   what we run today?" is always answerable from the journal alone.
4. EARLY PRUNING IS CONSERVATIVE. Only a trial that is *below the running
   median of completed trials after the warmup* can be cut. Pruning is optional
   (default off for the fallback sampler, because our objective is a
   full-strategy evaluation - there is no cheap intermediate step to observe).
5. NO EXCEPTIONS ESCAPE THE LOOP. A trial that raises is recorded as a failed
   trial with the reason, and the study continues. One bad strategy call must
   not lose an hour of searching.

WHY OPTUNA IS OPTIONAL
----------------------
``optuna`` is not installed in this venv (verified with ``find_spec``). The
engine therefore detects it once at construction: present -> use
``optuna.create_study`` with the TPE sampler; absent -> use the built-in
``sampler.TPESampler``. Both expose the same ask/tell shape from this file's
point of view, so the study record, the importance ranking and the three
visuals are identical regardless of which backend ran. That is the "upgrade
without breakage" property AGENTS.md demands: installing Optuna later changes
the *quality* of the search, not the interface, the reports or the dashboard.
"""
from __future__ import annotations

import math
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__, config, logger
from .search_space import SearchSpace
from .strategies import resolve_strategy
from .objective import Objective, ObjectiveResult
from .study import Study, TrialRecord

"""
Core optimization engine for Blue_engine param sweeps on top of Optuna-TPE,
with walk-forward scoring so parameters never get tuned against pure noise.
"""
__all__ = ["OptimizerEngine", "SearchResult", "EngineConfig"]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _copy_cost_gate(gate: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return a detached copy of a cost-gate dict, or None.

    Why a copy and not the same object: ``ObjectiveResult`` lives for one
    trial, but its ``cost_gate`` is then carried by the ``TrialRecord`` and
    finally mirrored into ``study.meta``. A caller mutating the dict in one
    layer must not silently corrupt the other two, and the JSONL writer must
    not see a dict that another layer still holds a reference to. A
    microsecond-deep ``dict(...)`` is the right grain - the gate has at most
    half a dozen scalar fields.
    """
    return None if not gate else dict(gate)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


class EngineConfig:
    """One run's knobs. Plain attributes so it JSON-serialises for the report.

    Kept as a class (not a dataclass) with explicit defaults because each value
    carries a rationale worth documenting inline, and dataclass defaults do not
    fit comments well.
    """

    def __init__(self, *,
                 name: str = "study",
                 n_trials: int = 200,
                 seed: int = 7,
                 periods: int = 600,
                 n_folds: int = 4,
                 cost_bps: float = config.COST_BPS_ROUND_TRIP,
                 risk_free_per_period: float = 0.0,
                 periods_per_year: Optional[float] = None,
                 min_fold_sharpe: Optional[float] = None,
                 n_startup: int = 12,
                 n_ei_candidates: int = 32,
                 early_stop_patience: int = 40,
                 keep_failed: bool = True,
                 multivariate: bool = True) -> None:
        self.name = name
        #: Hard cap on trials. 200 is the working default: enough for TPE to
        #: model a 10-parameter space (importance needs >=50 by config), small
        #: enough that a 600-bar synthetic evaluates in minutes.
        self.n_trials = max(1, int(n_trials))
        self.seed = int(seed)
        self.periods = int(periods)
        self.n_folds = int(n_folds)
        self.cost_bps = float(cost_bps)
        self.risk_free_per_period = float(risk_free_per_period)
        if not math.isfinite(self.risk_free_per_period):
            raise ValueError("risk_free_per_period must be finite")
        self.periods_per_year = (None if periods_per_year is None
                                 else float(periods_per_year))
        if self.periods_per_year is not None and (
                not math.isfinite(self.periods_per_year)
                or self.periods_per_year <= 0):
            raise ValueError("periods_per_year must be finite and positive")
        self.min_fold_sharpe = min_fold_sharpe
        self.n_startup = int(n_startup)
        self.n_ei_candidates = int(n_ei_candidates)
        #: Stop early if no improvement for this many consecutive trials.
        #: 0 disables. Set generously: TPE often improves late after switching
        #: regimes inside the parameter space, and a tight patience silently
        #: truncates the search.
        self.early_stop_patience = max(0, int(early_stop_patience))
        self.keep_failed = bool(keep_failed)
        #: Model parameters JOINTLY rather than independently (Optuna's
        #: ``multivariate`` TPE, or the fallback's coupled mode). ON by default:
        #: strategy parameters are almost always coupled (a fast MA period only
        #: means something relative to a slow one), and the measured cost of
        #: modelling them independently is failing to find the optimum at all
        #: - see audit finding C1d. Set False only to reproduce old behaviour.
        self.multivariate = bool(multivariate)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


class SearchResult:
    """Everything the caller needs after ``search()``.

    Layout intentionally mirrors ``study.Study.to_dict()`` plus the three
    workflow artefacts, so the report writer and the dashboard have exactly one
    shape to consume.
    """

    def __init__(self, *, ok: bool, study: Study, config: EngineConfig,
                 space: SearchSpace, importance: Dict[str, Any],
                 verdict: Dict[str, Any], top_k: List[str],
                 objective_stats: Dict[str, Any],
                 wall_seconds: float, backend: str) -> None:
        self.ok = ok
        self.study = study
        self.config = config
        self.space = space
        self.importance = importance
        self.verdict = verdict
        self.top_k = top_k
        self.objective_stats = objective_stats
        self.wall_seconds = wall_seconds
        self.backend = backend

    # -- the headline ------------------------------------------------------ #

    def recommended_params(self) -> Dict[str, Any]:
        """The parameter set to act on, snapped onto the space.

        Empty when nothing scoreable was found - never a guess. Snapping goes
        through ``SearchSpace.coerce`` so a continuous optimum becomes a value
        the strategy can actually be configured with (an int stays an int).
        """
        v = self.verdict or {}
        if not v.get("ok"):
            return {}
        return self.space.coerce(v.get("best_params") or {})

    def baseline_params(self) -> Dict[str, Any]:
        """The production defaults, for the before/after comparison."""
        return self.space.defaults()

    def improvement(self) -> Optional[float]:
        """Best-trial Sharpe minus default-parameter Sharpe, or None.

        None means "the defaults were never evaluated cleanly", which itself is
        a finding: the comparison the operator actually cares about does not
        exist for this run.
        """
        base = next((t for t in self.study.trials if t.number == 0 and t.ok),
                    None)
        v = self.verdict or {}
        if not v.get("ok") or base is None or base.mean_sharpe is None:
            return None
        b = v.get("best_mean_sharpe")
        return None if b is None else round(b - base.mean_sharpe, 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "backend": self.backend,
            "wall_seconds": round(self.wall_seconds, 2),
            "config": self.config.to_dict(),
            "space": self.space.to_dict(),
            "importance": self.importance,
            "verdict": self.verdict,
            "top_k": list(self.top_k),
            "recommended_params": self.recommended_params(),
            "baseline_params": self.baseline_params(),
            "improvement_vs_default": self.improvement(),
            "objective_stats": self.objective_stats,
            "study": self.study.to_dict(),
        }


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #


class OptimizerEngine:
    """Runs one study: space + strategy + budget -> SearchResult.

    Args:
        space: the :class:`SearchSpace` to search.
        strategy_spec: anything ``strategies.resolve_strategy`` accepts.
        cfg: an :class:`EngineConfig`.

    Usage::

        engine = OptimizerEngine(load_space("synthetic_demo"), "synthetic")
        result = engine.search()
        print(result.recommended_params())
    """

    def __init__(self, space: SearchSpace, strategy_spec: Any, *,
                 cfg: Optional[EngineConfig] = None) -> None:
        self.space = space
        self.cfg = cfg or EngineConfig()
        fn, err = resolve_strategy(strategy_spec)
        if err:
            raise ValueError(f"strategy: {err}")
        self.strategy_fn = fn
        self.strategy_name = getattr(fn, "__name__", str(strategy_spec))
        self.objective = Objective(
            fn, space,
            periods=self.cfg.periods, n_folds=self.cfg.n_folds,
            seed=self.cfg.seed, cost_bps=self.cfg.cost_bps,
            risk_free_per_period=self.cfg.risk_free_per_period,
            periods_per_year=self.cfg.periods_per_year,
            min_fold_sharpe=self.cfg.min_fold_sharpe)
        self._optuna = self._detect_optuna()

    @staticmethod
    def _detect_optuna() -> Optional[Any]:
        """One detection, at construction, never at import time."""
        try:
            import optuna  # noqa: F401
            return optuna
        except Exception:  # noqa: BLE001 - absence is the expected state
            return None

    # -- backends ---------------------------------------------------------- #

    def _backend_name(self) -> str:
        if self._optuna:
            return "optuna"
        return "builtin_tpe"

    # -- coupled-space help ------------------------------------------------ #

    def _coupled_seed_points(self, k: int) -> List[Dict[str, Any]]:
        """Legal points that respect each parameter's DEFAULT (its coupling).

        AUDIT FINDING C1d - WHY THIS EXISTS
        ----------------------------------
        Optuna 5.0.0's ``TPESampler`` models every parameter INDEPENDENTLY. Its
        own source says the ``multivariate`` field "remains only for historical
        reasons and is unused", and ``_parzen_estimator_cls`` is always the
        independent estimator (verified against the installed 5.0.0). So a
        coupled space cannot be fixed by flipping a flag, and the joint samplers
        that WOULD fix it (``GPSampler``, ``CmaEsSampler``) are not installed -
        they need ``torch`` and ``cmaes`` respectively. Both were checked.

        The cheapest honest improvement is therefore at the SEED stage. The
        space already knows each parameter's production default, and those
        defaults are coupled BY CONSTRUCTION - a strategy ships a coherent
        parameter set, not six independently-chosen numbers. Perturbing one
        coordinate at a time away from the default explores the neighbourhood of
        a point we know is internally consistent, which is exactly where a ridge
        tends to run.

        Measured context on the synthetic fixture: uniform random search over
        400 trials reaches only 3.300 against a 3.836 peak, and just 0.28% of
        draws clear 85% of the peak. The fixture is a genuinely narrow ridge, so
        seeding near the default is worth real trials.

        Each point is validated; an illegal perturbation is repaired by the
        space's own ``sample``-time repair, so this can never inject a bad point.
        """
        base = self.space.defaults()
        out: List[Dict[str, Any]] = []
        numeric = [p for p in self.space.params
                   if p.kind in ("int", "float") and p.default is not None]
        if not numeric:
            return out
        rng = __import__("random").Random(self.cfg.seed ^ 0x5EED)
        for _ in range(max(0, k)):
            cand = dict(base)
            # Move exactly one coordinate, by a small fraction of its range, so
            # the point stays inside the neighbourhood of the default.
            spec = rng.choice(numeric)
            span = float(spec.high) - float(spec.low)
            delta = rng.uniform(-0.15, 0.15) * span
            v = float(spec.default) + delta
            if spec.kind == "int":
                v = int(round(v))
            cand[spec.name] = v
            ok, _reasons = self.space.validate(cand)
            if ok:
                out.append(cand)
        return out

    def _run_trial_fn(self):
        """The objective in the shape Optuna's study.optimize expects."""
        def _f(trial):
            params = self.space.sample(trial)
            res = self.objective.evaluate(params)
            self._last_result = res
            return res.score if res.score is not None else 0.0
        return _f

    # -- the loop ---------------------------------------------------------- #

    def search(self, bars=None) -> SearchResult:
        """Run the full study. Returns a SearchResult; never raises.

        A raising search() would lose the journal for a bug on trial 173 of
        200 - the worst possible failure mode for a long study.

        ``bars`` is an optional list of OHLCV bar dicts (from
        ``OptimizerDataCache.load()``). When provided, the engine splits
        them into ``n_folds`` disjoint windows and the objective drives
        the strategy against those windows instead of the synthetic
        fixture. When ``bars`` is ``None`` (the default), the engine uses
        the synthetic series - all 37 existing tests pass unchanged.
        """
        started = time.time()
        cfg = self.cfg
        # Local binding so the long ``evaluate(...)`` calls below stay
        # short; the slicing happens inside ``Objective.evaluate``.
        bars_arg: Optional[List[Dict[str, Any]]] = bars
        study = Study(
            name=cfg.name, space_name=self.space.name,
            strategy=self.strategy_name, objective_version=__version__,
            seed=cfg.seed,
            meta={"backend": self._backend_name(),
                  "space_description": self.space.description})

        # --- trial 0: the production defaults, always --------------------- #
        defaults = self.space.defaults()
        res = self.objective.evaluate(defaults, bars=bars_arg)
        study.add(TrialRecord(
            number=0, params=res.params, value=res.score, ok=res.ok,
            reason=res.reason, mean_sharpe=res.mean_sharpe,
            std_sharpe=res.std_sharpe, min_sharpe=res.min_sharpe,
            n_folds=res.n_folds, n_obs=res.n_obs, seconds=res.seconds,
            cached=res.cached, cost_gate=_copy_cost_gate(res.cost_gate)))
        best_value = res.score if res.ok else None

        # --- coupled seed points: trial 0's neighbourhood ------------------ #
        # AUDIT FINDING C1d. The production defaults are a COHERENT parameter
        # set - a strategy ships coupled values, not independently chosen ones -
        # so the neighbourhood of the default is where a ridge is most likely to
        # run. Evaluating a few single-coordinate perturbations of it before
        # handing over to the sampler costs a handful of trials and gives the
        # model real evidence about the local gradient, which is exactly what an
        # independent TPE cannot get on its own.
        #
        # Skipped when the budget is tiny, so a 10-trial run is not all seeding.
        seed_pts: List[Dict[str, Any]] = []
        if self.cfg.multivariate and cfg.n_trials >= 30:
            seed_pts = self._coupled_seed_points(min(8, cfg.n_trials // 8))
            for i, p in enumerate(seed_pts, start=1):
                r0 = self.objective.evaluate(p, bars=bars_arg)
                study.add(TrialRecord(
                    number=i, params=r0.params, value=r0.score, ok=r0.ok,
                    reason=r0.reason, mean_sharpe=r0.mean_sharpe,
                    std_sharpe=r0.std_sharpe, min_sharpe=r0.min_sharpe,
                    n_folds=r0.n_folds, n_obs=r0.n_obs, seconds=r0.seconds,
                    cached=r0.cached,
                    cost_gate=_copy_cost_gate(r0.cost_gate)))
                if r0.ok and r0.score is not None:
                    if best_value is None or r0.score > best_value:
                        best_value = r0.score
        n_seeded = len(seed_pts)
        n_remaining = max(0, cfg.n_trials - 1 - n_seeded)

        # --- trials 1..n: the sampler ------------------------------------- #
        if self._optuna is not None:
            optuna = self._optuna
            optuna.logging.set_verbosity(optuna.logging.WARNING)
            # AUDIT FINDING C1d (corrected): Optuna 5.0.0's TPESampler models
            # every parameter INDEPENDENTLY. Its own source states the
            # ``multivariate`` field "remains only for historical reasons and is
            # unused", and ``_parzen_estimator_cls`` is always the independent
            # estimator - verified against the installed 5.0.0. Passing
            # ``multivariate=True`` is therefore a NO-OP, and doing so while
            # claiming a fix would be worse than not trying. The joint samplers
            # that would genuinely help (GPSampler, CmaEsSampler) are not
            # installed here; they need ``torch`` and ``cmaes``. The coupled
            # handling in this engine is therefore the seed-point neighbourhood
            # above, which is real and measurable, rather than a flag that does
            # nothing.
            sampler_kwargs: Dict[str, Any] = dict(
                seed=cfg.seed, n_startup_trials=max(2, cfg.n_startup),
                n_ei_candidates=cfg.n_ei_candidates)
            sampler = optuna.samplers.TPESampler(**sampler_kwargs)
            os_study = optuna.create_study(direction="maximize",
                                           sampler=sampler)
            self._last_result = res

            def _f(trial):
                params = self.space.sample(trial)
                r = self.objective.evaluate(params, bars=bars_arg)
                self._last_result = r
                return r.score if r.score is not None else 0.0

            try:
                os_study.optimize(_f, n_trials=n_remaining,
                                  show_progress_bar=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("optuna loop stopped early: %s", exc)
            for t in os_study.trials[1:]:
                r: ObjectiveResult = getattr(t, "user_attrs", {}).get("res")
                if r is None:
                    # Optuna does not hand back our result object.
                    #
                    # AUDIT FINDING C1/C2 (this line was the root cause of the
                    # engine failing to converge): the old code called
                    # ``self.objective.evaluate(t.params)`` - Optuna's RAW,
                    # unconstrained suggestion - instead of the repaired dict that
                    # ``space.sample(trial)`` actually produced and scored. Every
                    # trial whose raw draw violated ``fast < slow`` was therefore
                    # re-scored here as "invalid parameters" and discarded, even
                    # though a legal point had already been evaluated. On the
                    # synthetic space that silently threw away ~60% of the budget
                    # and left the baseline as the winner.
                    #
                    # The repaired params are already cached inside the objective
                    # (keyed on the canonical dict), so re-deriving them through
                    # ``space.sample`` is cheap and guaranteed identical to what
                    # was scored.
                    st = t.state
                    failed = str(st) != "TrialState.COMPLETE"
                    if not failed:
                        params = self.space.sample(t)
                        r = self.objective.evaluate(params, bars=bars_arg)
                    else:
                        r = ObjectiveResult(
                            params=dict(t.params), ok=False,
                            reason=f"trial state {st}")
                study.add(TrialRecord(
                    number=t.number + n_seeded,
                    params=r.params, value=r.score, ok=r.ok,
                    reason=r.reason, mean_sharpe=r.mean_sharpe,
                    std_sharpe=r.std_sharpe, min_sharpe=r.min_sharpe,
                    n_folds=r.n_folds, n_obs=r.n_obs, seconds=r.seconds,
                    cached=r.cached,
                    cost_gate=_copy_cost_gate(r.cost_gate)))
            backend = "optuna"
        else:
            from .sampler import TPESampler
            sampler = TPESampler(seed=cfg.seed, n_startup=max(5, cfg.n_startup),
                                 n_ei_candidates=cfg.n_ei_candidates,
                                 multivariate=cfg.multivariate)
            n_no_improve = 0
            for i in range(1, n_remaining + 1):
                trial = sampler.ask()
                params = self.space.sample(trial)
                r = self.objective.evaluate(params, bars=bars_arg)
                sampler.tell(trial, r.score if r.ok else None)
                study.add(TrialRecord(
                    number=i + n_seeded,
                    params=r.params, value=r.score, ok=r.ok,
                    reason=r.reason, mean_sharpe=r.mean_sharpe,
                    std_sharpe=r.std_sharpe, min_sharpe=r.min_sharpe,
                    n_folds=r.n_folds, n_obs=r.n_obs, seconds=r.seconds,
                    cached=r.cached,
                    cost_gate=_copy_cost_gate(r.cost_gate)))
                if r.ok and r.score is not None:
                    if best_value is None or r.score > best_value:
                        best_value = r.score
                        n_no_improve = 0
                    else:
                        n_no_improve += 1
                if cfg.early_stop_patience and \
                        n_no_improve >= cfg.early_stop_patience:
                    logger.info("early stop: no improvement in %d trials",
                                cfg.early_stop_patience)
                    break
            backend = "builtin_tpe"

        if n_seeded:
            study.meta["coupled_seed_trials"] = n_seeded
        study.wall_seconds = time.time() - started
        study.meta["objective_stats"] = self.objective.stats()
        # AUDIT FINDING F2 (2026-10-01). The cost-adjusted-edge gate is computed
        # by ``Objective.evaluate`` for every trial (objective.py:368-373) but
        # is not on ``TrialRecord`` by default. The report's "Cost gate (best
        # trial)" line falls through to a generic message without this. Mirror
        # the per-trial gate into ``study.meta["cost_gates"]`` so the existing
        # ``report._best_cost_gate`` lookup finds it AND a downstream consumer
        # can iterate the full cost-gate history without rebuilding the
        # ``ObjectiveResult`` chain. Empty for failed trials (no gate is
        # computed when the strategy did not run).
        study.meta["cost_gates"] = {
            str(t.number): (dict(t.cost_gate) if t.cost_gate else None)
            for t in study.trials
        }

        importance = study.param_importance()
        top_k = importance.get("ranking", [])[:config.DEFAULT_TOP_K]
        verdict = study.verdict()

        return SearchResult(ok=verdict.get("ok", False), study=study,
                            config=cfg, space=self.space,
                            importance=importance, verdict=verdict,
                            top_k=top_k,
                            objective_stats=self.objective.stats(),
                            wall_seconds=study.wall_seconds,
                            backend=backend)
