"""
test_agent_memory_learning.py - memory must CHANGE the next prompt.

A memory that is written but never read is a log. These tests prove the loop
closes end to end, using the real self-correction judge and a scripted agent:

    cycle 1: bad answer  -> mistake memory records the exact failure
    cycle 2: the NEXT prompt for that station contains it

and that only JUDGEMENT failures are taught, never infrastructure outages.
"""
from __future__ import annotations

import pytest

from blue_engine.core.agent_memory import AgentMemory
from blue_engine.core.self_correction import SelfCorrectionLoop


RISK_OK = {
    "risk_veto_active": True,
    "max_allowed_slippage_pct": 0.01,
    "force_trailing_stop_pct": 1.5,
    "veto_reason": "spread widened beyond the tolerance band",
}


class ScriptedAgent:
    """Answers from a queue and keeps every prompt it was given."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.prompts = []

    def advise(self, engine, text, *, baseline=None, tools=None):
        self.prompts.append(text)
        return self.outcomes.pop(0) if self.outcomes else {
            "ok": False, "method": "disabled",
            "warning": "no LLM backend available"}


def _ok(advice, clamped=False):
    return {"ok": True, "method": "ollama", "advice": advice,
            "clamped": clamped, "calls": 1}


def _bad_answer():
    """A schema-valid-shaped failure: one key missing."""
    return {"ok": True, "method": "ollama",
            "advice": {"risk_veto_active": True}, "calls": 1}


# --------------------------------------------------------------------------- #
# The loop closes: failure -> mistake memory -> next prompt
# --------------------------------------------------------------------------- #
def test_a_real_failure_is_remembered_and_shown_next_time(tmp_path):
    memory = AgentMemory.load(tmp_path)
    stage = "risk"

    # ---- cycle 1: the model answers badly -------------------------------- #
    agent = ScriptedAgent([_bad_answer()])
    result = SelfCorrectionLoop(agent, max_attempts=1).run(stage, "context")
    assert result["ok"] is False
    assert result["failure_kind"] == "missing_keys"

    memory.mistakes.record(stage, kind=result["failure_kind"],
                           detail=result["failure_detail"])
    memory.mark("mistakes")
    assert memory.mistakes.for_stage(stage)[0]["kind"] == "missing_keys"

    # ---- cycle 2: the prompt carries the lesson -------------------------- #
    block = memory.prompt_block(stage)
    assert "MISTAKES TO NOT REPEAT" in block
    assert "veto_reason" in block

    agent2 = ScriptedAgent([_ok(RISK_OK)])
    loop = SelfCorrectionLoop(agent2, max_attempts=1)
    ok = loop.run(stage, "context", memory_block=block)
    assert ok["ok"] is True
    assert "veto_reason" in agent2.prompts[0], (
        "the memory block must reach the prompt, not just the log")


def test_memory_is_kept_through_a_correction_round(tmp_path):
    """A re-ask must not lose what the station already knows."""
    memory = AgentMemory.load(tmp_path)
    memory.working.begin_cycle("c1")
    memory.working.record_station("risk", produced={"v": True},
                                  evidence=["portfolio.data"])
    block = memory.prompt_block("risk")

    answered = dict(RISK_OK, data_requests=["portfolio.data"])
    agent = ScriptedAgent([_bad_answer(), _ok(answered)])
    result = SelfCorrectionLoop(agent, max_attempts=2).run(
        "risk", "context", memory_block=block)
    assert result["ok"] is True
    assert len(agent.prompts) == 2, "the loop should have re-asked once"
    assert all("portfolio.data" in p for p in agent.prompts), (
        "a prompt lost the memory block")


# --------------------------------------------------------------------------- #
# Calibration: a clamp is remembered, and advises the model
# --------------------------------------------------------------------------- #
def test_a_clamped_answer_teaches_the_station_what_it_can_keep(tmp_path):
    memory = AgentMemory.load(tmp_path)
    memory.calibration.observe("risk", proposed={"max_allowed_slippage_pct": 0.04},
                               operative={"max_allowed_slippage_pct": 0.01},
                               clamped=True, method="ollama")
    block = memory.prompt_block("risk")
    assert "tightened by the safety rails" in block
    assert "0.01" in block


def test_calibration_records_a_valid_answer_without_clamping_it(tmp_path):
    memory = AgentMemory.load(tmp_path)
    memory.calibration.observe("risk", proposed=RISK_OK, operative=RISK_OK,
                               clamped=False)
    profile = memory.calibration.profile("risk")
    assert profile["clamp_ratio"] == 0.0
    assert "max_allowed_slippage_pct" in profile["operative_ranges"]


# --------------------------------------------------------------------------- #
# What must NOT be learned
# --------------------------------------------------------------------------- #
def test_an_infrastructure_outage_is_not_taught_as_a_mistake(tmp_path):
    """The model did nothing wrong when the chain was down."""
    memory = AgentMemory.load(tmp_path)
    agent = ScriptedAgent([{"ok": False, "method": "disabled",
                            "warning": "no LLM backend available"}])
    result = SelfCorrectionLoop(agent, max_attempts=2).run("risk", "context")
    assert result["ok"] is False
    # The pipeline only records schema_rejected/other, so nothing lands here.
    assert memory.mistakes.rows == []


# --------------------------------------------------------------------------- #
# Memory never decides
# --------------------------------------------------------------------------- #
def test_memory_cannot_turn_a_bad_answer_into_a_good_one(tmp_path):
    """Even with a perfect memory block, the validator still rejects."""
    memory = AgentMemory.load(tmp_path)
    memory.mistakes.record("risk", kind="missing_keys", detail=["veto_reason"])
    block = memory.prompt_block("risk")

    agent = ScriptedAgent([_bad_answer()])
    result = SelfCorrectionLoop(agent, max_attempts=1).run(
        "risk", "context", memory_block=block)
    assert result["ok"] is False
    assert result["advice"] is None


def test_memory_never_overrides_the_safe_direction(tmp_path):
    """A memory that 'remembers' a wider limit must not widen the result."""
    memory = AgentMemory.load(tmp_path)
    memory.calibration.observe("risk", proposed={"p": 0.05},
                               operative={"p": 0.05})
    wide = dict(RISK_OK, max_allowed_slippage_pct=0.04)
    agent = ScriptedAgent([_ok(wide)])
    result = SelfCorrectionLoop(agent, max_attempts=1).run(
        "risk", "context",
        baseline={"max_allowed_slippage_pct": 0.01},
        memory_block=memory.prompt_block("risk"))
    assert result["ok"] is True
    assert result["advice"]["max_allowed_slippage_pct"] <= 0.01


# --------------------------------------------------------------------------- #
# Persistence across processes
# --------------------------------------------------------------------------- #
def test_memory_survives_a_restart(tmp_path):
    memory = AgentMemory.load(tmp_path)
    memory.mistakes.record("economic", kind="missing_keys",
                           detail=["rationale absent"])
    memory.mark("mistakes")
    memory.save()

    restarted = AgentMemory.load(tmp_path)
    assert restarted.mistakes.for_stage("economic")[0]["kind"] == "missing_keys"
    assert "rationale absent" in restarted.prompt_block("economic")


@pytest.mark.parametrize("stage", ["economic", "market", "risk"])
def test_each_station_gets_only_its_own_memories(tmp_path, stage):
    memory = AgentMemory.load(tmp_path)
    for other in ("economic", "market", "risk"):
        memory.mistakes.record(other, kind=f"{other}-error", detail=[other])
    block = memory.prompt_block(stage)
    assert f"{stage}-error" in block
    for other in {"economic", "market", "risk"} - {stage}:
        assert f"{other}-error" not in block
