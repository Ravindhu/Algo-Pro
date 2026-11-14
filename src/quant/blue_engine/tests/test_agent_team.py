"""
test_agent_team.py - the factory line: synchronized stations, one product.

The contract under test:

  * every stage is a STATION; the roster follows the machine's stage order;
  * an employee only "works" when its answer reached the machine;
  * the line STOPS at the first station whose evidence is missing, and the
    stations behind it are not asked to work on a defective part;
  * a missing opinion is `idle`, not a failure - the machine runs on defaults;
  * the ledger is JSON-clean and names the material the next shift needs.
"""
from __future__ import annotations

import json

import pytest

from blue_engine.core.agent_team import (
    PRODUCT,
    STATION_ORDER,
    AgentTeam,
    ShiftLedger,
    StationRecord,
    shift_from_report,
)
from blue_engine.core.pipeline import MacroPipeline
from blue_engine.core.stage_skills import DEFAULT_SKILLS


def _report(**overrides):
    base = {
        "stages": {},
        "agent_advice": {},
        "stage_gate": {},
        "gated": [],
        "skill_writes": {},
        "self_correction": {},
        "data_requests": {},
        "ready_to_trade": None,
    }
    base.update(overrides)
    return base


def _stage(ok=True, gated=False, reason=""):
    return {"engine": "x", "ok": ok,
            "data": {"gated": True, "reason": reason} if gated else {},
            "warnings": [], "error": None}


def _gate(satisfied, missing=()):
    return {"satisfied": satisfied, "missing": list(missing)}


# --------------------------------------------------------------------------- #
# Roster
# --------------------------------------------------------------------------- #
def test_the_roster_follows_the_machine_stage_order():
    team = AgentTeam(DEFAULT_SKILLS)
    stages = [row["stage"] for row in team.roster()]
    assert stages == [s for s in STATION_ORDER if s in DEFAULT_SKILLS]


def test_every_station_has_an_employee_and_a_product_to_hand_over():
    for row in AgentTeam(DEFAULT_SKILLS).roster():
        assert row["employee"].startswith("frontline.")
        assert row["produces"]
        assert row["writes"] == "json_only"


def test_the_team_describes_itself_without_claiming_execution():
    doc = AgentTeam(DEFAULT_SKILLS).describe()
    assert doc["product"] == PRODUCT
    assert doc["executes_orders"] is False
    assert doc["writable"] == "json_only"


# --------------------------------------------------------------------------- #
# Station states
# --------------------------------------------------------------------------- #
def test_a_complete_station_is_recorded_as_worked():
    ledger = AgentTeam(DEFAULT_SKILLS).run_shift(_report(
        stages={"economic": _stage()},
        agent_advice={"economic": {"ok": True, "method": "ollama"}},
        stage_gate={"economic": _gate(True)},
        skill_writes={"economic": {"ok": True, "path": "…/economic.json"}},
        self_correction={"economic": {"corrections": 1, "attempts": []}},
    ))
    row = ledger.by_stage("economic")
    assert row.state == "worked"
    assert row.corrections == 1
    assert row.json_path.endswith("economic.json")
    assert row.evidence_ok is True


def test_a_stage_with_no_agent_opinion_is_idle_not_failed():
    ledger = AgentTeam(DEFAULT_SKILLS).run_shift(_report(
        stages={"screener": _stage()},
        stage_gate={"screener": _gate(True)}))
    row = ledger.by_stage("screener")
    assert row.state == "idle"
    assert any("machine used defaults" in n for n in row.notes)


def test_a_rejected_agent_answer_is_recorded_as_failed():
    ledger = AgentTeam(DEFAULT_SKILLS).run_shift(_report(
        stages={"risk": _stage()},
        agent_advice={"risk": {"ok": False, "warning": "schema"}},
        stage_gate={"risk": _gate(True)}))
    assert ledger.by_stage("risk").state == "failed"


# --------------------------------------------------------------------------- #
# The line stops - and only at the right station
# --------------------------------------------------------------------------- #
def test_missing_evidence_stops_the_line_and_is_named():
    ledger = AgentTeam(DEFAULT_SKILLS).run_shift(_report(
        stages={"economic": _stage()},
        agent_advice={"economic": {"ok": True}},
        stage_gate={"economic": _gate(False, ["macro", "macro.cpi"])}))
    assert ledger.stopped_at == "economic"
    assert "macro" in ledger.stop_reason
    assert ledger.by_stage("economic").state == "blocked"


