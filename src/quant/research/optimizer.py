"""Hyperparameter optimisation wrapper: the study runs in Blue_engine, the evidence stays here.

Flow stage: Walk-forward + purged CV, Trial logger + DSR/PBO
Status: PHASE 5 IMPLEMENTED (wrap; one OptimizerEngine, no second HPO stack)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/optimizer/engine.py
(OptimizerEngine, EngineConfig), src/quant/blue_engine/optimizer/spaces.py (load_space),
src/quant/blue_engine/optimizer/analysis.py (visual_workflow), src/quant/blue_engine/optimizer/plots.py

AGENTS.md: "Reuse src/quant/blue_engine/optimizer/ (Optuna). Do not build a second HPO
stack." So this module never re-implements search. It adds the three things a
v5 run must have and Blue does not enforce on its own:

1. **A trial log row or the run does not count.** ``run_optimisation`` writes the
   study to Blue's trial-log path, which ``research/trial_log.py`` reads as an
   ``optimizer_run`` row: sampler, trial cap, bounds, folds, cost bps, seed,
   verdict. With no log path the run is refused rather than silently unlogged.
2. **No promotion.** A recommendation is advice. ``OptimisationResult`` says so
   in ``advice_only`` and nothing in this file touches a parameter file.
3. **The 3-step visual workflow** (importance -> slice -> parallel coordinates)
   from AGENTS.md, in that order, from one call.

4. **A leak audit before the study is recorded** (wired 2026-11-16). See
   ``leakage_verdict`` and ``_refuse_if_leaked`` below: the engine writes its
   trial and ``optimizer_run`` rows inside ``search()``, so the ONLY place a
   refusal can actually prevent a contaminated row from reaching the trial log
   is before the search starts. Evidence from leaked features is worse than no
   evidence - it looks good and is wrong.

A run that raises is converted to ``ok=False`` with the reason. A long study
must never lose its journal because one trial crashed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from quant.core.blue_bridge import BlueEngineUnavailable, load
from quant.research import leakage_gate

__all__ = ["OptimizerUnavailable", "OptimisationResult", "run_optimisation",
           "visual_workflow", "available_spaces", "loss_verdict",
           "leakage_verdict"]


class OptimizerUnavailable(RuntimeError):
    """Blue's optimiser cannot be loaded (for example optuna is absent)."""


def _modules():
    try:
        return load("optimizer.engine"), load("optimizer.spaces"), load("optimizer.analysis")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise OptimizerUnavailable(f"Blue optimiser unavailable: {exc}") from exc


def available_spaces() -> List[str]:
    """Names of the search spaces Blue ships or that were registered."""
    _, spaces, _ = _modules()
    return list(spaces.available_spaces())


def loss_verdict(sharpe: Optional[float], *, floor: float,
                 deflated: Optional[float] = None,
                 min_deflated: float = 0.95) -> Dict[str, Any]:
    """The low-level gate for a loss-based search objective.

    Kept local and simple on purpose: it answers two questions, in order.
    ``sharpe >= floor`` (is there edge at all at this cost and sample?), then,
    when a deflated Sharpe is supplied, ``deflated >= min_deflated`` (did the
    edge survive the number of trials?). The second check is skipped - and the
    answer says "skipped" - when it was not supplied. Skipped is not passed.
    """
    reasons: List[str] = []
    if sharpe is None or sharpe != sharpe:
        return {"ok": False, "reasons": ["sharpe missing or NaN"]}
    if sharpe < floor:
        reasons.append(f"sharpe {sharpe:.3f} below the {floor:.3f} floor")
    if deflated is not None:
        if deflated < min_deflated:
            reasons.append(
                f"deflated sharpe {deflated:.3f} below {min_deflated:.3f}: "
                "the edge does not survive the trial count")
        check = "applied"
    else:
        check = "skipped: no deflated sharpe supplied"
    return {"ok": not reasons, "reasons": reasons, "deflated_check": check}


