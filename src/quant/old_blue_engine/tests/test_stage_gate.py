"""
test_stage_gate.py - the data-sufficiency gate must be read-only, deterministic
and FAIL-CLOSED.

The invariants under test:

1. A missing requirement blocks (never silently passes).
2. A requirement that cannot be EVALUATED blocks (e.g. bad config, freshness
   with no timestamp path).
3. A disabled gate never blocks, so enabling it is a deliberate decision.
4. The gate never raises and never mutates the context it is handed.
5. ``on_block`` is validated at construction, so a typo cannot silently
   degrade to "warn".
"""
from __future__ import annotations

import copy
import datetime as dt

import pytest

from blue_engine.core.stage_gate import (
    StageGate,
    StageGateError,
    check_requirements,
    resolve_path,
    Requirement,
)


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #
def test_resolve_path_walks_dicts_and_list_indexes():
    ctx = {"a": {"b": [{"c": 1}]}}
    assert resolve_path(ctx, "a.b.0.c") == (True, 1)
    assert resolve_path(ctx, "a.b.1.c") == (False, None)
    assert resolve_path(ctx, "missing") == (False, None)
    assert resolve_path(ctx, "a.b.0.c.d") == (False, None)
    assert resolve_path(ctx, "") == (False, None)


def test_resolve_path_does_not_confuse_a_string_with_a_list():
    assert resolve_path({"n": "abc", "i": "1"}, "n.0") == (False, None)
    assert resolve_path({"i": 5}, "i.0") == (False, None)


# --------------------------------------------------------------------------- #
# Defaults: disabled by default
# --------------------------------------------------------------------------- #
def test_gate_is_disabled_by_default():
    gate = StageGate.from_config({})
    verdict = gate.check("economic", {})
    assert verdict.satisfied is True
    assert "disabled" in verdict.reason


def test_disabled_gate_cannot_block_even_with_bad_context():
    gate = StageGate({"enabled": False, "stages": {
        "economic": [{"path": "macro", "check": "present"}]}})
    assert gate.check("economic", {}).satisfied is True


def test_enabled_gate_uses_default_requirements_for_unlisted_stages():
    gate = StageGate({"enabled": True})
    assert gate.has_requirements("economic")
    assert not gate.check("economic", {}).satisfied
    assert gate.check("economic", {"macro": {"cpi": 5.0}}).satisfied


def test_stage_with_no_requirements_is_reported_not_assumed_checked():
    gate = StageGate({"enabled": True, "stages": {"economic": []}})
    verdict = gate.check("economic", {})
    assert verdict.satisfied is True
    assert verdict.reason == "no requirements declared"


# --------------------------------------------------------------------------- #
# Every check, in both directions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("req,ctx,expected", [
    ({"path": "x", "check": "present"}, {"x": 1}, True),
    ({"path": "x", "check": "present"}, {"x": None}, False),
    ({"path": "x", "check": "present"}, {"x": ""}, False),
    ({"path": "x", "check": "present"}, {"x": []}, False),
    ({"path": "x", "check": "present"}, {"x": {}}, False),
    ({"path": "x", "check": "min_rows", "value": 2}, {"x": [1, 2]}, True),
    ({"path": "x", "check": "min_rows", "value": 2}, {"x": [1]}, False),
    ({"path": "x", "check": "min_rows", "value": 1}, {"x": 5}, False),
    ({"path": "x", "check": "min_count", "value": 3}, {"x": 3}, True),
    ({"path": "x", "check": "min_count", "value": 3}, {"x": 2.9}, False),
    ({"path": "x", "check": "min_count", "value": 1}, {"x": float("nan")}, False),
    ({"path": "x", "check": "range", "min_value": 0, "max_value": 1},
     {"x": 0.5}, True),
    ({"path": "x", "check": "range", "min_value": 0, "max_value": 1},
     {"x": 1.5}, False),
    ({"path": "x", "check": "range", "min_value": 0, "max_value": 1},
     {"x": True}, False),
    ({"path": "x", "check": "one_of", "value": ["a", "b"]}, {"x": "b"}, True),
    ({"path": "x", "check": "one_of", "value": ["a", "b"]}, {"x": "c"}, False),
    ({"path": "x", "check": "enum_keys", "value": ["a", "b"]},
     {"x": {"a": 1, "b": 2}}, True),
    ({"path": "x", "check": "enum_keys", "value": ["a", "b"]},
     {"x": {"a": 1}}, False),
])
def test_each_check_passes_and_fails_as_documented(req, ctx, expected):
    gate = StageGate({"enabled": True, "stages": {"s": [req]}})
    verdict = gate.check("s", ctx)
    assert verdict.satisfied is expected, verdict.to_dict()


def test_a_requirement_with_a_bad_value_blocks_instead_of_raising():
    gate = StageGate({"enabled": True, "stages": {
        "s": [{"path": "x", "check": "min_count", "value": "not-a-number"}]}})
    verdict = gate.check("s", {"x": 10})
    assert verdict.satisfied is False
    assert "could not be evaluated" in verdict.checks[0].reason


