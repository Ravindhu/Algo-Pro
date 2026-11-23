"""
agent_team.py - the AGENT TEAM: synchronized stage agents working one product.

WHY THIS FILE EXISTS
====================
The operator's model of the system:

    "all the agent work as a team, synchronize like factory employees.
     code logic is a machine. each employee should work for product."

So the pipeline is a factory floor:

* **The product** is one trading decision, built station by station.
* **Each station** (Economic, Market, Screener, Valuation, Portfolio, Alpha,
  Risk, Backtest, Optimization, Sector) is staffed by ONE small skill agent.
* **The machine** at each station is deterministic logic - it builds the part.
  The employee's job is to hand the machine the right raw material and to flag
  what is missing. The employee never operates the machine.
* **The shift supervisor** is the pipeline: it enforces station order, and it
  does not let a downstream station start on an unfinished part.

WHAT SYNCHRONISATION MEANS HERE (concretely)
--------------------------------------------
1. **One order.** Stations run in the fixed stage order; a station's inputs are
   the *parts actually produced* upstream, never a guess.
2. **One work-in-progress ledger.** Every station appends to a single shift
   record (``ShiftLedger``) - who worked, on what, with what evidence, what it
   concluded, where its JSON landed, and what it asked for.
3. **A handoff contract.** A station may only pass the part downstream when the
   machine reports ``ok`` AND the data gate is satisfied. Otherwise the line
   STOPS: no downstream employee is told to work on a defective part.
   (This is the "only data-satisfied logic code moves to the next stage" rule,
   expressed as a production line instead of a loop.)
4. **Blocked-station escalation.** A station that cannot get its material files
   a ``blocked`` ticket naming the exact missing paths, so the next shift starts
   with the material list instead of rediscovering it.
5. **No employee writes the machine's code.** Employees write JSON only
   (``SkillWriter``). A shift record is also JSON.
6. **Idle employees cost nothing.** Disabled/unavailable agents are recorded as
   ``idle``; the machine still runs with its deterministic default. The line
   only stops for *missing data*, never for a missing opinion.

WHAT THIS IS NOT
----------------
* Not an autonomous multi-agent planner. There is no free-form delegation, no
  agent-to-agent chat, and no self-directed task creation. The roster is fixed
  by the pipeline's stage order.
* Not in the execution path. Nothing here can place, size or cancel an order.
* Not a second pipeline. ``run_shift`` delegates every station to the existing
  ``MacroPipeline`` station result; it records and gates, it does not re-run.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "STATION_DEFINITIONS",
    "STATION_ORDER",
    "StationRecord",
    "ShiftLedger",
    "AgentTeam",
    "shift_from_report",
]

#: The product every station works on.
PRODUCT = "one risk-checked trading decision"

#: station -> (title, what the station PRODUCES for the next station).
#: Line order is ``EngineStage`` order (10 -> 60) so the roster always matches
#: the machine's real sequence; ``STATION_ORDER`` below is that order.
STATION_DEFINITIONS: Dict[str, Dict[str, str]] = {
    "economic": {"title": "Macro desk", "produces": "regime label + multiplier"},
    "market": {"title": "Market desk", "produces": "volatility state + lookback"},
    "screener": {"title": "Universe desk", "produces": "exclusion list"},
    "valuation": {"title": "Valuation desk", "produces": "assumption bounds"},
    "sector": {"title": "Sector desk", "produces": "sector stances"},
    "portfolio": {"title": "Allocation desk", "produces": "portfolio constraints"},
    "alpha": {"title": "Signal desk", "produces": "regime conflict flags"},
    "risk": {"title": "Risk desk", "produces": "veto decision"},
    "backtest": {"title": "Evidence desk", "produces": "honest window + costs"},
    "optimization": {"title": "Tuning desk", "produces": "search bounds"},
}

#: The order employees stand in on the line. Mirrors ``EngineStage`` values
#: (Economic 10 -> Market 20 -> Screener 25 -> Valuation 27 -> Portfolio 30 ->
#: Alpha 40 -> Risk 45 -> POV 50 -> Infrastructure 60). Backtest/Optimization
#: are research stations: they run off the daily line, before risk sizing.
STATION_ORDER: tuple = (
    "economic", "market", "screener", "valuation",
    "portfolio", "alpha", "risk", "backtest", "optimization",
)

#: States one station can be in at the end of its step.
STATION_STATES = (
    "worked",      # agent answered, machine accepted, gate satisfied
    "idle",        # no agent opinion (disabled/unavailable); machine used defaults
    "blocked",     # missing evidence - the line stops here
    "failed",      # agent answered but could not be corrected into a valid form
    "skipped",     # line already stopped upstream
)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class StationRecord:
    """One employee's step in one shift."""

    stage: str
    order: int
    state: str
    employee: str = ""
    title: str = ""
    machine_ok: Optional[bool] = None
    advice_ok: bool = False
    corrections: int = 0
    evidence_ok: Optional[bool] = None
    missing_evidence: List[str] = field(default_factory=list)
    data_requests: List[str] = field(default_factory=list)
    produced: str = ""
    json_path: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order": self.order, "stage": self.stage, "title": self.title,
            "employee": self.employee, "state": self.state,
            "machine_ok": self.machine_ok, "advice_ok": self.advice_ok,
            "corrections": self.corrections,
            "evidence_ok": self.evidence_ok,
            "missing_evidence": list(self.missing_evidence),
            "data_requests": list(self.data_requests),
            "produced": self.produced, "json_path": self.json_path,
            "notes": list(self.notes),
        }


