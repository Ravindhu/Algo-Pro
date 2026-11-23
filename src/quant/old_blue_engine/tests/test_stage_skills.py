"""
test_stage_skills.py - small per-stage skills stay in sync with the code.

The critical regression guarded here: a skill's declared ``schema`` must equal
``frontline_agent._SCHEMAS[stage]``. If someone edits ``_SCHEMAS`` and forgets
the skill (or the reverse), the skill would describe a contract the validator
does not accept - so the test fails instead of the operator finding out in
production.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from blue_engine.core import frontline_agent
from blue_engine.core.frontline_agent import _CAPS, _validate
from blue_engine.core.stage_skills import (
    AGENT_STAGES,
    DEFAULT_SKILLS,
    PIPELINE_SKILL_STAGES,
    load_skills,
    skills_document,
)


# ROOT is the engine folder: tests -> blue_engine. The repo root is four levels
# above this file (tests -> blue_engine -> quant -> src -> repo root), because the
# engine now lives at src/quant/blue_engine/. Sibling trees (docs/, skills/) are
# resolved from REPO_ROOT, never from ROOT.parent, which is only src/quant.
ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
# The per-stage skill docs were MOVED to docs/skills by 81ef720
# ("chore(layout): match AGENTS.md tree"), which also records "docs only, no code
# loads them". The markdown assertions below stayed pointed at the old folder and
# failed with FileNotFoundError. Keep a fallback so a future layout move that
# keeps either location working does not break this suite.
SKILLS_DIR = next(
    (p for p in (REPO_ROOT / "docs" / "skills", ROOT / "skills") if p.is_dir()),
    REPO_ROOT / "docs" / "skills",
)


# --------------------------------------------------------------------------- #
# Coverage + schema parity
# --------------------------------------------------------------------------- #
def test_every_pipeline_stage_has_a_skill():
    assert set(PIPELINE_SKILL_STAGES) <= set(DEFAULT_SKILLS)


def test_every_pipeline_stage_has_a_prompt_schema():
    assert set(PIPELINE_SKILL_STAGES) == set(frontline_agent._SCHEMAS)


@pytest.mark.parametrize("stage", sorted(frontline_agent._SCHEMAS))
def test_skill_schema_matches_the_validator(stage):
    assert DEFAULT_SKILLS[stage].schema == frontline_agent._SCHEMAS[stage], (
        f"skill schema for {stage!r} drifted from frontline_agent._SCHEMAS")


def test_default_skill_answers_are_accepted_by_the_validator():
    """A representative good answer per stage must pass ``_validate``."""
    good = {
        "economic": {"macro_regime": "Normal", "equity_regime_multiplier": 1.0,
                     "interest_rate_bias": "Neutral",
                     "rationale": "steady inflation and growth"},
        "market": {"market_volatility_state": "Low", "technical_lookback_days": 20,
                   "short_term_bias": "Sideways", "rationale": "range bound index"},
        "screener": {"asset_blacklist": ["XYZ"], "blacklist_reasons": [
            {"ticker": "XYZ", "reason": "auditor resignation disclosed"}]},
        "valuation": {"target_ticker": "INFY", "growth_rate_ceiling": 0.03,
                      "margin_safety_adjustment": 0.9,
                      "rationale": "conservative software growth"},
        "backtest": {"historical_start_date": "2020-01-01",
                     "slippage_penalty_pct": 0.02,
                     "flaw_detected": "no look ahead bias found"},
        "optimization": {"min_exploration_rsi": 20, "max_exploration_rsi": 80,
                         "optimization_method": "bounded_scipy",
                         "rationale": "bound the search space"},
        "portfolio": {"max_tech_sector_exposure": 0.3, "min_cash_buffer_pct": 0.1,
                      "rebalance_allowed": False,
                      "rationale": "defensive posture"},
        "risk": {"risk_veto_active": True, "max_allowed_slippage_pct": 0.01,
                 "force_trailing_stop_pct": 2.0,
                 "veto_reason": "unverified flash report"},
    }
    for stage, payload in good.items():
        assert _validate(stage, payload) is not None, stage


def test_no_skill_declares_a_key_outside_its_validated_schema():
    for stage, skill in DEFAULT_SKILLS.items():
        if stage not in frontline_agent._SCHEMAS:
            continue
        assert set(skill.schema) == set(frontline_agent._SCHEMAS[stage])


def test_every_skill_has_a_unique_id_and_a_json_filename():
    ids = [s.skill_id for s in DEFAULT_SKILLS.values()]
    assert len(set(ids)) == len(ids)
    for skill in DEFAULT_SKILLS.values():
        assert skill.filename.endswith(".json")
        assert skill.writes == "json_only"
        assert skill.failless is True
        assert skill.purpose


def test_every_skill_accepts_data_requests():
    for name in AGENT_STAGES:
        assert DEFAULT_SKILLS[name].accepts_data_requests() is True


# --------------------------------------------------------------------------- #
# Config overrides
# --------------------------------------------------------------------------- #
def test_a_config_override_replaces_the_default_schema():
    cfg = {"engines": {"screener": {"skill": {
        "schema": {"asset_blacklist": ("tickers",)}}}}}
    skills = load_skills(cfg)
    assert set(skills["screener"].schema) == {"asset_blacklist"}
    assert skills["screener"].source == "config_override"


def test_a_disabled_skill_reports_itself_as_disabled():
    cfg = {"engines": {"risk": {"skill": {"enabled": False}}}}
    skills = load_skills(cfg)
    assert skills["risk"].source == "disabled"
    assert skills["risk"].schema == {}


def test_disabled_skills_can_be_excluded_from_the_mapping():
    cfg = {"engines": {"risk": {"skill": {"enabled": False}}}}
    skills = load_skills(cfg, include_disabled=False)
    assert "risk" not in skills


def test_data_needs_can_be_overridden():
    cfg = {"engines": {"economic": {"skill": {"data_needs": [
        {"path": "macro.cpi", "check": "range", "min_value": 0, "max_value": 20}]}}}}
    needs = load_skills(cfg)["economic"].data_needs
    assert needs[0]["path"] == "macro.cpi"


def test_a_data_need_without_a_path_is_rejected():
    cfg = {"engines": {"economic": {"skill": {"data_needs": [{"check": "present"}]}}}}
    with pytest.raises(ValueError):
        load_skills(cfg)


def test_a_non_json_filename_is_rejected():
    cfg = {"engines": {"risk": {"skill": {"filename": "risk.py"}}}}
    with pytest.raises(ValueError):
        load_skills(cfg)


# --------------------------------------------------------------------------- #
# Prompt + document
# --------------------------------------------------------------------------- #
def test_instruction_names_every_schema_key():
    for stage in PIPELINE_SKILL_STAGES:
        skill = DEFAULT_SKILLS[stage]
        doc = skill.instruction()
        assert sorted(doc["must_return_json_with_exactly"]) == sorted(skill.schema)
        assert doc["role"].endswith("STAGE SKILL")
        assert any("data only" in rule.lower() for rule in doc["rules"])


def test_skills_document_is_json_safe_and_declares_json_only_writes():
    doc = skills_document()
    json.dumps(doc)  # must not raise
    assert doc["writes"] == "json_only"
    assert doc["count"] == len(DEFAULT_SKILLS)


# --------------------------------------------------------------------------- #
# The skills documentation must exist and stay honest
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stage", AGENT_STAGES)
def test_each_stage_has_a_skill_markdown_file(stage):
    path = SKILLS_DIR / stage / "SKILL.md"
    assert path.is_file(), f"missing {path}"
    text = path.read_text(encoding="utf-8")
    assert DEFAULT_SKILLS[stage].skill_id in text
    assert "Publishes" in text or "Output" in text


def test_readme_lists_every_skill():
    readme = (SKILLS_DIR / "README.md").read_text(encoding="utf-8")
    for skill in DEFAULT_SKILLS.values():
        assert skill.skill_id in readme


# --------------------------------------------------------------------------- #
# The caps are a code rail, not a skill feature
# --------------------------------------------------------------------------- #
def test_caps_live_in_code_and_cover_the_veto_and_rebalance_toggles():
    assert _CAPS["risk"]["risk_veto_active"][0] is True
    assert _CAPS["portfolio"]["rebalance_allowed"][0] is False


def test_skills_do_not_import_or_redefine_caps():
    text = (ROOT / "core" / "stage_skills.py").read_text(encoding="utf-8")
    assert "_CAPS = " not in text
    assert "from .frontline_agent import" not in text
