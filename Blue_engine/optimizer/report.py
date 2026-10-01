"""
optimizer/report.py - the human deliverable: JSON + Markdown, with a verdict.

THE ONE RULE THIS FILE ENFORCES
-------------------------------
**A study that did not establish an edge does not get a recommendation.** The
verdict (ESTABLISHED / SUGGESTIVE / NOT ESTABLISHED / NOT COMPUTABLE) comes
from ``study.verdict()`` (deflated Sharpe), and the Markdown says it in words a
PM can act on. Writing "recommended parameters" next to "NOT ESTABLISHED"
would be the exact false-comfort the project's dashboards refuse to produce.

WHAT THE REPORT CONTAINS, IN ORDER
----------------------------------
    1. The verdict banner (the first line a reader sees)
    2. Recommended parameters vs production defaults, with the improvement
    3. The deflated-Sharpe details (what makes the verdict what it is)
    4. Step 1/2/3 workflow summary (which params drive, where the sweet spots
       are, and the parallel-coordinate filter)
    5. The cost gate (does the edge survive Indian costs)
    6. The run's provenance (backend, seed, budget, wall time, data class)

DATA CLASS LABELLING (AGENTS.md hard requirement)
-------------------------------------------------
Every report states whether its numbers are backtested/simulated or live. This
engine is offline-only research, so its header always reads BACKTESTED/
SIMULATED - and if the strategy's own result declared ``data_class:
"simulated"`` (the synthetic fixture), the banner says so explicitly too.
Nothing in this package is ever presented as live market data.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from . import __version__

__all__ = ["build_report", "render_markdown"]


def build_report(result: Any, *, visual: Optional[Dict[str, Any]] = None
                 ) -> Dict[str, Any]:
    """Flatten a SearchResult into the JSON report payload."""
    study = result.study
    verdict = result.verdict or {}
    rec = result.recommended_params()
    base = result.baseline_params()

    return {
        "schema": "blue_engine.optimizer.report",
        "schema_version": 1,
        "generated_ts": time.time(),
        "engine_version": __version__,
        "data_class": "backtested/simulated (offline optimizer - NOT live)",
        "offline_only": True,
        "study": {
            "name": study.name,
            "space": study.space_name,
            "strategy": study.strategy,
            "objective_version": study.objective_version,
            "backend": result.backend,
            "seed": study.seed,
            "n_trials": len(study.trials),
            "n_ok": study.n_ok,
            "n_failed": study.n_failed,
            "wall_seconds": round(study.wall_seconds, 2),
        },
        "verdict": {
            "status": verdict.get("verdict"),
            "best_trial": verdict.get("best_trial"),
            "best_score": verdict.get("best_score"),
            "best_mean_sharpe": verdict.get("best_mean_sharpe"),
            "deflation": verdict.get("deflation"),
            "prob_real_bar": verdict.get("prob_real_bar"),
        },
        "parameters": {
            "recommended": rec,
            "baseline": base,
            "improvement_vs_default": result.improvement(),
            "note": ("recommendation only - applying it to the live config is "
                     "a separate, deliberate human action"),
        },
        "importance": result.importance,
        "top_k_params": result.top_k,
        "cost_gate_best": _best_cost_gate(study),
        "objective_stats": result.objective_stats,
        "visual_workflow": visual,
    }


def _best_cost_gate(study: Any) -> Optional[Dict[str, Any]]:
    """The cost gate of the best ok trial, for the 'does it survive' line."""
    best = study.best()
    if best is None:
        return None
    # AUDIT FINDING F2 (2026-10-01). The engine now writes the per-trial
    # cost-gate into BOTH ``study.meta["cost_gates"]`` (for the report
    # lookup) and ``trial.cost_gate`` (on the record itself, so it survives
    # a journal round-trip). Read the meta entry first - it is the
    # authoritative write side - and fall back to the trial field for old
    # journals that pre-date the F2 handoff. Either path returns None for
    # a study whose best trial never produced a gate (e.g. all-failed), and
    # the Markdown renders the generic message in that case.
    meta_gate = (study.meta or {}).get("cost_gates", {}).get(str(best.number))
    if meta_gate:
        return dict(meta_gate)
    if getattr(best, "cost_gate", None):
        return dict(best.cost_gate)
    return None


def render_markdown(payload: Dict[str, Any]) -> str:
    """The Markdown the operator reads. Verdict first, numbers after."""
    st = payload["study"]
    v = payload["verdict"]
    p = payload["parameters"]
    imp = payload.get("importance") or {}

    lines: List[str] = []
    status = v.get("status") or "UNKNOWN"
    banner = {"ESTABLISHED": "✅", "SUGGESTIVE": "🟡",
              "NOT ESTABLISHED": "🔴", "NOT COMPUTABLE": "⚪",
              "NO EDGE FOUND": "🔴"}.get(status, "⚪")
    lines.append(f"# Optimizer study: `{st['name']}`")
    lines.append("")
    lines.append(f"**{banner} VERDICT: {status}**")
    lines.append("")
    lines.append(f"*Data class: {payload['data_class']}.* "
                 "This engine is offline-only research tooling; it places no "
                 "orders and touches no live configuration.")
    lines.append("")

    # --- recommendation --------------------------------------------------- #
    if status in ("ESTABLISHED", "SUGGESTIVE") and p.get("recommended"):
        lines.append("## Recommended parameters")
        lines.append("")
        lines.append("| Parameter | Recommended | Current (default) |")
        lines.append("|---|---|---|")
        for k, val in p["recommended"].items():
            cur = p["baseline"].get(k, "-")
            marker = "  *← changed*" if val != cur else ""
            lines.append(f"| {k} | {val} | {cur}{marker} |")
        imp_v = p.get("improvement_vs_default")
        if imp_v is not None:
            lines.append("")
            lines.append(f"**Improvement vs current defaults: "
                         f"{imp_v:+.3f} mean-fold Sharpe** (deflated figures "
                         "below).")
    else:
        lines.append("## Recommended parameters")
        lines.append("")
        lines.append("_None. The verdict does not justify a change to the "
                     "current configuration._")
    lines.append("")

    # --- deflation -------------------------------------------------------- #
    d = v.get("deflation") or {}
    lines.append("## Deflated Sharpe (the honest number)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Observed best mean-fold Sharpe | {v.get('best_mean_sharpe', '-')} |")
    lines.append(f"| Trials searched | {st['n_ok']} scoreable / {st['n_trials']} run |")
    lines.append(f"| Expected max Sharpe for {d.get('n_trials', '-')} trials | "
                 f"{d.get('expected_max_sharpe', '-')} |")
    lines.append(f"| Deflated Sharpe | {d.get('deflated_sharpe', '-')} |")
    lines.append(f"| Probability real | {d.get('prob_real', '-')} "
                 f"(bar: {v.get('prob_real_bar', '-')}) |")
    lines.append("")

    # --- the three-step workflow ------------------------------------------ #
    vis = payload.get("visual_workflow")
    if vis:
        lines.append("## Visual workflow (importance -> slice -> parallel)")
        lines.append("")
        chosen = vis.get("important_params") or []
        lines.append(f"Parameters driving the ship: "
                     f"{', '.join(f'`{c}`' for c in chosen) or '-'}"
                     + (" _(under-budget - provisional)_" if vis.get("under_budget") else ""))
        lines.append("")
        sl = (vis.get("step2_slice") or {}).get("slices") or []
        if sl:
            lines.append("| Parameter | Sweet spot | Note |")
            lines.append("|---|---|---|")
            for s in sl:
                lines.append(f"| {s['name']} | {s.get('argmax', {}).get('mid', '-')} "
                             f"| {s.get('note', '')} |")
            lines.append("")
        if vis.get("fallback_note"):
            lines.append(f"*{vis['fallback_note']}*")
            lines.append("")

    # --- cost gate -------------------------------------------------------- #
    cg = payload.get("cost_gate_best")
    lines.append("## Cost gate (best trial)")
    lines.append("")
    if cg:
        lines.append(f"Gross {cg.get('gross_bps', '-')} bps/period, "
                     f"cost {cg.get('cost_bps', '-')} bps -> "
                     f"net {cg.get('net_bps', '-')} bps: "
                     f"**{cg.get('verdict', '-')}**")
    else:
        lines.append("_Per-trial cost gate recorded in the study journal; "
                     "the objective already charges costs inside every fold._")
    lines.append("")

    # --- provenance ------------------------------------------------------- #
    lines.append("## Provenance")
    lines.append("")
    lines.append(f"- space: `{st['space']}` · strategy: `{st['strategy']}` "
                 f"· objective v{st['objective_version']}")
    lines.append(f"- backend: `{st['backend']}` · seed: {st['seed']} · "
                 f"wall: {st['wall_seconds']}s")
    lines.append(f"- engine v{payload['engine_version']} · "
                 f"generated {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(payload['generated_ts']))}")
    lines.append("")
    lines.append("_Applying any parameter change to production remains a "
                 "deliberate human action outside this engine._")
    return "\n".join(lines) + "\n"
