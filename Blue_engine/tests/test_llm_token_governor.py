"""Tests for token governance: estimation, compaction, cache, metering.

Safety contract under test: governance may only make prompts SMALLER and calls
CHEAPER. It must never change the answer's validation, never drop a system
message, and never raise.
"""
from __future__ import annotations

from blue_engine.core.llm.protocol import ChatMessage, ChatResult
from blue_engine.core.llm.tokens import (
    TokenGovernor,
    cap_messages,
    compact_text,
    estimate_message_tokens,
    estimate_tokens,
)


class FakeClient:
    def __init__(self, text='{"ok": true}', backend="ollama"):
        self.text = text
        self.backend = backend
        self.calls = 0
        self.last_messages = []
        self.session_id = None

    def complete(self, messages, **kwargs):
        self.calls += 1
        self.last_messages = list(messages)
        return ChatResult(ok=True, text=self.text, backend=self.backend,
                          model="m", model_digest="sha256:x",
                          usage={"completion_tokens": 12})


# --- estimation -----------------------------------------------------------
def test_estimate_is_monotonic_and_zero_safe():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 360) > estimate_tokens("a" * 36)
    assert estimate_message_tokens([ChatMessage("user", "hi")]) > 0


# --- compaction -----------------------------------------------------------
def test_compaction_marks_what_it_cut():
    text = "HEAD\n" + ("x" * 5000) + "\nTAIL-END"
    out, cut = compact_text(text, 1000)
    assert cut > 0 and len(out) <= 1000
    assert "elided" in out
    assert out.startswith("HEAD") and out.endswith("TAIL-END")


def test_short_text_is_untouched():
    out, cut = compact_text("short", 1000)
    assert (out, cut) == ("short", 0)


def test_cap_never_drops_the_system_message():
    messages = [ChatMessage("system", "SCHEMA: return JSON only"),
                ChatMessage("user", "y" * 40000)]
    out, info = cap_messages(messages, max_prompt_tokens=1024, max_tokens_out=600)
    assert info["compacted"] is True
    assert out[0].role == "system"
    assert out[0].content == "SCHEMA: return JSON only"
    assert info["tokens_after"] < info["tokens_before"]


def test_cap_is_a_noop_when_it_already_fits():
    messages = [ChatMessage("user", "tiny")]
    out, info = cap_messages(messages, max_prompt_tokens=4096, max_tokens_out=600)
    assert info["compacted"] is False


# --- governor -------------------------------------------------------------
def test_identical_prompts_hit_the_cache_and_call_once():
    client = FakeClient()
    gov = TokenGovernor(client)
    msgs = [ChatMessage("user", "same prompt")]
    first = gov.complete(msgs)
    second = gov.complete(msgs)
    assert client.calls == 1
    assert second.text == first.text
    assert second.usage.get("cache_hit") is True
    assert gov.status()["cache"]["hits"] == 1


def test_changed_prompt_misses_the_cache():
    client = FakeClient()
    gov = TokenGovernor(client)
    gov.complete([ChatMessage("user", "a")])
    gov.complete([ChatMessage("user", "b")])
    assert client.calls == 2


def test_failed_call_is_never_cached():
    class Failing(FakeClient):
        def complete(self, messages, **kwargs):
            self.calls += 1
            return ChatResult(ok=False, attempts=[{"backend": "ollama",
                                                   "reason": "unreachable"}])

    client = Failing()
    gov = TokenGovernor(client)
    gov.complete([ChatMessage("user", "x")])
    gov.complete([ChatMessage("user", "x")])
    assert client.calls == 2


def test_outbound_messages_are_compacted_before_the_client_sees_them():
    client = FakeClient()
    gov = TokenGovernor(client, max_prompt_tokens=1024, max_tokens_out=600)
    gov.complete([ChatMessage("user", "z" * 40000)])
    sent = estimate_message_tokens(client.last_messages)
    assert sent <= 1024
    assert gov.status()["meter"]["compaction_events"] == 1


def test_disabled_governor_passes_straight_through():
    client = FakeClient()
    gov = TokenGovernor(client, enabled=False)
    gov.complete([ChatMessage("user", "z" * 40000)])
    gov.complete([ChatMessage("user", "z" * 40000)])
    assert client.calls == 2
    assert len(client.last_messages[0].content) == 40000


# --- router integration ---------------------------------------------------
def test_router_governor_is_on_by_default_and_can_be_disabled():
    from blue_engine.core.llm.router import LLMClient

    on = LLMClient.from_config({"ollama": {"enabled": False},
                                "openrouter": {"enabled": False}})
    assert on.governor is not None
    assert "tokens" in on.status()
    off = LLMClient.from_config({"ollama": {"enabled": False},
                                 "openrouter": {"enabled": False},
                                 "tokens": {"enabled": False}})
    assert off.governor is None
