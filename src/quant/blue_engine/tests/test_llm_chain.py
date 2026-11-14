"""Tests for the frontline LLM chain: Ollama -> OpenRouter -> Disabled.

These lock the properties that let an LLM sit anywhere near a trading system:

1. failover order and quota handling (a 429 must not be retried all day)
2. the deterministic caller still owns validation (no unvalidated advice)
3. agentic mode is bounded (rounds, calls) and its tools are read-only by
   contract, so it cannot widen a limit or clear a veto
4. model identity is recorded (digest pinning) for reproducibility
"""
from __future__ import annotations

import json

from blue_engine.core.frontline_agent import FrontlineAgent, _CAPS, _escalate
from blue_engine.core.llm import ChatMessage, LLMClient
from blue_engine.core.llm.backends import (
    DisabledBackend,
    OllamaBackend,
    OpenRouterBackend,
)
from blue_engine.core.llm.router import AgenticLLM


class FakeBackend:
    """Scripted backend: returns queued results and records calls."""

    def __init__(self, name, results, available=True):
        self.name = name
        self._results = list(results)
        self._available = available
        self.calls = 0

    def available(self):
        return self._available

    def status(self):
        return {"backend": self.name}

    def complete(self, messages, **kwargs):
        from blue_engine.core.llm.protocol import ChatResult

        self.calls += 1
        if not self._results:
            return ChatResult(ok=False, backend=self.name,
                              attempts=[{"backend": self.name, "reason": "no_script"}])
        item = self._results.pop(0)
        if isinstance(item, ChatResult):
            return item
        return ChatResult(ok=True, text=item, backend=self.name,
                          model="fake-model", model_digest="sha256:deadbeef")


def _result_text(payload):
    return json.dumps(payload)


_RISK_OK = {
    "risk_veto_active": True,
    "max_allowed_slippage_pct": 0.002,
    "force_trailing_stop_pct": 2.0,
    "veto_reason": "Unverified flash report",
}


# ---------------------------------------------------------------------------
# Router behaviour
# ---------------------------------------------------------------------------
def test_router_fails_over_from_ollama_to_openrouter():
    ollama = FakeBackend("ollama", [], available=False)
    openrouter = FakeBackend("openrouter", [_result_text(_RISK_OK)])
    client = LLMClient([ollama, openrouter])

    result = client.complete([ChatMessage("user", "x")])

    assert result.ok and result.backend == "openrouter"
    assert ollama.calls == 0, "an unavailable backend must not be called"
    assert {"backend": "ollama", "reason": "unavailable"} in result.attempts


def test_router_reports_disabled_when_chain_is_empty():
    result = LLMClient([DisabledBackend("no backend configured")]).complete(
        [ChatMessage("user", "x")])
    assert not result.ok
    assert result.attempts[-1]["backend"] == "disabled"


# ---------------------------------------------------------------------------
# Free-tier quota handling
# ---------------------------------------------------------------------------
def test_openrouter_429_marks_the_backend_exhausted_for_the_day(monkeypatch):
    backend = OpenRouterBackend(api_key="key", model="m:free")
    monkeypatch.setattr(backend, "_post", lambda *a, **k: (None, "rate_limited"))

    first = backend.complete([ChatMessage("user", "x")])
    assert not first.ok and first.attempts[-1]["reason"] == "rate_limited"
    assert backend.exhausted

    # No second network call: the router must see this as unavailable.
    called = []
    monkeypatch.setattr(backend, "_post", lambda *a, **k: called.append(1))
    second = backend.complete([ChatMessage("user", "x")])
    assert not second.ok
    assert second.attempts[-1]["reason"] == "daily_quota_exhausted"
    assert called == [], "an exhausted backend must not spend another request"


def test_ollama_needs_no_credential():
    assert OllamaBackend(host="http://127.0.0.1:11434").available()
    assert not OpenRouterBackend(api_key=None).available()


# ---------------------------------------------------------------------------
# FrontlineAgent on the new chain
# ---------------------------------------------------------------------------
def _agent_with(backends, **config):
    agent = FrontlineAgent({"enabled": True, **config})
    agent.client = LLMClient(backends)
    return agent


def test_agent_works_with_ollama_alone_no_openrouter_key():
    """The whole point of Ollama-primary: no cloud credential is required."""
    agent = _agent_with([FakeBackend("ollama", [_result_text(_RISK_OK)])])

    result = agent.advise("risk", "public report text")

    assert result["ok"] is True
    assert result["method"] == "ollama"
    assert result["model_digest"] == "sha256:deadbeef"


def test_agent_rejects_schema_invalid_output_even_from_the_primary_backend():
    bad = dict(_RISK_OK, risk_veto_active="false")  # string, not bool
    agent = _agent_with([FakeBackend("ollama", [_result_text(bad)])])

    result = agent.advise("risk", "text")

    assert result["ok"] is False
    assert result["method"] == "invalid_response"


