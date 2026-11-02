"""
test_agent_line.py - the dashboard handoff.

Two things must hold:

1. ``dashboard.state`` publishes one real ledger and refuses anything that is
   not one. No run => ``{}``, never a fabricated shift.
2. The Agent Line page renders from that ledger and degrades honestly when
   there is none.
"""
from __future__ import annotations

import json

import pytest

from blue_engine.core.agent_team import AgentTeam
from blue_engine.core.pipeline import MacroPipeline
from blue_engine.core.stage_skills import DEFAULT_SKILLS
from blue_engine.dashboard import state


@pytest.fixture(autouse=True)
def _clean():
    state.clear_shift()
    yield
    state.clear_shift()


def _shift():
    return AgentTeam(DEFAULT_SKILLS).run_shift({
        "stages": {}, "agent_advice": {}, "stage_gate": {"economic": {
            "satisfied": False, "missing": ["macro.cpi"]}},
        "gated": ["economic"], "skill_writes": {}, "self_correction": {},
        "data_requests": {}, "ready_to_trade": False,
    }).to_dict()


# --------------------------------------------------------------------------- #
# state: publish / read
# --------------------------------------------------------------------------- #
def test_publish_then_read_round_trips(tmp_path):
    target = tmp_path / "last_shift.json"
    out = state.publish_shift(_shift(), path=target)
    assert out["ok"] is True
    assert target.is_file()
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded["stopped_at"] == "economic"
    assert state.read_shift(path=target)["stopped_at"] == "economic"


def test_no_run_means_no_shift(tmp_path):
    shift, reason = state.read_shift_diagnostic(path=tmp_path / "absent.json")
    assert shift == {}
    assert reason == "no shift file"


def test_a_non_ledger_payload_is_refused_not_written(tmp_path):
    target = tmp_path / "last_shift.json"
    assert state.publish_shift({"hello": "world"}, path=target)["ok"] is False
    assert not target.is_file()


def test_a_corrupt_file_reads_as_no_shift(tmp_path):
    target = tmp_path / "last_shift.json"
    target.write_text("{not json", encoding="utf-8")
    shift, reason = state.read_shift_diagnostic(path=target)
    assert shift == {}
    assert "unreadable" in reason


def test_a_wrong_shaped_file_reads_as_no_shift(tmp_path):
    target = tmp_path / "last_shift.json"
    target.write_text(json.dumps({"product": "x"}), encoding="utf-8")
    shift, reason = state.read_shift_diagnostic(path=target)
    assert shift == {}
    assert "wrong shape" in reason


def test_an_oversized_file_reads_as_no_shift(tmp_path):
    target = tmp_path / "last_shift.json"
    target.write_text("x" * (state.MAX_SHIFT_BYTES + 10), encoding="utf-8")
    shift, reason = state.read_shift_diagnostic(path=target)
    assert shift == {}
    assert "too large" in reason


def test_the_in_process_mirror_wins_over_the_file(tmp_path):
    state.publish_shift(_shift(), path=tmp_path / "a.json")
    shift, reason = state.read_shift_diagnostic(path=tmp_path / "absent.json")
    assert reason == "in-process"
    assert shift["product"]


# --------------------------------------------------------------------------- #
# pipeline publishes automatically
# --------------------------------------------------------------------------- #
def test_a_pipeline_run_publishes_its_shift(tmp_path, monkeypatch):
    import blue_engine.dashboard.state as dash_state

    target = tmp_path / "last_shift.json"
    monkeypatch.setattr(dash_state, "shift_path", lambda: target)
    MacroPipeline({"dry_run": True, "engines": {
        "stage_gate": {"enabled": True},
        "agent_io": {"sandbox": str(tmp_path / "sandbox")}}}).run({})
    assert target.is_file()
    assert dash_state.read_shift(path=target)["stopped_at"] == "economic"