@dataclass
class ShiftLedger:
    """The work-in-progress record for one run of the line.

    This is the artifact the operator (and the dashboard) reads: one row per
    station, in order, with the product's state after each step.
    """

    product: str = PRODUCT
    shift_id: str = ""
    started_at: str = field(default_factory=_now_iso)
    finished_at: Optional[str] = None
    stations: List[StationRecord] = field(default_factory=list)
    stopped_at: Optional[str] = None
    stop_reason: str = ""
    ready_to_trade: Optional[bool] = None

    # ---- queries -------------------------------------------------------- #
    def by_stage(self, stage: str) -> Optional[StationRecord]:
        for row in self.stations:
            if row.stage == stage:
                return row
        return None

    def states(self) -> Dict[str, str]:
        return {row.stage: row.state for row in self.stations}

    def worked(self) -> List[str]:
        return [r.stage for r in self.stations if r.state == "worked"]

    def blocked(self) -> List[str]:
        return [r.stage for r in self.stations if r.state == "blocked"]

    def open_material_requests(self) -> Dict[str, List[str]]:
        """What the NEXT shift must supply, keyed by stage."""
        out: Dict[str, List[str]] = {}
        for row in self.stations:
            if row.missing_evidence or row.data_requests:
                out[row.stage] = sorted(set(row.missing_evidence)
                                        | set(row.data_requests))
        return out

    # ---- serialisation -------------------------------------------------- #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "product": self.product,
            "shift_id": self.shift_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ready_to_trade": self.ready_to_trade,
            "stopped_at": self.stopped_at,
            "stop_reason": self.stop_reason,
            "stations": [row.to_dict() for row in self.stations],
            "summary": {
                "worked": self.worked(),
                "blocked": self.blocked(),
                "idle": [r.stage for r in self.stations if r.state == "idle"],
                "failed": [r.stage for r in self.stations if r.state == "failed"],
                "skipped": [r.stage for r in self.stations if r.state == "skipped"],
            },
            "material_requests": self.open_material_requests(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False,
                          allow_nan=False, default=str)


