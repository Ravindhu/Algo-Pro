"""JSON-patch edit-protocol tests (docs/FRONTLINE_PATCH_PROTOCOL_PLAN.md)."""
from __future__ import annotations

import json

import pytest

from blue_engine.core.llm.patch_protocol import (
    PatchError,
    apply_patch,
    extract_target,
    make_edit_instruction,
    parse_patch,
    strip_ws,
)
from blue_engine.core.frontline_agent import (
    FrontlineAgent,
    _schema_as_constraint_text,
    _SCHEMAS,
)
from blue_engine.core.llm.protocol import ChatMessage, ChatResult


# --- element 4: whitespace stripping ---------------------------------------
def test_strip_ws_collapses_strings_and_nested_containers():
    assert strip_ws("  a\n\tb  ") == "a b"
    payload = {"a": " x  y ", "b": [" p ", {"c": " q "}]}
    assert strip_ws(payload) == {"a": "x y", "b": ["p", {"c": "q"}]}
    assert strip_ws(5) == 5


# --- elements 1/2: parse and apply ------------------------------------------
def test_parse_and_apply_set_and_inc_ops():
    ops = parse_patch('[{"op": "set", "key": "macro_regime", "value": "Normal"},'
                      '{"op": "inc", "key": "equity_regime_multiplier", "value": -0.1}]')
    base = {"macro_regime": "Expansionary", "equity_regime_multiplier": 1.0}
    out = apply_patch(base, ops)
    assert out == {"macro_regime": "Normal", "equity_regime_multiplier": 0.9}
    assert base == {"macro_regime": "Expansionary",
                    "equity_regime_multiplier": 1.0}, "base must not be mutated"


def test_unknown_op_or_missing_key_fails_closed():
    with pytest.raises(PatchError):
        parse_patch('[{"op": "delete", "key": "x", "value": 1}]')
    with pytest.raises(PatchError):
        parse_patch('[{"op": "set", "key": "", "value": 1}]')
    with pytest.raises(PatchError):
        parse_patch('[{"op": "set", "key": "x"}]')
    with pytest.raises(PatchError):
        parse_patch('{"op": "set"}')  # not an array
    with pytest.raises(PatchError):
        parse_patch("not json at all")


def test_inc_on_a_non_numeric_target_fails_closed():
    with pytest.raises(PatchError):
        apply_patch({"rationale": "text"},
                    [{"op": "inc", "key": "rationale", "value": 1}])


# --- element 3: target extraction -------------------------------------------
def test_extract_target_drops_foreign_keys():
    payload = {"macro_regime": "Normal", "risk_veto_active": True, "junk": 1}
    out = extract_target("economic", payload)
    assert "risk_veto_active" not in out and "junk" not in out
    assert out["macro_regime"] == "Normal"


def test_every_registered_stage_narrows_to_its_own_schema():
    for engine in _SCHEMAS:
        payload = {"foreign": True}
        assert extract_target(engine, payload) == {}


# --- element 6: strict schema on the patched result -------------------------
def test_patch_result_must_pass_the_stage_schema():
    agent = FrontlineAgent({"enabled": True, "patch": {"enabled": True}},
                           client=_ScriptedClient(_patch_reply()))
    outcome = agent.advise(
        "economic", "inflation cooled",
        previous_advice={"macro_regime": "Expansionary",
                         "equity_regime_multiplier": 1.0,
                         "interest_rate_bias": "Neutral",
                         "rationale": "growth steady"},
        patch_mode=True)
    assert outcome.get("ok") is True
    assert outcome.get("patch_mode") is True
    # enum value in the patch reply is invalid -> must have fallen back or
    # been rejected; here the scripted reply sets a VALID value.
    assert outcome["advice"]["macro_regime"] == "Normal"


def test_invalid_patch_result_is_not_returned():
    agent = FrontlineAgent({"enabled": True, "patch": {"enabled": True}},
                           client=_ScriptedClient('[{"op": "set", "key": "macro_regime", '
                                                  '"value": "NOT_AN_ENUM"}]'))
    outcome = agent.advise(
        "economic", "inflation cooled",
        previous_advice={"macro_regime": "Expansionary",
                         "equity_regime_multiplier": 1.0,
                         "interest_rate_bias": "Neutral",
                         "rationale": "growth steady"},
        patch_mode=True)
    assert outcome.get("patch_mode") is None, "invalid patched advice is refused"


