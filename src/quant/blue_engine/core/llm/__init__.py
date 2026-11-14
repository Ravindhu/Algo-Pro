"""llm - the single LLM transport layer for the engine (frontline only).

Public surface::

    from blue_engine.core.llm import LLMClient, AgenticLLM, ChatMessage

    client = LLMClient.from_config({"ollama": {"enabled": True}})
    result = client.complete([ChatMessage("user", "...")])

Backend order is Anthropic (subscription key, tried first) -> Ollama (local,
automatic fallback, unlimited) -> OpenRouter (overflow, free models with a hard
daily budget) -> Disabled (deterministic fallback).

MIGRATION STATUS (2026-10-29 COMPLETE)
--------------------------------------
Every LLM caller now uses this router; none owns a transport:

    core/frontline_agent.py          full chain + governor + agentic loop
    sectors/sector_engine.py         SectorAdvisor -> LLMClient (2 members)
    economics/nlp/llm_scorer.py      LLMClient (Ollama -> OpenRouter)
    portfolio/views/llm_note.py      LLMClient (Ollama -> OpenRouter)

The three migrated sites keep their prompts, schema validators and their own
content-hash DISK caches exactly as they were; they build through
``LLMClient.for_named_site`` so the in-memory governor cache stays opt-in.
"""
from .backends import AnthropicBackend, DisabledBackend, OllamaBackend, OpenRouterBackend
from .protocol import ChatMessage, ChatResult, LLMBackend
from .router import AgenticLLM, LLMClient, ToolFunc
from .tokens import (
    TokenGovernor,
    TokenMeter,
    cap_messages,
    compact_text,
    estimate_tokens,
    governor_for,
)

__all__ = [
    "ChatMessage",
    "ChatResult",
    "LLMBackend",
    "LLMClient",
    "AgenticLLM",
    "ToolFunc",
    "AnthropicBackend",
    "OllamaBackend",
    "OpenRouterBackend",
    "DisabledBackend",
    "TokenGovernor",
    "TokenMeter",
    "cap_messages",
    "compact_text",
    "estimate_tokens",
    "governor_for",
]
