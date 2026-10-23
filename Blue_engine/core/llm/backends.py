"""llm/backends.py - concrete providers behind the ``LLMBackend`` protocol.

CHAIN (configurable, default)
-----------------------------
    ollama      local, unlimited, no quota, no credential      <- primary
      |
    openrouter  cloud, ``:free`` models, 20 rpm / 50-1000 per day  <- overflow
      |
    disabled    deterministic caller fallback                  <- last resort

WHY OLLAMA IS PRIMARY
---------------------
It has no quota, so it cannot run out mid-session, and it keeps market text on
the box. OpenRouter is the overflow: used only when Ollama is unreachable or
returns an invalid answer, which is exactly what its free-tier limits can
tolerate (see ``docs`` note in ``router.py`` about 50 vs 1000 req/day).

Everything here is stdlib-only (``urllib``) so no new dependency is added to
a component that sits in front of the order path.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

from .protocol import ChatMessage, ChatResult

logger = logging.getLogger(__name__)

_MAX_RESPONSE_BYTES = 64 * 1024


def _messages_payload(messages: Sequence[ChatMessage]) -> List[Dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in messages]


def _extract_content(raw: Any) -> Optional[str]:
    """Pull assistant text out of either an OpenAI or native Ollama response."""
    if not isinstance(raw, dict):
        return None
    choices = raw.get("choices")
    if isinstance(choices, list) and choices:
        try:
            content = choices[0]["message"]["content"]
            return content if isinstance(content, str) else None
        except (KeyError, IndexError, TypeError):
            return None
    message = raw.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    return None


class _HttpBackend:
    """Shared POST helper. Returns ``(raw_json, error_reason)``; never raises."""

    def __init__(self, timeout: float = 20.0):
        self.timeout = float(timeout)

    def _post(self, url: str, payload: Dict[str, Any],
              headers: Optional[Dict[str, str]] = None,
              timeout: Optional[float] = None) -> tuple:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(
                    request, timeout=timeout or self.timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # 429 is the OpenRouter free-tier quota; distinguish it because it
            # must move the chain to the next backend, not retry the same one.
            reason = "rate_limited" if exc.code == 429 else f"http_{exc.code}"
            return None, reason
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return None, f"unreachable:{type(exc).__name__}"
        if len(raw) > _MAX_RESPONSE_BYTES:
            return None, "response_too_large"
        try:
            return json.loads(raw.decode("utf-8")), ""
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "bad_json"


class OllamaBackend(_HttpBackend):
    """Local Ollama server. Primary backend: unlimited and credential-free.

    Auth: Ollama has **no API key**; ``OLLAMA_HOST`` is a URL. This backend and
    every other caller resolve it through the same ``get_secret`` path, which
    ends the old ``os.getenv`` vs ``get_secret`` disagreement where one module
    reported "disabled" while another worked.
    """

    name = "ollama"

    def __init__(self, host: Optional[str] = None, model: Optional[str] = None,
                 timeout: float = 20.0, digest: Optional[str] = None):
        super().__init__(timeout)
        self.host = (host or os.getenv("OLLAMA_HOST")
                     or "http://127.0.0.1:11434").rstrip("/")
        # ``:latest`` is mutable, so a model upgrade would change results with
        # no record. Pin by digest in config once known.
        self.model = model or os.getenv("OLLAMA_MODEL", "qwen3:8b")
        self.digest = digest or ""

    @property
    def endpoint(self) -> str:
        return f"{self.host}/v1/chat/completions"

    def available(self) -> bool:
        return bool(self.host)

    def status(self) -> Dict[str, Any]:
        return {"backend": self.name, "host": self.host, "model": self.model,
                "model_digest": self.digest or "<unpinned>"}

    def complete(self, messages: Sequence[ChatMessage], *,
                 model: Optional[str] = None, temperature: float = 0.0,
                 max_tokens: int = 600, timeout: Optional[float] = None,
                 json_schema: Optional[Dict[str, Any]] = None) -> ChatResult:
        started = time.monotonic()
        resolved = model or self.model
        payload: Dict[str, Any] = {
            "model": resolved,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": _messages_payload(messages),
        }
        # Ollama honours response_format on recent versions; older builds
        # ignore it and the caller's schema validation still applies.
        if json_schema:
            payload["response_format"] = {"type": "json_object"}

        raw, reason = self._post(self.endpoint, payload)
        latency = int((time.monotonic() - started) * 1000)
        if raw is None:
            return ChatResult(ok=False, backend=self.name, model=resolved,
                              attempts=[{"backend": self.name, "reason": reason}],
                              latency_ms=latency)
        content = _extract_content(raw)
        if content is None:
            return ChatResult(ok=False, backend=self.name, model=resolved,
                              attempts=[{"backend": self.name,
                                         "reason": "unexpected_shape"}],
                              latency_ms=latency)
        return ChatResult(ok=True, text=content, backend=self.name,
                          model=resolved, model_digest=self.digest or resolved,
                          latency_ms=latency)


class OpenRouterBackend(_HttpBackend):
    """Cloud overflow. Uses ``:free`` model variants; strict per-day budget.

    OpenRouter free-tier rules (verified 2026-10-23,
    https://openrouter.ai/docs/api-reference/limits):
        * 20 requests/minute on any model id ending in ``:free``
        * 50 requests/day with < 10 credits purchased
        * 1,000 requests/day once >= 10 credits have been purchased
    A 429 therefore means "exhausted" and the router stops calling this backend
    for the rest of the UTC day instead of burning attempts.
    """

    name = "openrouter"
    endpoint = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: Optional[str] = None,
                 model: Optional[str] = None, timeout: float = 20.0,
                 referer: str = "https://openalgo.in/",
                 title: str = "Blue Engine Frontline Advisor"):
        super().__init__(timeout)
        self.api_key = api_key
        self.model = model or os.getenv(
            "OPENROUTER_MODEL", "qwen/qwen-2.5-7b-instruct:free")
        self.referer = referer
        self.title = title
        self._exhausted_on: Optional[str] = None

    def available(self) -> bool:
        return bool(self.api_key) and not self.exhausted

    @property
    def exhausted(self) -> bool:
        """True once a 429 was seen today (UTC)."""
        return self._exhausted_on == time.strftime("%Y-%m-%d", time.gmtime())

    def mark_exhausted(self) -> None:
        self._exhausted_on = time.strftime("%Y-%m-%d", time.gmtime())

    def status(self) -> Dict[str, Any]:
        return {"backend": self.name, "model": self.model,
                "configured": bool(self.api_key),
                "exhausted_today": self.exhausted}

    def complete(self, messages: Sequence[ChatMessage], *,
                 model: Optional[str] = None, temperature: float = 0.0,
                 max_tokens: int = 600, timeout: Optional[float] = None,
                 json_schema: Optional[Dict[str, Any]] = None) -> ChatResult:
        started = time.monotonic()
        resolved = model or self.model
        if not self.api_key:
            return ChatResult(ok=False, backend=self.name, model=resolved,
                              attempts=[{"backend": self.name,
                                         "reason": "no_api_key"}])
        if self.exhausted:
            return ChatResult(ok=False, backend=self.name, model=resolved,
                              attempts=[{"backend": self.name,
                                         "reason": "daily_quota_exhausted"}])

        payload: Dict[str, Any] = {
            "model": resolved,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": _messages_payload(messages),
        }
        if json_schema:
            payload["response_format"] = {"type": "json_object"}

        raw, reason = self._post(
            self.endpoint, payload,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "HTTP-Referer": self.referer,
                     "X-Title": self.title},
            timeout=timeout)
        latency = int((time.monotonic() - started) * 1000)
        if reason == "rate_limited":
            self.mark_exhausted()
        if raw is None:
            return ChatResult(ok=False, backend=self.name, model=resolved,
                              attempts=[{"backend": self.name, "reason": reason}],
                              latency_ms=latency)
        content = _extract_content(raw)
        if content is None:
            return ChatResult(ok=False, backend=self.name, model=resolved,
                              attempts=[{"backend": self.name,
                                         "reason": "unexpected_shape"}],
                              latency_ms=latency)
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        return ChatResult(ok=True, text=content, backend=self.name,
                          model=resolved, model_digest=resolved,
                          usage=usage, latency_ms=latency)


class DisabledBackend(_HttpBackend):
    """Terminal sink: records *why* nothing was available. Never makes a call."""

    name = "disabled"

    def __init__(self, reason: str = "no backend configured"):
        super().__init__(timeout=0.0)
        self.reason = reason

    def available(self) -> bool:
        return True  # always "answers", always with a failure

    def status(self) -> Dict[str, Any]:
        return {"backend": self.name, "reason": self.reason}

    def complete(self, messages: Sequence[ChatMessage], **_: Any) -> ChatResult:
        return ChatResult(ok=False, backend=self.name,
                          attempts=[{"backend": self.name, "reason": self.reason}])


__all__ = ["OllamaBackend", "OpenRouterBackend", "DisabledBackend"]
