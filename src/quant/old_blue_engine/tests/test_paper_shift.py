"""
test_paper_shift.py - the measurement harness must be safe and honest.

Invariants:

1. It can never run with ``dry_run=False``.
2. A rate with no denominator is ``None``, never 0 or 0.0.
3. A cycle that the gate stops is counted as NOT allowed.
4. Attempts, clamp rate and gate blocks are aggregated from real report fields.
5. It never places an order and never writes outside ``data/``.
"""
from __future__ import annotations

import json

import pytest

from blue_engine.core.paper_shift import (
    collect_metrics,
    load_base_config,
    research_config,
    run_paper_shift,
)


BASE = {
    "dry_run": False,
    "engines": {
        "agent": {"enabled": False, "timeout_seconds": 45,
                  "llm": {"ollama": {"enabled": False},
                          "openrouter": {"enabled": False}}},
        "stage_gate": {"enabled": False},
    },
}


# --------------------------------------------------------------------------- #
# Config shaping
# --------------------------------------------------------------------------- #
def test_research_config_forces_paper_mode():
    cfg = research_config(BASE)
    assert cfg["dry_run"] is True
    assert BASE["dry_run"] is False, "the caller's dict must not be mutated"


def test_research_config_enables_the_agent_layer():
    cfg = research_config(BASE, gate=True, tools=True, attempts=2)
    agent = cfg["engines"]["agent"]
    assert agent["enabled"] is True
    assert agent["self_correction"] == {"enabled": True, "max_attempts": 2,
                                        "tools": True}
    assert cfg["engines"]["stage_gate"] == {"enabled": True,
                                            "on_block": "halt_analysis"}


def test_attempts_are_clamped_to_the_hard_cap():
    assert research_config(BASE, attempts=99)["engines"]["agent"][
        "self_correction"]["max_attempts"] == 3
    assert research_config(BASE, attempts=0)["engines"]["agent"][
        "self_correction"]["max_attempts"] == 1


def test_the_agent_timeout_is_capped_for_measurement():
    assert research_config(BASE)["engines"]["agent"]["timeout_seconds"] <= 20


def test_the_gate_can_be_switched_off_to_measure_the_agent_alone():
    assert research_config(BASE, gate=False)["engines"]["stage_gate"]["enabled"] is False


# --------------------------------------------------------------------------- #
# Metrics - honesty
# --------------------------------------------------------------------------- #
def test_an_empty_run_reports_none_not_zero():
    metrics = collect_metrics([])
    assert metrics["mean_attempts"] is None
    assert metrics["clamp_rate"] is None
    assert metrics["mean_duration_seconds"] is None
    assert metrics["cycles"] == 0


def test_a_gate_stop_is_not_counted_as_an_allowed_cycle():
    report = {"gated": ["economic"], "ready_to_trade": False,
              "shift": {"stopped_at": "economic", "stations": []}}
    metrics = collect_metrics([report])
    assert metrics["cycles_allowed"] == 0
    assert metrics["gate_blocks"] == {"economic": 1}
    assert metrics["stopped_at"] == {"economic": 1}
    assert any("data gate stops the line" in n for n in metrics["notes"])


def test_attempts_and_clamps_are_aggregated_from_real_fields():
    report = {
        "gated": [],
        "ready_to_trade": False,
        "agent_advice": {
            "risk": {"ok": True, "warnings": ["agent advice clamped to the safe "
                                              "direction"]},
            "economic": {"ok": True, "warnings": []},
        },
        "self_correction": {"risk": {"corrections": 1, "attempts": [{}, {}]}},
        "skill_writes": {"risk": {"ok": True, "path": "x"}},
        "shift": {"stations": [{"stage": "risk", "state": "worked"},
                               {"stage": "market", "state": "idle"}]},
        "duration_seconds": 2.0,
    }
    metrics = collect_metrics([report])
    assert metrics["advised_stage_runs"] == 2
    assert metrics["corrections_total"] == 1
    assert metrics["mean_attempts"] == 1.5
    assert metrics["clamp_rate"] == 0.5
    assert metrics["worked_stages"] == {"risk": 1}
    assert metrics["idle_stages"] == {"market": 1}
    assert metrics["mean_duration_seconds"] == 2.0


def test_a_refused_json_write_is_counted():
    metrics = collect_metrics([{"skill_writes": {"risk": {"ok": False}}}])
    assert metrics["write_failures"] == 1


def test_no_agent_opinion_is_explained_not_hidden():
    metrics = collect_metrics([{"gated": [], "agent_advice": {}}])
    assert metrics["advised_stage_runs"] == 0
    assert any("LLM chain was unavailable" in n for n in metrics["notes"])


# --------------------------------------------------------------------------- #
# The harness end to end (the agent LLM chain is disabled in BASE)
# --------------------------------------------------------------------------- #
def test_a_paper_shift_runs_and_stays_paper():
    result = run_paper_shift(cycles=1, config=BASE,
                             agent_inputs={"economic": "steady inflation"})
    assert result["dry_run"] is True
    assert result["metrics"]["cycles"] == 1
    assert len(result["cycles"]) == 1
    assert result["cycles"][0]["ready_to_trade"] is not None


