"""
test_pipeline_stage_integration.py - the gate, the skills and the JSON writer
wired into ``MacroPipeline``.

The behaviours asserted here are the operator's stated contract:

  * "only data satisfied logic code move to next stage"  -> the chain halts.
  * "execution engine should be fully logic only"        -> risk/infrastructure
    still run and POV stays blocked while the analysis is halted.
  * "it should not edit code base only json entry"       -> the agent answer is
    published as JSON in the sandbox, and a code path is refused.
"""
from __future__ import annotations

import pytest

from blue_engine.core.pipeline import MacroPipeline, _data_requests


def _pipeline(tmp_path, *, gate: bool = True, stages=None, agent_io: bool = True):
    engines = {
        "stage_gate": {"enabled": gate},
        "agent": {"enabled": False},
    }
    if stages is not None:
        engines["stage_gate"]["stages"] = stages
    if agent_io:
        engines["agent_io"] = {"sandbox": str(tmp_path / "sandbox"),
                               "audit": True}
    return MacroPipeline({"dry_run": True, "engines": engines})


# --------------------------------------------------------------------------- #
# Gate: halt behavior
# --------------------------------------------------------------------------- #
def test_gate_disabled_by_default_runs_the_whole_chain(tmp_path):
    pipe = _pipeline(tmp_path, gate=False)
    report = pipe.run({})
    assert report["gated"] == []
    assert set(report) >= {"data_requests", "stage_gate", "skill_writes"}


def test_a_starved_stage_halts_the_chain_and_lists_what_is_missing(tmp_path):
    report = _pipeline(tmp_path).run({})
    assert report["gated"] == ["economic"]
    verdict = report["stage_gate"]["economic"]
    assert verdict["satisfied"] is False
    assert "macro" in verdict["missing"]
    assert report["ready_to_trade"] is False


def test_stages_after_the_halt_are_recorded_as_skipped_not_run(tmp_path):
    pipe = _pipeline(tmp_path)
    report = pipe.run({})
    market = report["stages"]["market"]
    assert market["ok"] is False
    assert market["data"]["gated"] is True
    assert market["data"]["halted_at"] == "economic"


def test_risk_and_infrastructure_still_run_while_the_chain_is_halted(tmp_path):
    """Partial execution must still be visible: the hard gates are evaluated."""
    report = _pipeline(tmp_path).run({})
    assert "risk" in report["stages"]
    assert "infrastructure" in report["stages"]
    # Risk must never be reported as gated - it is the stage that blocks POV.
    assert "risk" not in report["gated"]


def test_pov_is_blocked_while_the_analysis_is_halted(tmp_path):
    report = _pipeline(tmp_path).run({})
    pov = report["stages"].get("pov", {})
    assert pov.get("data", {}).get("slice", {}).get("status") == "blocked"


def test_satisfied_evidence_lets_the_chain_advance(tmp_path):
    stages = {"economic": [{"path": "macro", "check": "present"}],
              "market": [{"path": "market", "check": "present"}]}
    report = _pipeline(tmp_path, stages=stages).run(
        {"macro": {"policy_rate": 6.5}, "market": {"nifty": 24000}})
    assert report["gated"] == []
    assert report["stages"]["economic"]["ok"] is True
    assert report["stage_gate"]["economic"]["satisfied"] is True
    assert report["stage_gate"]["market"]["satisfied"] is True


#: A gate config where ONLY ``economic`` is governed, so the rest of the chain
#: behaves exactly as before (ungated). Keeping this explicit is what makes the
#: halt assertions below about the gate and nothing else.
_ONLY_ECONOMIC = {
    "economic": [{"path": "macro", "check": "present"}],
    "market": [], "portfolio": [], "alpha": [], "risk": [], "pov": [],
    "infrastructure": [], "screener": [], "valuation": [], "sector": [],
}