def test_agent_reports_quota_exhaustion_without_raising():
    agent = _agent_with([FakeBackend("openrouter", [], available=False)])
    result = agent.advise("risk", "text")
    assert result["ok"] is False
    assert result["method"] == "disabled"


# ---------------------------------------------------------------------------
# Escalation-only caps: the safety property for agentic use
# ---------------------------------------------------------------------------
def test_caps_are_defined_for_every_veto_capable_engine():
    assert _CAPS["risk"]["risk_veto_active"][0] is True
    assert _CAPS["risk"]["risk_veto_active"][2] == "toggle"
    assert _CAPS["portfolio"]["min_cash_buffer_pct"] == (0.0, 1)
    assert _CAPS["portfolio"]["max_tech_sector_exposure"] == (1.0, -1)


def test_agent_cannot_clear_a_veto_when_a_baseline_says_veto():
    advice = _escalate("risk", dict(_RISK_OK, risk_veto_active=False),
                       baseline={"risk_veto_active": True})
    assert advice["risk_veto_active"] is True


def test_agent_cannot_widen_exposure_past_the_baseline_ceiling():
    advice = _escalate("portfolio", {
        "max_tech_sector_exposure": 0.9,      # tries to RAISE a ceiling
        "min_cash_buffer_pct": 0.01,          # tries to LOWER the buffer
        "rebalance_allowed": True,            # tries to ENABLE trading
        "rationale": "let it run",
    }, baseline={"max_tech_sector_exposure": 0.2,
                 "min_cash_buffer_pct": 0.15,
                 "rebalance_allowed": False})
    assert advice["max_tech_sector_exposure"] == 0.2
    assert advice["min_cash_buffer_pct"] == 0.15
    assert advice["rebalance_allowed"] is False


def test_without_a_baseline_a_toggle_still_cannot_be_enabled():
    """rebalance_allowed has an absolute safe side, so it is forced off."""
    advice = _escalate("portfolio", {"rebalance_allowed": True,
                                     "rationale": "x"})
    assert advice["rebalance_allowed"] is False


def test_agent_may_tighten_and_that_is_preserved():
    advice = _escalate("portfolio", {
        "max_tech_sector_exposure": 0.1,
        "min_cash_buffer_pct": 0.25,
        "rebalance_allowed": False,
        "rationale": "concentration risk",
    })
    assert advice["max_tech_sector_exposure"] == 0.1
    assert advice["min_cash_buffer_pct"] == 0.25


def test_tighter_slippage_penalty_is_kept_because_higher_is_safer():
    advice = _escalate("backtest", {"slippage_penalty_pct": 0.03,
                                    "historical_start_date": "2020-01-01",
                                    "flaw_detected": "mapping"})
    assert advice["slippage_penalty_pct"] == 0.03


# ---------------------------------------------------------------------------
# Agentic loop: bounded, read-only, schema-gated
# ---------------------------------------------------------------------------
def test_agentic_loop_is_hard_clamped_even_if_configured_huge():
    loop = AgenticLLM(LLMClient([DisabledBackend()]),
                      max_rounds=99, max_calls=99)
    assert loop.max_rounds == 4
    assert loop.max_calls == 6


def test_agentic_tool_result_is_fed_back_and_a_valid_payload_wins():
    tool_request = json.dumps({"tool": "latest_headline", "arguments": {"symbol": "X"}})
    backend = FakeBackend("ollama", [tool_request, _result_text(_RISK_OK)])
    seen = []

    def latest_headline(args):
        seen.append(args)
        return {"headline": "RBI holds rates"}

    agent = _agent_with([backend])
    result = agent.advise("risk", "text", tools={"latest_headline": latest_headline})

    assert result["ok"] is True
    assert result["advice"]["risk_veto_active"] is True
    assert seen == [{"symbol": "X"}]
    assert result["calls"] == 2
    assert result["tool_trace"][0]["tool"] == "latest_headline"


def test_agentic_loop_stops_at_the_call_budget():
    tool_request = json.dumps({"tool": "t", "arguments": {}})
    backend = FakeBackend("ollama", [tool_request, tool_request, tool_request,
                                     tool_request, tool_request])
    agent = _agent_with([backend], max_calls=2, max_rounds=4)
    loop = AgenticLLM(agent.client, tools={"t": lambda a: {"ok": 1}},
                      max_rounds=4, max_calls=2)

    out = loop.run([ChatMessage("user", "x")])

    assert out["ok"] is False
    assert out["calls"] <= 2
    assert backend.calls == 2


def test_agentic_tool_failure_becomes_data_not_an_exception():
    tool_request = json.dumps({"tool": "boom", "arguments": {}})
    backend = FakeBackend("ollama", [tool_request, _result_text(_RISK_OK)])

    def boom(_args):
        raise RuntimeError("vendor down")

    loop = AgenticLLM(LLMClient([backend]), tools={"boom": boom})
    out = loop.run([ChatMessage("user", "x")],
                   validate=lambda p: p if isinstance(p, dict) else None)

    assert out["ok"] is True
    assert out["tool_trace"][0]["ok"] is False