@dataclass
class OptimisationResult:
    """One study's output, flattened into the shape the dashboard and research runner read."""

    ok: bool
    strategy: str
    space: str
    recommended_params: Dict[str, Any] = field(default_factory=dict)
    baseline_params: Dict[str, Any] = field(default_factory=dict)
    improvement: Optional[float] = None
    importance: Dict[str, Any] = field(default_factory=dict)
    top_k: List[str] = field(default_factory=list)
    verdict: Dict[str, Any] = field(default_factory=dict)
    backend: str = ""
    n_trials: int = 0
    wall_seconds: float = 0.0
    trial_log_path: Optional[str] = None
    error: Optional[str] = None
    #: Result of the pre-study leak audit (see ``leakage_verdict``). Recorded on
    #: every run, passed or refused, so the evidence row carries its provenance.
    leak_gate: Dict[str, Any] = field(default_factory=dict)
    raw: Any = None

    #: A recommendation is advice. A person installs parameters (AGENTS.md rule 5).
    advice_only: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "strategy": self.strategy, "space": self.space,
            "recommended_params": dict(self.recommended_params),
            "baseline_params": dict(self.baseline_params),
            "improvement_vs_default": self.improvement,
            "importance": self.importance, "top_k": list(self.top_k),
            "verdict": self.verdict, "backend": self.backend,
            "n_trials": self.n_trials, "wall_seconds": round(self.wall_seconds, 2),
            "trial_log_path": self.trial_log_path, "error": self.error,
            "leak_gate": dict(self.leak_gate),
            "advice_only": True,
        }


def leakage_verdict(bars: Optional[Sequence[Dict[str, Any]]]) -> Dict[str, Any]:
    """Audit the SERIES a study will be fed, before any trial is recorded.

    What this can and cannot certify, stated plainly:

    * It audits the **bar series**, not the strategy's features. It catches the
      two failures that make an optimisation meaningless and are invisible in
      the score: a missing knowledge timestamp (a lag is then undefined, so
      every feature may be reading the future) and duplicated/unsorted
      timestamps (rolling windows become ambiguous and can double-count a bar).
    * It does NOT run the behavioural shift test, because that needs a
      ``build_features`` callable which only the strategy author can supply. So
      the report comes back ``incomplete`` by construction and is reported as
      such - an unrun check is never a pass (``leakage_gate`` rule).
    * Synthetic data has no bar series to audit. That is returned as
      ``synthetic`` and is neither clean nor refused.

    Returns ``{"verdict": ..., "gate": "pass"|"block"|"n/a", "detail": ...}``.
    ``block`` means a FATAL timestamp finding: leaked evidence.
    """
    if not bars:
        return {"verdict": "synthetic", "gate": "n/a",
                "detail": "no bar series (synthetic fixture) - not auditable"}

    # The audit reads ``as_of_ts`` (point-in-time knowledge time). Bars carry
    # ``ts``, the bar's own stamp. Renaming here rather than teaching the audit
    # a second column name keeps ONE definition of knowledge time in the engine.
    rows: List[Dict[str, Any]] = []
    for bar in bars:
        row = dict(bar)
        if "as_of_ts" not in row:
            row["as_of_ts"] = row.get("ts")
        rows.append(row)

    report = leakage_gate.audit(rows)
    verdict = str(report.get("verdict"))

    # Why this does not simply call ``leakage_gate.require_clean``: that refuses
    # SUSPECT and INCOMPLETE as well, and here INCOMPLETE is unavoidable (the
    # shift test needs a strategy-supplied ``build_features``). Refusing every
    # real study would make the gate something operators switch off, which is
    # worse than no gate. So the split is on the finding's SEVERITY, which is the
    # engine's own judgement:
    #   FATAL   -> block. A missing/empty/unsorted stamp makes every lag
    #              undefined; the study is not evidence.
    #   DEGRADED -> pass with a named warning (typically duplicate timestamps).
    #              The engine rates this a screen rather than a proof, and the
    #              caller is told, so it is a decision the operator can see.
    return {
        "verdict": verdict,
        "gate": "block" if report.get("fatal") else "pass",
        "detail": report.get("detail"),
        "n_rows": len(rows),
        "fatal": report.get("fatal", 0),
        "suspect": report.get("suspect", 0),
        "checks_run": report.get("checks_run"),
        "checks_missing": report.get("checks_missing"),
    }


