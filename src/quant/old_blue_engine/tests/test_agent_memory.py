"""
test_agent_memory.py - the three memories.

The invariants that make memory safe to add to a trading system:

1. JSON only, under the writer sandbox; a corrupt file degrades to empty.
2. Bounded: working keeps N cycles, calibration a rolling window, mistakes a
   capped, de-duplicated list.
3. Credential-shaped text is never stored.
4. Memory is CONTEXT: it never decides an answer, and a memory failure never
   breaks a run.
5. The pipeline actually learns: a real failure lands in the mistake memory, an
   accepted answer lands in calibration, and the next prompt carries both.
"""
from __future__ import annotations

import json

import pytest

from blue_engine.core.agent_memory import (
    MAX_CYCLES,
    MAX_MISTAKES,
    MAX_OBSERVATIONS,
    AgentMemory,
    CalibrationMemory,
    MistakeMemory,
    WorkingMemory,
    memory_context_block,
)


@pytest.fixture()
def mem(tmp_path):
    return AgentMemory.load(tmp_path / "sandbox")


# --------------------------------------------------------------------------- #
# 1. Active working memory
# --------------------------------------------------------------------------- #
def test_working_memory_bounds_the_cycle_list():
    wm = WorkingMemory.empty()
    for i in range(MAX_CYCLES + 4):
        wm.begin_cycle(f"c{i}")
    assert len(wm.cycles) == MAX_CYCLES
    assert wm.cycles[-1]["cycle"] == f"c{MAX_CYCLES + 3}"


def test_working_memory_records_what_a_station_produced():
    wm = WorkingMemory.empty()
    wm.begin_cycle("c1")
    wm.record_station("economic", produced={"macro_regime": "Normal"},
                      evidence=["macro.cpi"], state="worked")
    assert wm.cycles[-1]["stations"]["economic"]["state"] == "worked"
    assert wm.evidence_already_have() == ["macro.cpi"]


def test_working_memory_detects_a_changed_input():
    wm = WorkingMemory.empty()
    wm.begin_cycle("c1")
    wm.record_station("market", produced={"state": "Low"})
    wm.begin_cycle("c2")
    wm.record_station("market", produced={"state": "High_Panic"})
    assert wm.changed_inputs("market") == ["market"]
    assert wm.changed_inputs("risk") == []


def test_working_memory_reports_no_change_when_identical():
    wm = WorkingMemory.empty()
    wm.begin_cycle("c1")
    wm.record_station("market", produced={"state": "Low"})
    wm.begin_cycle("c2")
    wm.record_station("market", produced={"state": "Low"})
    assert wm.changed_inputs("market") == []


# --------------------------------------------------------------------------- #
# 2. Historical calibration memory
# --------------------------------------------------------------------------- #
def test_calibration_keeps_a_rolling_window():
    cm = CalibrationMemory()
    for i in range(MAX_OBSERVATIONS + 10):
        cm.observe("risk", proposed={"p": i}, operative={"p": i})
    assert len(cm.stages["risk"]["observations"]) == MAX_OBSERVATIONS


def test_calibration_profiles_clamp_ratio_and_ranges():
    cm = CalibrationMemory()
    cm.observe("risk", proposed={"slippage": 0.04},
               operative={"slippage": 0.01}, clamped=True)
    cm.observe("risk", proposed={"slippage": 0.02},
               operative={"slippage": 0.02}, clamped=False)
    profile = cm.profile("risk")
    assert profile["observations"] == 2
    assert profile["clamp_ratio"] == 0.5
    assert profile["operative_ranges"]["slippage"] == {
        "min": 0.01, "max": 0.02, "last": 0.02}


def test_calibration_of_an_unknown_stage_is_empty_not_zero():
    assert CalibrationMemory().profile("nope") == {}


# --------------------------------------------------------------------------- #
# 3. Mistake memory
# --------------------------------------------------------------------------- #
def test_repeating_a_signature_bumps_the_count_instead_of_duplicating():
    mm = MistakeMemory()
    mm.record("risk", kind="missing_keys", detail=["veto_reason absent"])
    mm.record("risk", kind="missing_keys", detail=["veto_reason absent"])
    assert len(mm.rows) == 1
    assert mm.rows[0]["count"] == 2


def test_distinct_signatures_are_capped_per_stage():
    mm = MistakeMemory()
    for kind in ("a", "b", "c", "d", "e"):
        mm.record("risk", kind=kind, detail=[kind])
    assert len([r for r in mm.rows if r["stage"] == "risk"]) == mm.max_per_stage


def test_the_total_store_is_capped():
    mm = MistakeMemory()
    for i in range(MAX_MISTAKES + 20):
        mm.record(f"stage{i}", kind="x", detail=["d"])
    assert len(mm.rows) <= MAX_MISTAKES


def test_a_resolved_mistake_is_evicted_first():
    mm = MistakeMemory()
    mm.record("risk", kind="a", detail=["first"], resolved=True)
    mm.record("risk", kind="b", detail=["second"])
    mm.record("risk", kind="c", detail=["third"])
    mm.record("risk", kind="d", detail=["fourth"])   # forces an eviction
    kinds = {r["kind"] for r in mm.rows if r["stage"] == "risk"}
    assert "a" not in kinds