def test_agentic_never_accepts_a_payload_that_fails_the_engine_schema():
    backend = FakeBackend("ollama", [_result_text({"not": "the schema"})] * 3)
    agent = _agent_with([backend])

    result = agent.advise("risk", "text", tools={"x": lambda a: {}})

    assert result["ok"] is False


# ---------------------------------------------------------------------------
# Regression: the direction branch bug (2026-10-23)
# ---------------------------------------------------------------------------
def test_every_minus_one_key_is_clamped_toward_the_baseline():
    """A -1 key must GET SMALLER, never inherit the agent's larger proposal.

    The original implementation tested ``direction >= 0``, so every safety
    ceiling behaved like a floor. This walks all -1 keys explicitly.
    """
    cases = [
        ("risk", "max_allowed_slippage_pct", 0.04, 0.002, 0.002),
        ("risk", "force_trailing_stop_pct", 2.0, 5.0, 2.0),
        ("portfolio", "max_tech_sector_exposure", 0.9, 0.2, 0.2),
        ("economic", "equity_regime_multiplier", 1.4, 0.5, 0.5),
        ("valuation", "growth_rate_ceiling", 0.05, 0.01, 0.01),
        ("valuation", "margin_safety_adjustment", 0.95, 0.5, 0.5),
    ]
    for engine, key, proposed, baseline_value, expected in cases:
        out = _escalate(engine, {key: proposed}, baseline={key: baseline_value})
        assert out[key] == expected, (engine, key, out[key])


def test_every_plus_one_key_is_clamped_toward_the_baseline():
    """A +1 key must GET LARGER when that is the safer direction."""
    out = _escalate("portfolio", {"min_cash_buffer_pct": 0.05},
                    baseline={"min_cash_buffer_pct": 0.20})
    assert out["min_cash_buffer_pct"] == 0.20


def test_agentic_path_cannot_clear_a_veto_end_to_end():
    """Full path: tool-using model returns a cleared veto -> still vetoed."""
    cleared = dict(_RISK_OK, risk_veto_active=False)
    backend = FakeBackend("ollama", [_result_text(cleared)])
    agent = _agent_with([backend])

    result = agent.advise("risk", "text",
                          baseline={"risk_veto_active": True},
                          tools={"x": lambda a: {}})

    assert result["ok"] is True
    assert result["advice"]["risk_veto_active"] is True


# ---------------------------------------------------------------------------
# Remote Ollama (a laptop or another VPS) and the shared call budget
# ---------------------------------------------------------------------------
def test_ollama_host_from_env_is_used_verbatim_for_a_remote_box(monkeypatch):
    """The remote case: OLLAMA_HOST alone must be enough to point the agent."""
    monkeypatch.setenv("OLLAMA_HOST", "http://100.64.1.7:11434/")
    backend = OllamaBackend()

    assert backend.host == "http://100.64.1.7:11434", "trailing / must be trimmed"
    assert backend.endpoint == "http://100.64.1.7:11434/v1/chat/completions"
    assert backend.available(), "a configured remote host counts as available"


def test_agent_chain_uses_the_remote_ollama_host(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://100.64.1.7:11434")
    agent = FrontlineAgent({"enabled": True, "llm": {
        "ollama": {"enabled": True, "model": "qwen3:8b"},
        "openrouter": {"enabled": False},
    }})

    status = agent.status()["chain"]
    assert status["chain"] == ["ollama"]
    assert status["backends"][0]["host"] == "http://100.64.1.7:11434"


def test_agentic_call_budget_is_shared_across_engines_in_one_run():
    """A pipeline advises many engines; the budget must not multiply per call.

    AUDIT (2026-10-23): the counter lived on AgenticLLM, and FrontlineAgent
    builds a new one per advise(), so 8 engines x max_calls=6 = 48 calls -
    enough to spend a 50/day OpenRouter quota in one pipeline run.
    """
    tool_request = json.dumps({"tool": "t", "arguments": {}})
    backend = FakeBackend("ollama", [tool_request] * 20)
    agent = _agent_with([backend], max_calls=2, max_rounds=2)
    agent.client.begin_session("pipeline-run-1")
    try:
        for engine_text, engine in (("a", "risk"), ("b", "market"), ("c", "economic")):
            agent.advise(engine, engine_text, tools={"t": lambda a: {"ok": 1}})
        assert backend.calls == 2, f"budget leaked across calls: {backend.calls} calls"
    finally:
        agent.client.end_session()


def test_without_a_session_each_advise_call_gets_its_own_budget():
    """A one-off call must stay cheap, not inherit an exhausted global budget."""
    backend = FakeBackend("ollama", [json.dumps({"tool": "t", "arguments": {}})] * 6)
    agent = _agent_with([backend], max_calls=1, max_rounds=1)

    agent.advise("risk", "a", tools={"t": lambda a: {}})
    agent.advise("market", "b", tools={"t": lambda a: {}})

    assert backend.calls == 2
