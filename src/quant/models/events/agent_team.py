"""Agent team wrapper: one shift ledger per run, DELEGATED to Blue_engine.

Flow stage: Event scoring, Filings and circulars
Status: PHASE 5 IMPLEMENTED (wrap; advice and evidence only, never an order)
Existing code to reuse or wrap (do not copy): Blue_engine/core/agent_team.py
(AgentTeam, ShiftLedger, STATION_ORDER), Blue_engine/core/stage_gate.py (StageGate)

WHY THE STATION LEDGER LIVES OUTSIDE THE TRADE PATH
---------------------------------------------------
``run_shift`` answers one question for the dashboard and the operator: *which
stage worked, which sat idle, and where did the line stop for missing data?*
Blue's ``run_shift`` reads a finished pipeline report and derives that ledger. It
executes nothing, and neither does this wrapper.

The wrapper narrows the report to the keys the ledger reads (stages, gate,
advice, writes, corrections, requests) and ignores every other key, so an
execution-engine payload that happens to be in the report cannot leak into a
station row - and in particular cannot set ``ready_to_trade``. A shift ledger is
evidence about data, never a permission to trade.

STAGE GATE
----------
``check_stage`` exposes Blue's read-only data-sufficiency gate so a v5 stage can
ask "is there enough evidence to run me?" without importing Blue directly.
A disabled gate answers "satisfied" with that reason stated, never silently.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["AgentTeamUnavailable", "STATION_ORDER", "describe_team",
           "run_shift", "check_stage"]

#: Ledger key subset that a station row may read. Deliberately explicit.
_REPORT_KEYS = ("stages", "stage_gate", "agent_advice", "skill_writes",
                "self_correction", "data_requests", "gated")


class AgentTeamUnavailable(RuntimeError):
    """Blue's agent team cannot be loaded."""


def _team_module():
    try:
        return load("core.agent_team")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise AgentTeamUnavailable(f"Blue agent team unavailable: {exc}") from exc


def _gate_cls():
    try:
        return load("core.stage_gate").StageGate
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise AgentTeamUnavailable(f"Blue stage gate unavailable: {exc}") from exc


def _safe_report(report: Any) -> Dict[str, Any]:
    """Keep only ledger keys. Anything else is dropped, never copied through."""
    if not isinstance(report, dict):
        return {}
    out = {k: report[k] for k in _REPORT_KEYS if k in report}
    # A trade permission is not evidence about data. The ledger records it, so it
    # is forced False here: only the risk gate may grant one (AGENTS.md rule 2).
    out.pop("ready_to_trade", None)
    return out


def run_shift(report: Optional[Dict[str, Any]], *,
              shift_id: Optional[str] = None) -> Dict[str, Any]:
    """Turn a pipeline report into a JSON-ready shift ledger. Never raises.

    A missing report produces an empty ledger whose ``stop_reason`` says so -
    the same answer Blue gives - rather than an exception the dashboard must catch.
    """
    module = _team_module()
    if not isinstance(report, dict) or not report:
        # ``_safe_report`` turns a non-dict into {}, which Blue would happily walk
        # as an empty shift. Say what happened instead: an empty ledger that reads
        # as "nothing to report" hides a missing pipeline run.
        return {"product": "one risk-checked trading decision", "shift_id": shift_id,
                "stations": [], "stop_reason": "no pipeline report supplied",
                "summary": {"worked": [], "blocked": [], "idle": [], "failed": [],
                            "skipped": []}, "material_requests": {},
                "ready_to_trade": None, "advice_only": True}
    try:
        ledger = module.AgentTeam().run_shift(_safe_report(report), shift_id=shift_id)
        out = ledger.to_dict()
    except Exception as exc:  # noqa: BLE001 - a ledger must always be produced
        return {"product": "one risk-checked trading decision", "shift_id": shift_id,
                "stations": [], "stop_reason": f"ledger could not be built: {exc!r}",
                "summary": {"worked": [], "blocked": [], "idle": [], "failed": [],
                            "skipped": []}, "material_requests": {},
                "ready_to_trade": None}
    out["ready_to_trade"] = None          # evidence about data; never a trade permit
    out["advice_only"] = True
    return out


def describe_team() -> Dict[str, Any]:
    """Roster: station order, titles and what each station produces."""
    module = _team_module()
    team = module.AgentTeam()
    return {"product": getattr(module, "PRODUCT", "one trading decision"),
            "station_order": list(getattr(module, "STATION_ORDER", ())),
            "stations": team.roster(),
            "executes_orders": False}


def check_stage(stage: str, context: Optional[Dict[str, Any]],
                config: Optional[Dict[str, Any]] = None,
                now: Any = None) -> Dict[str, Any]:
    """Data-sufficiency verdict for one stage. Never raises.

    ``config`` is the ``stage_gate`` block (``{"enabled": true, "stages": ...}``).
    With no config the gate is disabled, and the verdict says exactly that so a
    reader never mistakes "not checked" for "passed the check".
    """
    try:
        gate = _gate_cls()(dict(config or {}))
        verdict = gate.check(stage, dict(context or {}), now=now)
    except Exception as exc:  # noqa: BLE001 - a gate must answer, not raise
        return {"stage": stage, "satisfied": False, "missing": [],
                "enabled": bool((config or {}).get("enabled", False)),
                "reason": f"stage gate could not be evaluated: {exc!r}"}
    results = getattr(verdict, "results", []) or []
    return {
        "stage": getattr(verdict, "stage", stage),
        "satisfied": bool(getattr(verdict, "satisfied", False)),
        "enabled": bool(getattr(gate, "enabled", False)),
        "missing": [str(m) for m in getattr(verdict, "missing", []) or []],
        "reason": str(getattr(verdict, "reason", "")),
        "requirements": [r.describe() for r in results] if results else [],
        "on_block": getattr(gate, "on_block", None),
    }


def station_sequence(stages: Sequence[str]) -> List[str]:
    """Order ``stages`` the way the line runs them; unknown names go last, in input order."""
    module = _team_module()
    order = list(getattr(module, "STATION_ORDER", ()))
    rank = {name: i for i, name in enumerate(order)}
    return sorted((str(s) for s in stages),
                  key=lambda s: (rank.get(s, len(order)), str(s)))