def test_stations_behind_the_stop_are_skipped_not_blocked():
    """The stale gate evidence of an unstaffed station must not be blamed."""
    ledger = AgentTeam(DEFAULT_SKILLS).run_shift(_report(
        stages={"economic": _stage(), "market": _stage(gated=True,
                                                       reason="insufficient")},
        stage_gate={"economic": _gate(False, ["macro"]),
                    "market": _gate(False, ["market"])}))
    assert ledger.stopped_at == "economic"
    market = ledger.by_stage("market")
    assert market.state == "skipped"
    assert market.missing_evidence == []
    assert market.evidence_ok is None


def test_a_station_before_the_stop_still_records_its_work():
    ledger = AgentTeam(DEFAULT_SKILLS).run_shift(_report(
        stages={"economic": _stage(), "market": _stage()},
        agent_advice={"economic": {"ok": True}, "market": {"ok": True}},
        stage_gate={"economic": _gate(True), "market": _gate(False, ["market"])}))
    assert ledger.by_stage("economic").state == "worked"
    assert ledger.by_stage("market").state == "blocked"
    assert ledger.stopped_at == "market"


def test_the_report_gate_list_is_used_when_stage_evidence_is_absent():
    ledger = AgentTeam(DEFAULT_SKILLS).run_shift(_report(gated=["valuation"]))
    assert ledger.stopped_at == "valuation"


# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #
def test_the_ledger_is_json_clean():
    ledger = AgentTeam(DEFAULT_SKILLS).run_shift(_report())
    text = ledger.to_json()
    payload = json.loads(text)
    assert payload["product"] == PRODUCT
    assert len(payload["stations"]) == len(STATION_ORDER)


def test_the_ledger_lists_material_for_the_next_shift():
    ledger = AgentTeam(DEFAULT_SKILLS).run_shift(_report(
        stages={"economic": _stage()},
        stage_gate={"economic": _gate(False, ["macro.cpi"])},
        data_requests={"economic": ["cpi_history_12m"]}))
    needs = ledger.open_material_requests()
    assert needs["economic"] == ["cpi_history_12m", "macro.cpi"]


def test_a_bad_report_never_raises():
    ledger = AgentTeam(DEFAULT_SKILLS).run_shift(None)
    assert isinstance(ledger, ShiftLedger)
    assert ledger.stop_reason == "no pipeline report"


def test_shift_from_report_returns_a_plain_dict():
    out = shift_from_report(_report(), DEFAULT_SKILLS, shift_id="shift-42")
    assert out["shift_id"] == "shift-42"
    assert isinstance(out["stations"], list)


def test_station_record_round_trips():
    row = StationRecord(stage="risk", order=7, state="worked",
                        employee="frontline.risk", missing_evidence=["a"])
    payload = json.loads(json.dumps(row.to_dict()))
    assert payload["stage"] == "risk"
    assert payload["missing_evidence"] == ["a"]


# --------------------------------------------------------------------------- #
# End to end through the real pipeline
# --------------------------------------------------------------------------- #
def test_the_pipeline_publishes_a_shift(tmp_path):
    pipe = MacroPipeline({"dry_run": True, "engines": {
        "stage_gate": {"enabled": True},
        "agent_io": {"sandbox": str(tmp_path / "s")}}})
    report = pipe.run({})
    shift = report["shift"]
    assert shift["product"] == PRODUCT
    assert shift["stopped_at"] == "economic"
    assert shift["ready_to_trade"] is False
    assert [row["stage"] for row in shift["stations"]] == [
        s for s in STATION_ORDER if s in pipe.skills]


def test_a_clean_pipeline_run_reports_no_stop(tmp_path):
    pipe = MacroPipeline({"dry_run": True, "engines": {
        "stage_gate": {"enabled": False},
        "agent_io": {"sandbox": str(tmp_path / "s")}}})
    shift = pipe.run({})["shift"]
    assert shift["stopped_at"] is None
    assert shift["stop_reason"] == ""


def test_the_team_never_imports_an_execution_module():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[1]
            / "core" / "agent_team.py").read_text(encoding="utf-8")
    for forbidden in ("place_order(", "cancel_order(", "from ..pov",
                      "import risk_gate"):
        assert forbidden not in text