class AgentTeam:
    """The roster. Maps pipeline stages to employees (skills) and machines.

    The team holds no state between shifts: :meth:`run_shift` reads a finished
    ``MacroPipeline`` report and turns it into a :class:`ShiftLedger`. That keeps
    the line auditable and keeps this module out of the pipeline's control flow.
    """

    def __init__(self, skills: Optional[Mapping[str, Any]] = None,
                 *, station_order: Optional[Iterable[str]] = None) -> None:
        self.skills: Dict[str, Any] = dict(skills or {})
        self.station_order: List[str] = list(
            station_order
            or [s for s in STATION_ORDER if s in self.skills]
            or list(STATION_ORDER))

    # ---- roster ---------------------------------------------------------- #
    def roster(self) -> List[Dict[str, Any]]:
        """Who works which station, in line order."""
        rows = []
        for index, stage in enumerate(self.station_order, start=1):
            skill = self.skills.get(stage)
            spec = STATION_DEFINITIONS.get(stage, {})
            rows.append({
                "order": index,
                "stage": stage,
                "title": spec.get("title", stage.title()),
                "employee": getattr(skill, "skill_id", f"frontline.{stage}"),
                "produces": spec.get("produces", ""),
                "has_skill": skill is not None,
                "writes": getattr(skill, "writes", "json_only"),
            })
        return rows

    # ---- the shift ------------------------------------------------------- #
    def run_shift(self, report: Mapping[str, Any], *,
                  shift_id: Optional[str] = None) -> ShiftLedger:
        """Convert one pipeline report into a shift ledger. Never raises."""
        ledger = ShiftLedger(shift_id=shift_id or f"shift-{_now_iso()}")
        if not isinstance(report, Mapping):
            ledger.stop_reason = "no pipeline report"
            ledger.finished_at = _now_iso()
            return ledger

        stages = report.get("stages") or {}
        advice = report.get("agent_advice") or {}
        gate = report.get("stage_gate") or {}
        writes = report.get("skill_writes") or {}
        corrections = report.get("self_correction") or {}
        requests = report.get("data_requests") or {}
        gated = list(report.get("gated") or [])
        halted_at = gated[0] if gated else None

        seen_halt = False
        for index, stage in enumerate(self.station_order, start=1):
            spec = STATION_DEFINITIONS.get(stage, {})
            record = StationRecord(
                stage=stage, order=index,
                title=spec.get("title", stage.title()),
                employee=getattr(self.skills.get(stage), "skill_id",
                                 f"frontline.{stage}"),
                produced=spec.get("produces", ""),
                state="idle")

            stage_result = stages.get(stage)
            if isinstance(stage_result, Mapping):
                data = stage_result.get("data")
                if isinstance(data, Mapping) and data.get("gated"):
                    record.state = "skipped"
                    record.notes.append(str(data.get("reason") or "gated"))
                else:
                    record.machine_ok = stage_result.get("ok")

            gate_row = gate.get(stage)
            if isinstance(gate_row, Mapping):
                record.evidence_ok = bool(gate_row.get("satisfied"))
                record.missing_evidence = [str(m) for m in
                                           gate_row.get("missing") or []]

            verdict = advice.get(stage)
            if isinstance(verdict, Mapping):
                record.advice_ok = bool(verdict.get("ok"))
                if verdict.get("warning"):
                    record.notes.append(str(verdict["warning"]))
                trace = corrections.get(stage) or {}
                record.corrections = int(trace.get("corrections") or 0)

            write = writes.get(stage)
            if isinstance(write, Mapping) and write.get("path"):
                record.json_path = str(write["path"])

            if isinstance(requests, Mapping) and requests.get(stage):
                record.data_requests = [str(r) for r in requests[stage]]

            # ---- decide the station's state, in precedence order ---------- #
            # 1. The line was already stopped upstream: this employee was never
            #    asked to work, so its (stale) gate evidence is irrelevant.
            if record.state == "skipped":
                record.missing_evidence = []
                record.evidence_ok = None
                record.notes.append("line stopped upstream; station not staffed")
                ledger.stations.append(record)
                continue
            # 2. The station ran and its evidence is missing: the line STOPS.
            if gate_row is not None and record.evidence_ok is False:
                record.state = "blocked"
                ledger.stations.append(record)
                if not seen_halt:
                    seen_halt = True
                    ledger.stopped_at = stage
                    ledger.stop_reason = ("missing evidence: "
                                          + ", ".join(record.missing_evidence))
                continue
            if verdict is None:
                # No employee opinion. The machine still ran on its defaults.
                record.state = "idle"
                record.notes.append("no agent opinion; machine used defaults")
            elif record.advice_ok:
                record.state = "worked"
            else:
                record.state = "failed"
                record.notes.append("agent answer rejected after correction")
            ledger.stations.append(record)

        if halted_at and not ledger.stopped_at:
            ledger.stopped_at = halted_at
            ledger.stop_reason = f"data gate halted at {halted_at}"

        ledger.ready_to_trade = report.get("ready_to_trade")
        ledger.finished_at = _now_iso()
        return ledger

    def describe(self) -> Dict[str, Any]:
        return {"product": PRODUCT, "stations": self.roster(),
                "writable": "json_only", "executes_orders": False}


def shift_from_report(report: Mapping[str, Any],
                      skills: Optional[Mapping[str, Any]] = None,
                      *, shift_id: Optional[str] = None) -> Dict[str, Any]:
    """Convenience: report in, JSON-ready shift ledger out."""
    return AgentTeam(skills).run_shift(report, shift_id=shift_id).to_dict()