def test_a_gate_halted_at_a_later_stage_keeps_earlier_stages_ok(tmp_path):
    stages = dict(_ONLY_ECONOMIC)
    stages["market"] = [{"path": "market.rows", "check": "min_rows", "value": 2}]
    report = _pipeline(tmp_path, stages=stages).run(
        {"macro": {"policy_rate": 6.5}, "market": {"rows": [1]}})
    assert report["gated"] == ["market"]
    assert "market.rows" in report["stage_gate"]["market"]["missing"]
    # Economic SATISFIED the gate, so it was executed - the halt comes after it.
    # (Whether the engine itself succeeds depends on its own inputs, so the
    # assertion is that it is not marked as gate-skipped.)
    assert report["stages"]["economic"]["data"].get("gated") is not True
    # Portfolio comes after the halt, so it is skipped.
    assert report["stages"]["portfolio"]["data"]["gated"] is True


def test_a_satisfied_earlier_stage_still_advances_the_chain(tmp_path):
    report = _pipeline(tmp_path, stages=_ONLY_ECONOMIC).run(
        {"macro": {"policy_rate": 6.5}})
    assert report["gated"] == []
    assert report["stage_gate"]["market"]["satisfied"] is True


def test_on_block_warn_records_the_verdict_without_halting(tmp_path):
    pipe = _pipeline(tmp_path, stages=_ONLY_ECONOMIC)
    pipe.stage_gate.on_block = "warn"
    report = pipe.run({})          # no macro -> economic verdict is failed
    # The verdict is recorded and the starved stage is listed ...
    assert report["stage_gate"]["economic"]["satisfied"] is False
    assert report["gated"] == ["economic"]
    # ... but nothing was SKIPPED: the chain ran to the end.
    assert report["stages"]["portfolio"]["data"].get("gated") is None
    assert any("warn mode" in warning for warning in report["warnings"])


# --------------------------------------------------------------------------- #
# Wiring: skills + writer are constructed and honest
# --------------------------------------------------------------------------- #
def test_the_pipeline_carries_one_skill_per_stage(tmp_path):
    pipe = _pipeline(tmp_path)
    assert "risk" in pipe.skills and "economic" in pipe.skills
    assert pipe.skills["risk"].skill_id == "frontline.risk"


def test_the_writer_sandbox_follows_the_config(tmp_path):
    pipe = _pipeline(tmp_path)
    assert pipe.skill_writer.sandbox == (tmp_path / "sandbox").resolve()


def test_the_agent_write_path_refuses_code_inside_a_live_pipeline(tmp_path):
    pipe = _pipeline(tmp_path)
    refused = pipe.skill_writer.write_json("core/risk_engine.py", {"x": 1})
    assert refused["ok"] is False


# --------------------------------------------------------------------------- #
# Data requests
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("advice,expected", [
    ({"data_requests": {"needs": ["cpi"], "reason": "regime"}}, ["cpi"]),
    ({"data_requests": ["cpi", " repo"]}, ["cpi", "repo"]),
    ({"data_requests": "cpi"}, ["cpi"]),
    ({"data_requests": {"needs": []}}, []),
    ({"data_requests": None}, []),
    ({"data_requests": [1, None, ""]}, []),      # non-strings are dropped
    ({}, []),
    (None, []),
])
def test_data_requests_are_extracted_defensively(advice, expected):
    assert _data_requests(advice) == expected


def test_data_requests_are_capped_to_twenty():
    assert len(_data_requests(
        {"data_requests": [f"f{i}" for i in range(50)]})) == 20


# --------------------------------------------------------------------------- #
# Report shape (dashboard contract)
# --------------------------------------------------------------------------- #
def test_report_gains_the_new_keys_without_losing_the_old_ones(tmp_path):
    report = _pipeline(tmp_path, gate=False).run({})
    assert set(report) >= {
        "dry_run", "stages", "order", "warnings", "agent_advice",
        "data_requests", "stage_gate", "gated", "skill_writes", "ready_to_trade",
    }
    assert isinstance(report["skill_writes"], dict)


def test_a_disabled_agent_writes_no_skill_files(tmp_path):
    report = _pipeline(tmp_path, gate=False).run({})
    assert report["skill_writes"] == {}
    assert report["data_requests"] == {}


def test_a_configured_default_sandbox_lives_under_data():
    from blue_engine.runtime_paths import runtime_data_root

    pipe = MacroPipeline({"dry_run": True, "engines": {}})
    assert pipe.skill_writer.sandbox.name == "agent_stage"
    # The default follows the runtime data root (repo/data unless the test
    # suite redirects it), so tests never write into the working tree.
    assert pipe.skill_writer.sandbox == runtime_data_root() / "agent_stage"
