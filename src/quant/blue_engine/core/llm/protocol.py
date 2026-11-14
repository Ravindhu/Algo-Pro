"""llm/protocol.py - the ONE contract every LLM backend must satisfy.

WHY THIS FILE EXISTS
--------------------
The engine had five hand-rolled LLM call sites, each with its own transport,
prompt string, cache and JSON validator:

    core/frontline_agent.py          OpenRouter only
    economics/nlp/llm_scorer.py      OpenAI + Ollama (env-var mismatch)
    sectors/sector_engine.py         OpenRouter -> Ollama
    portfolio/views/llm_note.py      OpenAI / Ollama
    portfolio/views/nlp_views.py     local lexical (no LLM: always lexical)

They disagreed about which endpoint, which model, and how to read credentials.
A backend swap therefore changed behaviour silently. This package replaces the
*transport* only - one protocol, one failover chain, one cache key - and leaves
each caller's prompt and schema-validation exactly where it is (those are the
safety layer, not plumbing).

As of 2026-10-29 all four LLM sites are migrated onto the chain; the fourth
(``nlp_views``) is deliberately lexical and has no transport to migrate.

DESIGN RULES
------------
1. **Frontline only.** Nothing here places, sizes or cancels an order. Output
   is advisory and is re-validated by the caller against its own schema.
2. **Degrade, never halt.** Every failure returns a ``ChatResult`` with
   ``ok=False``. No exception escapes a backend.
3. **Loose coupling.** ``LLMClient`` talks to ``LLMBackend`` objects only, so
   Ollama, OpenRouter, or any future provider (including a LangChain adapter)
   can be swapped without touching callers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class ChatMessage:
    """One OpenAI-format chat message."""

    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class ChatResult:
    """Outcome of one completion. Never raises; inspect ``ok`` first."""

    ok: bool
    text: str = ""
    #: backend id that answered ("ollama", "openrouter", "disabled", ...)
    backend: str = ""
    model: str = ""
    #: model identity for reproducibility. An immutable digest where the
    #: backend can supply one, else the exact resolved model id.
    model_digest: str = ""
    #: backends that were tried and failed, in order, with a short reason
    attempts: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0

    @property
    def method(self) -> str:
        """Compact label for logs / dashboard: ``ok:backend`` or ``fail:reason``."""
        if self.ok:
            return f"ok:{self.backend}"
        reason = self.attempts[-1]["reason"] if self.attempts else "unavailable"
        return f"fail:{reason}"


@runtime_checkable
class LLMBackend(Protocol):
    """A single completion provider (Ollama, OpenRouter, ...).

    Implementations MUST NOT raise from :meth:`complete`: turn every transport,
    auth and parse failure into ``ChatResult(ok=False)`` so the router can
    decide whether to fail over.
    """

    #: Stable identifier used in config and logs (``"ollama"``, ``"openrouter"``).
    name: str

    def available(self) -> bool:
        """Cheap, offline check: is this backend configured at all?"""

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 600,
        timeout: Optional[float] = None,
        json_schema: Optional[Dict[str, Any]] = None,
    ) -> ChatResult:
        """Return one completion. ``json_schema`` requests structured output."""

    def status(self) -> Dict[str, Any]:
        """Redacted, loggable description. Never include credential values."""


__all__ = ["ChatMessage", "ChatResult", "LLMBackend"]