def test_the_harness_refuses_a_live_config():
    """Even an explicit ``dry_run=False`` is overridden, never honoured."""
    result = run_paper_shift(cycles=1, config=dict(BASE, dry_run=False))
    assert result["dry_run"] is True
    assert result["cycles"][0]["ready_to_trade"] is not None


def test_the_gate_metrics_match_the_pipeline_when_enabled():
    result = run_paper_shift(cycles=1, config=BASE, gate=True)
    metrics = result["metrics"]
    # BASE has no macro context, so the economic station is starved.
    assert metrics["gate_blocks"].get("economic") == 1
    assert metrics["cycles_allowed"] == 0


def test_the_gate_can_be_disabled_for_the_agent_measurement():
    result = run_paper_shift(cycles=1, config=BASE, gate=False)
    assert result["gate_enabled"] is False
    assert result["metrics"]["gate_blocks"] == {}


def test_the_measurement_is_json_serialisable():
    result = run_paper_shift(cycles=1, config=BASE)
    json.dumps(result)     # must not raise


def test_the_shipped_config_loads():
    cfg = load_base_config()
    assert "engines" in cfg
    assert cfg["dry_run"] is True


# --------------------------------------------------------------------------- #
# "Unavailable" is not "rejected" - the distinction that fixed a false negative
# --------------------------------------------------------------------------- #
def test_chain_unavailability_is_not_counted_as_a_rejection():
    report = {
        "gated": [], "ready_to_trade": False,
        "agent_advice": {"risk": {"ok": False,
                                  "method": "disabled",
                                  "warning": "no LLM backend available"}},
        "agent_telemetry": {"risk": {"ok": False,
                                     "warning_kind": "llm_unavailable"}},
    }
    metrics = collect_metrics([report])
    assert metrics["llm_unavailable_runs"] == 1
    assert metrics["schema_rejected_runs"] == 0
    assert any("EVERY advised stage failed for a chain reason" in n
               for n in metrics["notes"])


def test_a_schema_rejection_is_counted_as_a_rejection():
    report = {
        "agent_advice": {"risk": {"ok": False, "method": "invalid_response",
                                  "warning": "agent response failed the engine "
                                             "schema"}},
        "agent_telemetry": {"risk": {"ok": False,
                                     "warning_kind": "schema_rejected"}},
    }
    metrics = collect_metrics([report])
    assert metrics["schema_rejected_runs"] == 1
    assert metrics["llm_unavailable_runs"] == 0


def test_tool_calls_are_summed_from_telemetry():
    report = {"agent_telemetry": {"economic": {"tools_used": 2},
                                  "market": {"tools_used": 1}},
              "agent_advice": {"economic": {"ok": True},
                               "market": {"ok": True}}}
    assert collect_metrics([report])["tool_calls"] == 3


# --------------------------------------------------------------------------- #
# offline + infrastructure probe switches
# --------------------------------------------------------------------------- #
def test_offline_disables_every_backend():
    cfg = research_config(BASE, offline=True)
    llm = cfg["engines"]["agent"]["llm"]
    assert llm["ollama"]["enabled"] is False
    assert llm["openrouter"]["enabled"] is False


def test_the_infrastructure_probe_is_redirected_by_default():
    cfg = research_config(BASE)
    infra = cfg["engines"]["infrastructure"]
    assert infra["openalgo_port"] == 9
    assert infra["dash_port"] == 9
    assert infra["ws_port"] == 9


def test_the_probe_can_be_left_alone():
    cfg = research_config(BASE, probe_infrastructure=True)
    assert "openalgo_port" not in cfg["engines"].get("infrastructure", {})


def test_the_result_records_the_measurement_flags():
    result = run_paper_shift(cycles=1, config=BASE)
    assert result["offline"] is True
    assert result["probed_infrastructure"] is False


def test_cycles_carry_telemetry_for_reproducibility():
    result = run_paper_shift(cycles=1, config=BASE, gate=True)
    assert "agent_telemetry" in result["cycles"][0]

# --- Phase E: the governor meter is surfaced per cycle (LLM_STAGE_TTL_PLAN) ---
def test_llm_calls_totals_the_meter_across_cycles():
    from blue_engine.core.paper_shift import llm_calls
    cycles = [
        {"llm_tokens": {"meter": {"calls": 2, "cache_hits": 1,
                                  "prompt_tokens_saved": 300}}},
        {"llm_tokens": {"meter": {"calls": 1, "cache_hits": 1,
                                  "prompt_tokens_saved": 150}}},
        {"llm_tokens": {}},  # governor absent that cycle: contributes zero
    ]
    assert llm_calls(cycles) == {"calls": 3, "cache_hits": 2,
                                 "tokens_saved": 450}


def test_paper_shift_cycles_carry_the_llm_meter():
    result = run_paper_shift(cycles=1, config=BASE, offline=True)
    assert "llm_tokens" in result["cycles"][0]