def test_for_stage_returns_the_most_repeated_unresolved_first():
    mm = MistakeMemory()
    mm.record("risk", kind="rare", detail=["x"])
    mm.record("risk", kind="common", detail=["y"])
    mm.record("risk", kind="common", detail=["y"])
    assert mm.for_stage("risk")[0]["kind"] == "common"


# --------------------------------------------------------------------------- #
# 4. Safety: secrets, bounds, honesty
# --------------------------------------------------------------------------- #
def test_a_credential_shaped_string_is_redacted_not_stored(mem):
    mem.mistakes.record("risk", kind="x",
                        detail=["leaked sk-or-v1-" + "a" * 30])
    assert "sk-or-v1-" not in mem.mistakes.rows[0]["detail"]
    assert "redacted" in mem.mistakes.rows[0]["detail"]


def test_long_free_text_is_truncated():
    mm = MistakeMemory()
    mm.record("risk", kind="x", detail=["z" * 2000])
    assert len(mm.rows[0]["detail"]) <= 400


@pytest.mark.parametrize("payload", ["{not json", "[]", "\x00\x01"])
def test_a_corrupt_memory_file_degrades_to_empty(tmp_path, payload):
    root = tmp_path / "sandbox" / "_memory"
    root.mkdir(parents=True)
    (root / "working.json").write_text(payload, encoding="utf-8")
    memory = AgentMemory.load(tmp_path / "sandbox")
    assert memory.working.cycles == []
    assert memory.load_notes and "working" in memory.load_notes[0]


def test_missing_memory_files_are_silent(tmp_path):
    memory = AgentMemory.load(tmp_path / "sandbox")
    assert memory.load_notes == []
    assert memory.working.cycles == []


# --------------------------------------------------------------------------- #
# 5. Persistence through the ONE write path
# --------------------------------------------------------------------------- #
def test_save_then_load_round_trips(mem):
    mem.working.begin_cycle("c1")
    mem.working.record_station("economic", produced={"r": "Normal"},
                               evidence=["macro"], state="worked")
    mem.calibration.observe("risk", proposed={"p": 1}, operative={"p": 1})
    mem.mistakes.record("risk", kind="missing_keys", detail=["a absent"])
    mem.mark("working", "calibration", "mistakes")

    saved = mem.save()
    assert set(saved) == {"working", "calibration", "mistakes"}
    assert all(row["ok"] for row in saved.values())

    again = AgentMemory.load(mem.sandbox)
    assert again.working.cycles[-1]["stations"]["economic"]["state"] == "worked"
    assert again.calibration.profile("risk")["observations"] == 1
    assert again.mistakes.for_stage("risk")[0]["kind"] == "missing_keys"


def test_memory_files_are_plain_json_an_operator_can_read(mem):
    mem.working.begin_cycle("c1")
    mem.mark("working")
    path = mem.save()["working"]["path"]
    raw = json.loads(open(path, encoding="utf-8").read())
    assert raw["kind"] == "working"
    assert raw["version"] == 1


def test_only_dirty_memory_is_written(mem):
    mem.working.begin_cycle("c1")
    mem.mark("working")
    assert set(mem.save()) == {"working"}
    assert mem.save() == {}          # nothing dirty the second time


# --------------------------------------------------------------------------- #
# 6. The prompt block
# --------------------------------------------------------------------------- #
def test_the_prompt_block_is_empty_when_nothing_is_remembered():
    assert memory_context_block("risk") == ""


def test_the_prompt_block_carries_evidence_calibration_and_mistakes():
    wm = WorkingMemory.empty()
    wm.begin_cycle("c1")
    wm.record_station("economic", produced={"r": "Normal"}, evidence=["macro.cpi"])
    cm = CalibrationMemory()
    cm.observe("economic", proposed={"m": 1.0}, operative={"m": 1.0})
    mm = MistakeMemory()
    mm.record("economic", kind="missing_keys", detail=["rationale absent"])

    block = memory_context_block("economic", working=wm, calibration=cm, mistakes=mm)
    assert "EVIDENCE ALREADY IN HAND" in block
    assert "macro.cpi" in block
    assert "WHAT THIS STATION NORMALLY PRODUCES" in block
    assert "MISTAKES TO NOT REPEAT" in block
    assert "rationale absent" in block


def test_the_prompt_block_says_it_never_decides():
    wm = WorkingMemory.empty()
    wm.begin_cycle("c1")
    wm.record_station("risk", produced={"v": True}, evidence=["portfolio.data"])
    assert "never decides" in memory_context_block("risk", working=wm)


def test_a_high_clamp_ratio_produces_an_actionable_hint():
    cm = CalibrationMemory()
    cm.observe("risk", proposed={"p": 0.04}, operative={"p": 0.01}, clamped=True)
    block = memory_context_block("risk", calibration=cm)
    assert "tightened by the safety rails" in block


def test_the_prompt_block_only_shows_the_asked_stage():
    mm = MistakeMemory()
    mm.record("risk", kind="risk-error", detail=["risk only"])
    block = memory_context_block("economic", mistakes=mm)
    assert "risk only" not in block


def test_describe_reports_json_only_writes(mem):
    assert mem.describe()["writable"] == "json_only"
