"""llm/tokens.py - token accounting, prompt compaction and the call cache.

WHY THIS FILE EXISTS
--------------------
Every agentic call costs three things: money (OpenRouter), latency (Ollama on a
small box), and context (a hard window that silently truncates the prompt). The
frontline agent is called once per engine per pipeline run - up to 8 engines -
so an unbounded prompt is paid for 8 times, every run, even when nothing
changed.

WHAT IT DOES (and strictly does not do)
---------------------------------------
1. **Estimates tokens** with no tokenizer dependency (~3.6 chars/token plus a
   per-message overhead). Used only for *budgets and warnings*, never for
   trading decisions. A wrong estimate costs tokens, never capital.
2. **Compacts prompts** deterministically and detectably: an oversized user
   message is cut at a line boundary, in the middle, and an explicit
   ``[elided N chars]`` marker is left behind so the model never sees a silently
   truncated sentence and believes it finished.
3. **Caches identical prompts** for a TTL, keyed by a SHA-256 of the exact
   system + user content, the model and the backend chain. Identical input must
   produce identical output for this cache to be sound - which is why the agent
   already calls with ``temperature=0``. A cache hit returns the SAME text the
   model produced, so schema validation downstream is unchanged.
4. **Measures** what a run actually spent, per backend, for the dashboard.

INVARIANTS
----------
* Frontline only: nothing here places, sizes or cancels an order.
* Degrade, never halt: every public function is total; a failure to account is
  reported in ``warnings``, never raised.
* One entry point: :class:`TokenGovernor`, wrapped around ``LLMClient.complete``
  so no caller has to remember to use it.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Characters per token for English + JSON. Deliberately conservative: slightly
#: over-estimating triggers compaction a little early, which is cheap; an
#: under-estimate lets the provider truncate the middle of a schema instruction.
CHARS_PER_TOKEN = 3.6

#: Fixed per-message framing overhead (role tags, separators) in tokens.
MESSAGE_OVERHEAD_TOKENS = 4


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------
def estimate_tokens(text: str) -> int:
    """Rough token count for one string. Never raises."""
    if not text:
        return 0
    return int(len(str(text)) / CHARS_PER_TOKEN) + 1


def estimate_message_tokens(messages: Sequence[Any]) -> int:
    """Token estimate for a whole chat (``ChatMessage`` or ``(role, content)``)."""
    total = 0
    for message in messages or []:
        content = getattr(message, "content", None)
        if content is None and isinstance(message, (tuple, list)) and len(message) > 1:
            content = message[1]
        total += estimate_tokens(content or "") + MESSAGE_OVERHEAD_TOKENS
    return total


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------
def compact_text(text: str, max_chars: int, *, keep_tail_ratio: float = 0.4,
                 label: str = "input") -> Tuple[str, int]:
    """Cut ``text`` to ``max_chars`` with an explicit elision marker.

    Head and tail are preserved because a market snapshot is usually ordered
    ("as of <date>" at the top, the newest readings at the bottom); the middle
    carries the bulk. Returns ``(compacted, chars_elided)``.
    """
    text = "" if text is None else str(text)
    limit = max(0, int(max_chars))
    if len(text) <= limit:
        return text, 0
    marker = f"\n[... {label} elided {{n}} chars ...]\n"
    # Reserve room for the marker itself before splitting the budget.
    budget = max(0, limit - len(marker.format(n=len(text))))
    if budget <= 0:
        return text[:limit], len(text) - limit
    head_chars = int(budget * (1.0 - keep_tail_ratio))
    tail_chars = budget - head_chars
    tail = text[-tail_chars:] if tail_chars else ""
    # Snap the head to a line boundary when one is close, so the model does not
    # receive half a number.
    newline = text.rfind("\n", 0, head_chars)
    if newline > head_chars * 0.7:
        head_chars = newline
    head = text[:head_chars]
    return head + marker.format(n=len(text) - head_chars - len(tail)) + tail, \
        len(text) - head_chars - len(tail)


def cap_messages(messages: Sequence[Any], *, max_prompt_tokens: int,
                 max_tokens_out: int, reserve_tokens: int = 0,
                 ) -> Tuple[List[Any], Dict[str, Any]]:
    """Fit a chat into ``max_prompt_tokens`` (+output) by shrinking user turns.

    System messages are NEVER dropped - they carry the JSON schema and the
    "untrusted data" rule. Only user/assistant content is compacted, longest
    first, until the estimate fits.
    """
    from .protocol import ChatMessage  # local import: avoids a cycle at import

    out: List[Any] = [
        m if isinstance(m, ChatMessage) else ChatMessage(str(m[0]), str(m[1]))
        for m in messages
    ]
    available = max(256, int(max_prompt_tokens) - int(max_tokens_out)
                    - int(reserve_tokens))
    before = estimate_message_tokens(out)
    elided = 0
    if before > available:
        # Overhead of the system turns is fixed; give the rest to the user turns.
        system_cost = sum(estimate_message_tokens([m]) for m in out
                          if m.role == "system")
        user_budget_tokens = max(64, available - system_cost)
        user_idx = [i for i, m in enumerate(out) if m.role != "system"]
        per_message = user_budget_tokens // max(1, len(user_idx))
        for i in user_idx:
            new_content, cut = compact_text(out[i].content,
                                            int(per_message * CHARS_PER_TOKEN))
            if cut:
                out[i] = ChatMessage(out[i].role, new_content)
                elided += cut
    after = estimate_message_tokens(out)
    return out, {
        "tokens_before": before,
        "tokens_after": after,
        "chars_elided": elided,
        "compacted": after < before,
        "max_prompt_tokens": int(max_prompt_tokens),
    }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def cache_key(messages: Sequence[Any], *, model: str = "", chain: str = "",
              json_schema: Optional[Dict[str, Any]] = None,
              salt: str = "") -> str:
    """Stable SHA-256 over everything that can change the answer."""
    digest = hashlib.sha256()
    for message in messages or []:
        digest.update(str(getattr(message, "role", "") or "").encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(str(getattr(message, "content", "") or "").encode("utf-8"))
        digest.update(b"\x1e")
    digest.update(f"|model={model}|chain={chain}|salt={salt}".encode("utf-8"))
    if json_schema:
        digest.update(b"|schema=")
        digest.update(repr(sorted(json_schema.items())).encode("utf-8"))
    return digest.hexdigest()


@dataclass
class CacheEntry:
    text: str
    backend: str
    model: str
    model_digest: str
    created_at: float
    usage: Dict[str, Any] = field(default_factory=dict)


class PromptCache:
    """Small LRU + TTL cache of completions. Thread-safe, bounded."""

    def __init__(self, max_entries: int = 256, ttl_seconds: float = 900.0,
                 enabled: bool = True):
        self.max_entries = max(1, int(max_entries))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.enabled = bool(enabled) and self.ttl_seconds > 0
        self._store: "OrderedDict[str, CacheEntry]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[CacheEntry]:
        if not self.enabled:
            return None
        now = time.time()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            if now - entry.created_at > self.ttl_seconds:
                self._store.pop(key, None)
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return entry

    def put(self, key: str, entry: CacheEntry) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._store[key] = entry
            self._store.move_to_end(key)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {"entries": len(self._store), "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
                "ttl_seconds": self.ttl_seconds, "enabled": self.enabled}


# ---------------------------------------------------------------------------
# Meter
# ---------------------------------------------------------------------------
@dataclass
class TokenMeter:
    """Per-session usage accounting, surfaced to the dashboard."""

    calls: int = 0
    cache_hits: int = 0
    prompt_tokens_saved: int = 0
    prompt_tokens_estimated: int = 0
    completion_tokens_reported: int = 0
    by_backend: Dict[str, int] = field(default_factory=dict)
    compaction_events: int = 0

    def record_call(self, backend: str, prompt_tokens: int,
                    usage: Optional[Dict[str, Any]] = None) -> None:
        self.calls += 1
        self.prompt_tokens_estimated += max(0, int(prompt_tokens))
        self.by_backend[backend or "unknown"] = \
            self.by_backend.get(backend or "unknown", 0) + 1
        usage = usage or {}
        completion = usage.get("completion_tokens") or usage.get("output_tokens")
        try:
            self.completion_tokens_reported += int(completion or 0)
        except (TypeError, ValueError):
            pass

    def record_cache_hit(self, prompt_tokens: int) -> None:
        self.cache_hits += 1
        self.prompt_tokens_saved += max(0, int(prompt_tokens))

    def record_compaction(self) -> None:
        self.compaction_events += 1

    def as_dict(self) -> Dict[str, Any]:
        return {"calls": self.calls, "cache_hits": self.cache_hits,
                "prompt_tokens_estimated": self.prompt_tokens_estimated,
                "prompt_tokens_saved": self.prompt_tokens_saved,
                "completion_tokens_reported": self.completion_tokens_reported,
                "compaction_events": self.compaction_events,
                "by_backend": dict(self.by_backend)}


# ---------------------------------------------------------------------------
# Governor
# ---------------------------------------------------------------------------
class TokenGovernor:
    """Compaction + cache + metering around any ``client.complete`` callable.

    ``client`` only needs a ``complete(messages, **kwargs) -> ChatResult``
    method, so this wraps ``LLMClient`` without importing it.
    """

    def __getattr__(self, name: str) -> Any:
        """Refuse a self-referential wrap instead of recursing at call time.

        ``governor_for()`` returns a governor unchanged, so a caller that then
        does ``client.governor = wrapped`` on the SAME object makes
        ``complete() -> governor.complete() -> client.complete()`` an infinite
        chain (observed as ``RecursionError`` in the paper-shift pipeline).
        """
        if name == "client":
            raise AttributeError(name)
        raise AttributeError(name)

    def __init__(self, client: Any, *,
                 max_prompt_tokens: int = 3072,
                 max_tokens_out: int = 600,
                 cache_ttl_seconds: float = 900.0,
                 cache_entries: int = 256,
                 enabled: bool = True,
                 monotonic: Any = time.time):
        if client is self:
            raise ValueError("TokenGovernor cannot wrap itself")
        self.client = client
        self.max_prompt_tokens = max(512, int(max_prompt_tokens))
        self.max_tokens_out = max(64, int(max_tokens_out))
        self.enabled = bool(enabled)
        self.cache = PromptCache(max_entries=cache_entries,
                                 ttl_seconds=cache_ttl_seconds,
                                 enabled=self.enabled)
        self.meter = TokenMeter()
        self._clock = monotonic or time.time

    # -- config -----------------------------------------------------------
    @classmethod
    def from_config(cls, client: Any,
                    config: Optional[Dict[str, Any]] = None) -> "TokenGovernor":
        cfg = dict(config or {})
        return cls(
            client,
            max_prompt_tokens=int(cfg.get("max_prompt_tokens", 3072)),
            max_tokens_out=int(cfg.get("max_tokens_out", 600)),
            cache_ttl_seconds=float(cfg.get("cache_ttl_seconds", 900)),
            cache_entries=int(cfg.get("cache_entries", 256)),
            enabled=bool(cfg.get("enabled", True)),
        )

    # -- main entry point -------------------------------------------------
    def complete(self, messages: Sequence[Any], *,
                 model: Optional[str] = None, temperature: float = 0.0,
                 max_tokens: Optional[int] = None,
                 timeout: Optional[float] = None,
                 json_schema: Optional[Dict[str, Any]] = None,
                 use_cache: Optional[bool] = None,
                 _hops: int = 0) -> Any:
        """Governed completion. Returns the backend's ``ChatResult`` unchanged.

        A cache hit returns exactly the text a live call produced, with
        ``backend`` preserved and a ``cache_hit`` flag in ``usage`` so callers
        and the dashboard can see it. On a hit the schema validator still runs
        downstream: nothing about safety changes.
        """
        from .protocol import ChatResult  # local import: avoids a cycle

        out_tokens = int(max_tokens or self.max_tokens_out)
        prepared, info = cap_messages(
            messages, max_prompt_tokens=self.max_prompt_tokens,
            max_tokens_out=out_tokens) if self.enabled else (list(messages), {})
        if info.get("compacted"):
            self.meter.record_compaction()

        allowed = self.enabled if use_cache is None else (self.enabled and use_cache)
        chain = ",".join(getattr(b, "name", "?")
                         for b in getattr(self.client, "backends", []) or [])
        key = cache_key(prepared, model=model or "", chain=chain,
                        json_schema=json_schema,
                        salt=str(getattr(self.client, "session_id", "") or ""))
        prompt_tokens = estimate_message_tokens(prepared)

        if allowed:
            entry = self.cache.get(key)
            if entry is not None:
                self.meter.record_cache_hit(prompt_tokens)
                return ChatResult(
                    ok=True, text=entry.text, backend=entry.backend,
                    model=entry.model, model_digest=entry.model_digest,
                    usage={**entry.usage, "cache_hit": True,
                           "prompt_tokens_estimated": prompt_tokens},
                    latency_ms=0)

        # Hop guard: two governed clients that reference each other would
        # otherwise recurse until the interpreter's stack limit.
        if _hops >= 2:
            return ChatResult(ok=False, attempts=[
                {"backend": "governor",
                 "reason": "governor chain loop detected"}])
        if isinstance(self.client, TokenGovernor):
            result = self.client.complete(
                prepared, model=model, temperature=temperature,
                max_tokens=out_tokens, timeout=timeout,
                json_schema=json_schema, _hops=_hops + 1)
        else:
            result = self.client.complete(
                prepared, model=model, temperature=temperature,
                max_tokens=out_tokens, timeout=timeout,
                json_schema=json_schema)
        self.meter.record_call(getattr(result, "backend", "") or "",
                               prompt_tokens, getattr(result, "usage", None))
        # The cache key includes the chain, not the backend that answered. If
        # Anthropic is retired mid-session and Ollama answers the SAME prompt,
        # storing it under the existing key would later serve the cheaper model's
        # text back to a run that expects the subscribed one. Store only when the
        # full chain was actually intact.
        chain_intact = all(getattr(b, "available", lambda: True)()
                           for b in getattr(self.client, "backends", []) or [])
        if allowed and chain_intact and getattr(result, "ok", False) \
                and getattr(result, "text", ""):
            self.cache.put(key, CacheEntry(
                text=result.text, backend=result.backend, model=result.model,
                model_digest=result.model_digest,
                created_at=self._clock(), usage=dict(result.usage or {})))
        # Report compaction on the result so a caller can log it.
        try:
            if info:
                result.usage = {**(result.usage or {}), "tokens": info}
        except Exception:  # noqa: BLE001 - accounting must never break a call
            pass
        return result

    # -- introspection ----------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        """Read-only view for the dashboard. Never touches the network."""
        return self.status()

    def status(self) -> Dict[str, Any]:
        return {"enabled": self.enabled,
                "max_prompt_tokens": self.max_prompt_tokens,
                "max_tokens_out": self.max_tokens_out,
                "cache": self.cache.stats(),
                "meter": self.meter.as_dict()}

    def __getattr__(self, name: str) -> Any:
        """Proxy everything else (``backends``, ``status``, ...) to the client."""
        return getattr(self.client, name)


def governor_for(client: Any, config: Optional[Dict[str, Any]]) -> TokenGovernor:
    """Wrap ``client`` unless it is already governed (idempotent)."""
    if isinstance(client, TokenGovernor):
        return client
    return TokenGovernor.from_config(client, config)


__all__ = [
    "CHARS_PER_TOKEN",
    "MESSAGE_OVERHEAD_TOKENS",
    "CacheEntry",
    "PromptCache",
    "TokenGovernor",
    "TokenMeter",
    "cache_key",
    "cap_messages",
    "compact_text",
    "estimate_message_tokens",
    "estimate_tokens",
    "governor_for",
]