def run_optimisation(space: str, strategy: Any, *,
                     trial_log_path: Any,
                     engine_config: Optional[Dict[str, Any]] = None,
                     space_path: Optional[Any] = None,
                     bars: Optional[Sequence[Dict[str, Any]]] = None,
                     skip_leak_gate: bool = False) -> OptimisationResult:
    """Run one Blue study and return its flattened result. Never raises.

    ``trial_log_path`` is required: AGENTS.md says an optimisation that is not in
    the trial log is not evidence. Pass the study name as ``engine_config["name"]``.
    """
    if not trial_log_path:
        return OptimisationResult(
            ok=False, strategy=str(strategy), space=str(space), error=(
                "refused: no trial_log_path, and an unlogged optimisation is "
                "not evidence (AGENTS.md)"))

    # --- leak gate, BEFORE the search ------------------------------------- #
    # The engine appends its ``trial`` and ``optimizer_run`` rows inside
    # ``search()``, so refusing afterwards would leave the contaminated rows in
    # the trial log and the gate would be decorative. Refuse first.
    leak = ({"verdict": "disabled", "gate": "n/a",
             "detail": "leak gate skipped by caller"}
            if skip_leak_gate else leakage_verdict(bars))
    if leak.get("gate") == "block":
        return OptimisationResult(
            ok=False, strategy=str(strategy), space=str(space),
            trial_log_path=str(trial_log_path), leak_gate=leak,
            error=(f"refused by leakage gate (verdict={leak.get('verdict')}): "
                   f"{leak.get('detail')}. Nothing was written to the trial "
                   f"log; fix the timestamps at the source and rerun."))

    engine_mod, spaces_mod, _ = _modules()
    try:
        space_obj = (spaces_mod.load_space(space, str(space_path))
                     if space_path else spaces_mod.load_space(space))
        cfg_kwargs = dict(engine_config or {})
        cfg_kwargs.setdefault("name", str(space))
        cfg_kwargs["trial_log_path"] = str(trial_log_path)
        cfg = engine_mod.EngineConfig(**cfg_kwargs)
        result = engine_mod.OptimizerEngine(space_obj, strategy, cfg=cfg).search(
            bars=list(bars) if bars is not None else None)
    except Exception as exc:  # noqa: BLE001 - a study failure is a result, not a crash
        return OptimisationResult(ok=False, strategy=str(strategy), space=str(space),
                                  trial_log_path=str(trial_log_path), error=repr(exc),
                                  leak_gate=leak)

    study = getattr(result, "study", None)
    trials = list(getattr(study, "trials", []) or [])
    return OptimisationResult(
        ok=bool(getattr(result, "ok", False)),
        strategy=str(getattr(result, "strategy_name", strategy)),
        space=str(getattr(space_obj, "name", space)),
        recommended_params=dict(result.recommended_params() or {}),
        baseline_params=dict(result.baseline_params() or {}),
        improvement=result.improvement(),
        importance=dict(getattr(result, "importance", {}) or {}),
        top_k=list(getattr(result, "top_k", []) or []),
        verdict=dict(getattr(result, "verdict", {}) or {}),
        backend=str(getattr(result, "backend", "")),
        n_trials=len(trials),
        wall_seconds=float(getattr(result, "wall_seconds", 0.0) or 0.0),
        trial_log_path=str(trial_log_path),
        leak_gate=leak,
        raw=result,
    )


def visual_workflow(result: OptimisationResult,
                    top_k: Optional[int] = None) -> Dict[str, Any]:
    """Steps 1 -> 2 -> 3 in the AGENTS.md order, from one finished study.

    1. ``plot_param_importances``: which 3-4 parameters drive the result.
    2. ``plot_slice`` on those parameters: their individual sweet spots.
    3. ``plot_parallel_coordinate`` on the same parameters: how they combine.

    ``provisional`` is True when importance was computed on fewer trials than
    Blue's stability bar. A dashboard must show that as an under-budget warning
    rather than present a thin ranking as a finding.
    """
    _, _, analysis = _modules()
    study = getattr(result.raw, "study", None) if result.raw is not None else None
    if study is None:
        return {"ok": False, "provisional": True,
                "error": "no finished study: nothing to visualise"}
    try:
        report = analysis.visual_workflow(study, k=top_k)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "provisional": True, "error": repr(exc)}
    out = dict(report)
    out["advice_only"] = True
    return out
