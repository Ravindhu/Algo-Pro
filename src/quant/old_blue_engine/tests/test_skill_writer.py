"""
test_skill_writer.py - the agent write path must be JSON-only and sandboxed.

These tests are the enforcement of the operator's rule:

    "it should not edit code base, only json entry"

If any test here fails, an LLM could in principle change trading logic.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from blue_engine.core.skill_writer import (
    AgentWriteRefused,
    FORBIDDEN_EXTENSIONS,
    SkillWriter,
    DEFAULT_SANDBOX_RELATIVE,
)


@pytest.fixture()
def writer(tmp_path):
    return SkillWriter(tmp_path / "sandbox")


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_write_stage_round_trips(writer):
    res = writer.write_stage("risk", {"risk_veto_active": True,
                                      "veto_reason": "unverified flash report"})
    assert res["ok"] is True
    path = pathlib.Path(res["path"])
    assert path.name == "risk.json"
    assert json.loads(path.read_text(encoding="utf-8"))["risk_veto_active"] is True
    read = writer.read_stage("risk")
    assert read["ok"] is True
    assert read["payload"]["veto_reason"] == "unverified flash report"


def test_write_is_atomic_and_leaves_no_tmp_files(writer):
    writer.write_stage("economic", {"macro_regime": "Normal"})
    leftovers = [p.name for p in writer.sandbox.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_nested_json_paths_inside_the_sandbox_are_allowed(writer):
    res = writer.write_json("2026/10/economic.json", {"a": 1})
    assert res["ok"] is True
    assert pathlib.Path(res["path"]).parent.name == "10"


# --------------------------------------------------------------------------- #
# Refusals - extension
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ext", sorted(FORBIDDEN_EXTENSIONS))
def test_every_forbidden_extension_is_refused(writer, ext):
    res = writer.write_json(f"payload{ext}", {"x": 1})
    assert res["ok"] is False
    assert "not a .json file" in res["reason"]


def test_a_python_file_is_refused_even_with_a_json_extension_trick(writer):
    res = writer.write_json("risk_engine.py", {"x": 1})
    assert res["ok"] is False


def test_extension_check_is_case_insensitive(writer):
    res = writer.write_json("PATCH.PY", {"x": 1})
    assert res["ok"] is False


# --------------------------------------------------------------------------- #
# Refusals - location
# --------------------------------------------------------------------------- #
def test_traversal_out_of_the_sandbox_is_refused(writer):
    res = writer.write_json("../../core/risk_engine.json", {"x": 1})
    assert res["ok"] is False
    assert "escapes the sandbox" in res["reason"]


def test_an_absolute_path_is_refused(writer):
    res = writer.write_json(str(pathlib.Path.cwd() / "evil.json"), {"x": 1})
    assert res["ok"] is False
    assert "absolute path" in res["reason"]


def test_a_windows_drive_path_is_refused(writer):
    res = writer.write_json("C:/Windows/Temp/evil.json", {"x": 1})
    assert res["ok"] is False


def test_a_nul_byte_in_the_name_is_refused(writer):
    res = writer.write_json("a\x00b.json", {"x": 1})
    assert res["ok"] is False
    assert "NUL" in res["reason"]


def test_an_empty_name_is_refused(writer):
    assert writer.write_json("   ", {"x": 1})["ok"] is False


# --------------------------------------------------------------------------- #
# Refusals - protected configuration
# --------------------------------------------------------------------------- #
def test_macro_master_is_refused_even_inside_the_sandbox(writer):
    res = writer.write_json("macro_master.json", {"engines": {}})
    assert res["ok"] is False
    assert "protected configuration" in res["reason"]


def test_strategy_active_state_is_refused(writer):
    assert writer.write_json("strategy.active.json", {})["ok"] is False


def test_extra_protected_names_can_be_configured(tmp_path):
    w = SkillWriter(tmp_path / "s", protected_names=["my_limits.json"])
    assert w.write_json("my_limits.json", {})["ok"] is False


# --------------------------------------------------------------------------- #
# Refusals - content
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_refused_not_coerced(writer, bad):
    res = writer.write_json("x.json", {"v": bad})
    assert res["ok"] is False
    assert "not JSON-serialisable" in res["reason"]


def test_a_scalar_payload_is_refused(writer):
    assert writer.write_json("x.json", 5)["ok"] is False
    assert writer.write_json("x.json", "text")["ok"] is False


def test_a_datetime_is_serialised_as_text_not_refused(writer):
    import datetime as dt
    res = writer.write_json("x.json", {"ts": dt.datetime(2026, 1, 1)})
    assert res["ok"] is True


# --------------------------------------------------------------------------- #
# Strict mode + result shape
# --------------------------------------------------------------------------- #
def test_strict_mode_raises_on_a_refusal(writer):
    with pytest.raises(AgentWriteRefused):
        writer.write_json("core/risk_engine.py", {"x": 1}, strict=True)


def test_result_shape_is_stable(writer):
    res = writer.write_stage("market", {"market_volatility_state": "Low"})
    assert set(res) == {"ok", "path", "reason", "bytes", "written_at"}
    assert res["bytes"] > 0


def test_an_invalid_stage_name_is_refused(writer):
    assert writer.write_stage("../risk", {})["ok"] is False
    assert writer.write_stage("", {})["ok"] is False


def test_read_stage_on_a_missing_file_never_raises(writer):
    out = writer.read_stage("nope")
    assert out["ok"] is False and out["payload"] is None


# --------------------------------------------------------------------------- #
# Audit trail
# --------------------------------------------------------------------------- #
def test_refusals_are_audited(writer):
    writer.write_json("core/risk_engine.py", {"x": 1})
    audit = writer.sandbox / "_audit" / "writes.jsonl"
    assert audit.is_file()
    rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert any(row["ok"] is False and "risk_engine.py" in row["target"]
               for row in rows)


def test_audit_can_be_disabled(tmp_path):
    w = SkillWriter(tmp_path / "s", audit=False)
    w.write_stage("risk", {"risk_veto_active": True})
    assert not (tmp_path / "s" / "_audit").exists()


# --------------------------------------------------------------------------- #
# Architectural invariant - execution is logic only
# --------------------------------------------------------------------------- #
EXECUTION_MODULES = [
    "core/risk_gate.py",
    "core/risk_engine.py",
    "core/orchestrator.py",
    "core/live_monitor.py",
    "core/runner.py",
    "core/ledger.py",
    "pov/pov_engine.py",
    "pov/slicers/pov.py",
    "pov/slicers/base.py",
]


@pytest.mark.parametrize("rel", EXECUTION_MODULES)
def test_execution_modules_never_import_the_writer(rel):
    """An execution module must not be able to read or write agent JSON.

    This is the test that keeps "execution engine is fully logic" true over
    time: adding ``from .skill_writer import ...`` to any of these files fails
    the suite.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / rel
    if not path.is_file():          # module layout may evolve; skip, do not fail
        pytest.skip(f"{rel} not present")
    text = path.read_text(encoding="utf-8")
    for needle in ("skill_writer", "skill_writer import", "stage_skills",
                   "stage_gate", "frontline_agent", "core.llm"):
        assert needle not in text, f"{rel} must stay logic-only (found {needle!r})"


def test_default_sandbox_is_under_the_project_data_dir():
    """Sandbox is <repo>/data/agent_stage, whatever the checkout folder is called."""
    from blue_engine.core import skill_writer as mod
    root = mod._repo_root()
    assert root / DEFAULT_SANDBOX_RELATIVE == root / "data" / "agent_stage"