def test_malformed_patch_falls_back_to_full_regeneration():
    agent = FrontlineAgent({"enabled": True, "patch": {"enabled": True}},
                           client=_ScriptedClient("garbage",
                                                  full='{"macro_regime": "Normal", '
                                                       '"equity_regime_multiplier": 1.0, '
                                                       '"interest_rate_bias": "Neutral", '
                                                       '"rationale": "growth steady"}'))
    outcome = agent.advise(
        "economic", "inflation cooled",
        previous_advice={"macro_regime": "Expansionary",
                         "equity_regime_multiplier": 1.0,
                         "interest_rate_bias": "Neutral",
                         "rationale": "growth steady"},
        patch_mode=True)
    assert outcome.get("ok") is True and "patch_mode" not in outcome


def test_patch_cannot_loosen_a_veto_cap():
    # equity_regime_multiplier cap: safe direction is DOWN from 1.5 baseline.
    agent = FrontlineAgent({"enabled": True, "patch": {"enabled": True}},
                           client=_ScriptedClient(
                               '[{"op": "set", "key": "equity_regime_multiplier", '
                               '"value": 1.5}]'))
    outcome = agent.advise(
        "economic", "markets calm",
        baseline={"equity_regime_multiplier": 1.0},
        previous_advice={"macro_regime": "Normal",
                         "equity_regime_multiplier": 1.0,
                         "interest_rate_bias": "Neutral",
                         "rationale": "steady"},
        patch_mode=True)
    assert outcome["advice"]["equity_regime_multiplier"] <= 1.0, \
        "the safe-direction clamp still governs a patched value"


# --- element 7: low-model switch ---------------------------------------------
def test_edit_call_uses_the_low_model():
    client = _ScriptedClient(_patch_reply())
    agent = FrontlineAgent(
        {"enabled": True, "patch": {"enabled": True,
                                    "edit_model": "ollama/qwen3:8b"}},
        client=client)
    agent.advise("economic", "inflation cooled",
                 previous_advice={"macro_regime": "Expansionary",
                                  "equity_regime_multiplier": 1.0,
                                  "interest_rate_bias": "Neutral",
                                  "rationale": "growth steady"},
                 patch_mode=True)
    assert client.last_kwargs.get("model") == "ollama/qwen3:8b"


# --- element 8: static-schema cache -------------------------------------------
def test_edit_instruction_is_compiled_once_per_engine():
    from blue_engine.core.llm import patch_protocol

    patch_protocol._instruction_cache.clear()
    first = make_edit_instruction("economic", _schema_as_constraint_text("economic"))
    second = make_edit_instruction("economic", _schema_as_constraint_text("economic"))
    assert first is second, "memoised: same object, compiled once"
    assert "PATCH" in first.upper() or "patch" in first


def _patch_reply():
    return json.dumps([{"op": "set", "key": "macro_regime", "value": "Normal"}])


class _ScriptedClient:
    """Answers the patch call (or a full regen) with scripted text."""

    def __init__(self, patch_text, full=None):
        self.patch_text = patch_text
        self.full = full
        self.calls = 0
        self.last_kwargs: dict = {}
        self.session_id = None
        self.governor = None

    class _AlwaysAvailable:
        name = "fake"

        def available(self):
            return True

        def status(self):
            return {"backend": "fake"}

    backends = [_AlwaysAvailable()]

    def status(self):
        return {"chain": [b.name for b in self.backends]}

    def complete(self, messages, **kwargs):
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        # Distinguish: the patch prompt asks for a JSON ARRAY.
        wants_patch = any("JSON patch" in getattr(m, "content", "")
                          for m in messages)
        text = self.patch_text if wants_patch else (self.full or "{}")
        return ChatResult(ok=True, text=text, backend="fake", model="m",
                          model_digest="sha256:x", latency_ms=1, usage={})


def test_full_regen_path_still_works_without_previous_advice():
    agent = FrontlineAgent({"enabled": True},
                           client=_ScriptedClient(_patch_reply(),
                                                  full='{"macro_regime": "Normal", '
                                                       '"equity_regime_multiplier": 1.0, '
                                                       '"interest_rate_bias": "Neutral", '
                                                       '"rationale": "growth steady"}'))
    outcome = agent.advise("economic", "inflation cooled")
    assert outcome.get("ok") is True and "patch_mode" not in outcome