def test_one_blocked_requirement_blocks_the_whole_stage():
    gate = StageGate({"enabled": True, "stages": {"s": [
        {"path": "a", "check": "present"},
        {"path": "b", "check": "present"},
    ]}})
    verdict = gate.check("s", {"a": 1})
    assert verdict.satisfied is False
    assert verdict.missing == ["b"]
    assert len(verdict.checks) == 2


# --------------------------------------------------------------------------- #
# Freshness - the fail-closed corner
# --------------------------------------------------------------------------- #
def _fresh_ts(seconds_ago: float) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")


def test_fresh_data_passes_the_age_check():
    gate = StageGate({"enabled": True, "stages": {"s": [
        {"path": "x", "check": "present", "timestamp_path": "as_of",
         "max_age_seconds": 3600}]}})
    verdict = gate.check("s", {"x": 1, "as_of": _fresh_ts(60)})
    assert verdict.satisfied is True


def test_stale_data_blocks():
    gate = StageGate({"enabled": True, "stages": {"s": [
        {"path": "x", "check": "present", "timestamp_path": "as_of",
         "max_age_seconds": 60}]}})
    verdict = gate.check("s", {"x": 1, "as_of": _fresh_ts(7200)})
    assert verdict.satisfied is False
    assert "age" in verdict.checks[0].reason


def test_freshness_required_without_a_timestamp_path_blocks():
    gate = StageGate({"enabled": True, "stages": {"s": [
        {"path": "x", "check": "present", "require_freshness": True}]}})
    verdict = gate.check("s", {"x": 1})
    assert verdict.satisfied is False
    assert "timestamp_path" in verdict.checks[0].reason


def test_a_naive_timestamp_is_rejected_not_assumed_utc():
    gate = StageGate({"enabled": True, "stages": {"s": [
        {"path": "x", "check": "present", "timestamp_path": "as_of",
         "max_age_seconds": 3600}]}})
    # Fixed and deliberately ancient: no wall-clock dependency, so this test
    # cannot become flaky as time passes.
    verdict = gate.check("s", {"x": 1, "as_of": "2020-01-01T10:00:00"})
    assert verdict.satisfied is False
    assert "timezone-aware" in verdict.checks[0].reason


def test_a_future_timestamp_is_flagged_as_clock_skew():
    future = (dt.datetime.now(dt.timezone.utc)
              + dt.timedelta(hours=2)).isoformat(timespec="seconds")
    gate = StageGate({"enabled": True, "stages": {"s": [
        {"path": "x", "check": "present", "timestamp_path": "as_of",
         "max_age_seconds": 3600}]}})
    verdict = gate.check("s", {"x": 1, "as_of": future})
    assert verdict.satisfied is False
    assert "future" in verdict.checks[0].reason


def test_the_age_check_uses_the_injected_clock():
    """A caller-supplied ``now`` makes the freshness rule deterministic."""
    gate = StageGate({"enabled": True, "stages": {"s": [
        {"path": "x", "check": "present", "timestamp_path": "as_of",
         "max_age_seconds": 60}]}})
    ctx = {"x": 1, "as_of": "2026-03-01T09:00:00+00:00"}
    on_time = dt.datetime(2026, 3, 1, 9, 0, 30, tzinfo=dt.timezone.utc)
    late = dt.datetime(2026, 3, 1, 9, 5, 0, tzinfo=dt.timezone.utc)
    assert gate.check("s", ctx, now=on_time).satisfied is True
    assert gate.check("s", ctx, now=late).satisfied is False


# --------------------------------------------------------------------------- #
# Config validation + purity
# --------------------------------------------------------------------------- #
def test_on_block_is_validated_at_construction():
    with pytest.raises(StageGateError):
        StageGate({"enabled": True, "on_block": "ignore"})


def test_a_requirement_without_a_path_is_a_config_error():
    with pytest.raises(StageGateError):
        StageGate({"enabled": True, "stages": {"s": [{"check": "present"}]}})


def test_an_unknown_check_is_a_config_error():
    with pytest.raises(StageGateError):
        StageGate({"enabled": True, "stages": {"s": [
            {"path": "x", "check": "magic"}]}})


def test_requirements_must_be_a_list():
    with pytest.raises(StageGateError):
        StageGate({"enabled": True, "stages": {"s": {"path": "x"}}})


def test_the_gate_never_mutates_the_context():
    ctx = {"macro": {"cpi": 5.0}}
    snapshot = copy.deepcopy(ctx)
    gate = StageGate({"enabled": True})
    gate.check("economic", ctx)
    assert ctx == snapshot


def test_check_requirements_matches_the_gate_verdict():
    reqs = [Requirement.from_dict({"path": "x", "check": "present"})]
    passed, results = check_requirements(reqs, {"x": 1})
    assert passed is True and results[0].passed is True
    passed, results = check_requirements(reqs, {})
    assert passed is False and results[0].reason == "path not found"


def test_describe_is_json_safe():
    import json
    gate = StageGate({"enabled": True})
    json.dumps(gate.describe())  # must not raise
    assert gate.describe()["enabled"] is True
