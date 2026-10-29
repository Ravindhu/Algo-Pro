"""llm - the single LLM transport layer for the engine (frontline only).

Public surface::

    from blue_engine.core.llm import LLMClient, AgenticLLM, ChatMessage

    client = LLMClient.from_config({"ollama": {"enabled": True}})
    result = client.complete([ChatMessage("user", "...")])

Backend order is Ollama (primary, unlimited) -> OpenRouter (overflow, free
models with a hard daily budget) -> Disabled (deterministic fallback).

This package REPLACES the transport inside ``core/frontline_agent.py``. The
other legacy call sites (``economics/nlp/llm_scorer``, ``sectors``,
``portfolio/views``) still own their own transport; they are candidates to
migrate onto this router, one at a time, with their prompts and validators
left untouched.
"""
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
    "TokenGovernor",
    "TokenMeter",
    "cap_messages",
    "compact_text",
    "estimate_tokens",
    "governor_for",
]
