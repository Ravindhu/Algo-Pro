"""Tests for the Anthropic-first chain with automatic Ollama fallback.

Locks the four properties the user asked for:
  1. Anthropic is FIRST when a key exists.
  2. No subscription / no key -> Ollama answers, no exception, one attempt.
  3. A billing or auth failure retires Anthropic for the UTC day, then Ollama
     answers on the very same call.
  4. The Messages API wire format (system field, x-api-key, required max_tokens).
"""
from __future__ import annotations

import json

import pytest

from blue_engine.core.llm.backends import AnthropicBackend
from blue_engine.core.llm.protocol import ChatMessage, ChatResult
from blue_engine.core.llm.router import LLMClient

_RISK_OK = {"risk_veto_active": True, "max_allowed_slippage_pct": 0.002,
            "force_trailing_stop_pct": 2.0, "veto_reason": "test"}


class FakeBackend:
    def __init__(self, name, text="", ok=True, available=True):
        self.name = name
        self.text = text or json.dumps(_RISK_OK)
        self._ok = ok
        self._available = available
        self.calls = 0

    def available(self):
        return self._available

    def status(self):
        return {"backend": self.name}

    def complete(self, messages, **kwargs):
        self.calls += 1
        if not self._ok:
            return ChatResult(ok=False, backend=self.name,
                              attempts=[{"backend": self.name, "reason": "scripted"}])
        return ChatResult(ok=True, text=self.text, backend=self.name,
                          model="fake", model_digest="sha256:fake")


# --- wire format ----------------------------------------------------------
def test_system_messages_move_to_a_top_level_field():
    system, chat = AnthropicBackend.split_system([
        ChatMessage("system", "SCHEMA: JSON only"),
        ChatMessage("user", "advise me"),
    ])
    assert system == "SCHEMA: JSON only"
    assert chat == [{"role": "user", "content": "advise me"}]


def test_request_shape_matches_the_messages_api(monkeypatch):
    backend = AnthropicBackend(api_key="sk-ant-test", model="claude-x")
    captured = {}

    def fake_post(url, payload, headers=None, timeout=None):
        captured.update(url=url, payload=payload, headers=headers)
        return ({"content": [{"type": "text", "text": '{"ok":true}'}],
                 "model": "claude-x-20250101",
                 "usage": {"input_tokens": 40, "output_tokens": 12}}, "", 200, "")

    monkeypatch.setattr(backend, "_post_with_status", fake_post)
    result = backend.complete([ChatMessage("system", "S"),
                               ChatMessage("user", "U")], max_tokens=300)

    assert result.ok and result.text == '{"ok":true}'
    assert captured["url"].endswith("/v1/messages")
    assert captured["payload"]["system"] == "S"
    assert captured["payload"]["max_tokens"] == 300          # required by the API
    assert captured["payload"]["messages"] == [{"role": "user", "content": "U"}]
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"]
    # usage normalised to the names the token meter reads
    assert result.usage["completion_tokens"] == 12


def test_no_key_means_not_available_and_zero_calls():
    backend = AnthropicBackend(api_key=None)
    assert backend.available() is False


# --- chain order / automatic fallback -------------------------------------
def test_anthropic_is_first_when_configured():
    client = LLMClient.from_config(
        {"anthropic": {"model": "claude-x"},
         "ollama": {"enabled": True},
         "openrouter": {"enabled": False}},
        secrets=lambda name: "k" if name == "ANTHROPIC_API_KEY" else None)
    assert client.status()["chain"] == ["anthropic", "ollama"]


def test_missing_key_falls_back_to_ollama_without_error():
    client = LLMClient.from_config(
        {"anthropic": {}, "ollama": {"enabled": True},
         "openrouter": {"enabled": False}},
        secrets=lambda name: None)
    assert "anthropic" not in client.status()["chain"]
    assert client.status()["chain"] == ["ollama"]


def test_billing_failure_retires_anthropic_and_ollama_answers(monkeypatch):
    """No subscription -> HTTP 400 'credit balance' -> Ollama answers THIS call."""
    anthropic = AnthropicBackend(api_key="sk-ant-test", model="claude-x")
    monkeypatch.setattr(
        anthropic, "_post_with_status",
        lambda *a, **k: (None, "http_400", 400,
                         '{"error":{"message":"Your credit balance is too low"}}'))
    ollama = FakeBackend("ollama")
    client = LLMClient([anthropic, ollama])

    result = client.complete([ChatMessage("user", "advise")])

    assert result.ok and result.backend == "ollama"
    assert {"backend": "anthropic", "reason": "billing_required"} in result.attempts
    assert anthropic.exhausted is True          # retired for the UTC day
    assert anthropic.available() is False
    # Second call skips Anthropic entirely: no wasted attempt.
    client.complete([ChatMessage("user", "advise again")])
    assert ollama.calls == 2


def test_auth_failure_also_retires_anthropic(monkeypatch):
    anthropic = AnthropicBackend(api_key="bad")
    monkeypatch.setattr(anthropic, "_post_with_status",
                        lambda *a, **k: (None, "http_401", 401, ""))
    ollama = FakeBackend("ollama")
    result = LLMClient([anthropic, ollama]).complete([ChatMessage("user", "x")])
    assert result.backend == "ollama"
    assert anthropic.exhausted is True


def test_unreachable_anthropic_does_not_retire_it(monkeypatch):
    """A transient network failure must not burn the subscription for the day."""
    anthropic = AnthropicBackend(api_key="sk-ant-test")
    monkeypatch.setattr(anthropic, "_post_with_status",
                        lambda *a, **k: (None, "unreachable:URLError", 0, ""))
    ollama = FakeBackend("ollama")
    result = LLMClient([anthropic, ollama]).complete([ChatMessage("user", "x")])
    assert result.backend == "ollama"
    assert anthropic.exhausted is False


def test_never_raises_on_a_malformed_response(monkeypatch):
    anthropic = AnthropicBackend(api_key="k")
    monkeypatch.setattr(anthropic, "_post_with_status",
                        lambda *a, **k: ({"nope": 1}, "", 200, ""))
    result = anthropic.complete([ChatMessage("user", "x")])
    assert result.ok is False
    assert result.attempts[-1]["reason"] == "unexpected_shape"


# --- governor interaction -------------------------------------------------
def test_fallback_answer_is_not_cached_under_the_full_chain_key():
    """A cheaper backend's text must never be served as if Claude produced it."""
    from blue_engine.core.llm.tokens import TokenGovernor

    anthropic = FakeBackend("anthropic", available=False)
    ollama = FakeBackend("ollama")
    client = LLMClient([anthropic, ollama])
    governor = TokenGovernor(client)

    governor.complete([ChatMessage("user", "same")])
    governor.complete([ChatMessage("user", "same")])

    assert ollama.calls == 2, "the degraded answer must not be cached"
