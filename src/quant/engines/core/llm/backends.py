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


class AnthropicBackend(_HttpBackend):
    """Anthropic Claude via the Messages API. Optional FIRST backend.

    Chain position (configured, not hard-coded by this class):

        anthropic -> ollama -> openrouter -> disabled

    The user requirement is: try the subscription-backed Claude key first, and
    *automatically* fall back to local Ollama when there is no subscription, no
    key, no credit, or the API is unreachable. That fallback is not special
    code - the router already skips any backend whose :meth:`available` is
    False and records the reason. This class only has to be honest about
    availability and about which failures are permanent.

    WIRE FORMAT DIFFERS FROM OPENAI (verified against
    https://docs.anthropic.com/en/api/messages, 2026-10-29):
        * ``system`` is a TOP-LEVEL string, not a message with role=system;
        * ``max_tokens`` is REQUIRED;
        * auth is the ``x-api-key`` header plus ``anthropic-version``;
        * the answer is ``content: [{type: "text", text: ...}, ...]``.

    FAILURES THAT RETIRE THE BACKEND FOR THE UTC DAY (no point retrying):
        * 401 - invalid/revoked key
        * 402/403 - no credit or no access to the model
        * 429 - rate or usage cap reached
    Usage-limit errors from a free/zero-credit key arrive as ``invalid_request``
    text rather than a status code, so 400 bodies are inspected for it.
    """

    name = "anthropic"
    endpoint = "https://api.anthropic.com/v1/messages"
    api_version = "2023-06-01"

    _PERMANENT_CODES = frozenset({401, 402, 403, 429})
    _BILLING_HINTS = ("credit", "billing", "quota", "usage limit",
                      "insufficient", "not subscribed", "subscription")

    def __init__(self, api_key: Optional[str] = None,
                 model: Optional[str] = None, timeout: float = 20.0):
        super().__init__(timeout)
        self.api_key = api_key
        self.model = model or os.getenv(
            "ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
        self._exhausted_on: Optional[str] = None

    # -- availability -----------------------------------------------------
    def available(self) -> bool:
        return bool(self.api_key) and not self.exhausted

    @property
    def exhausted(self) -> bool:
        """True once a billing/auth limit was seen today (UTC)."""
        return self._exhausted_on == time.strftime("%Y-%m-%d", time.gmtime())

    def mark_exhausted(self, reason: str = "limit") -> None:
        self._exhausted_on = time.strftime("%Y-%m-%d", time.gmtime())
        logger.info("anthropic retired for the UTC day (%s)", reason)

    def status(self) -> Dict[str, Any]:
        return {"backend": self.name, "model": self.model,
                "configured": bool(self.api_key),
                "exhausted_today": self.exhausted}

    # -- request shaping --------------------------------------------------
    @staticmethod
    def split_system(messages: Sequence[ChatMessage]) -> tuple:
        """Move system turns out of the message list (Anthropic wants a field)."""
        system_parts: List[str] = []
        chat: List[Dict[str, str]] = []
        for message in messages:
            role = getattr(message, "role", "user")
            content = str(getattr(message, "content", "") or "")
            if role == "system":
                system_parts.append(content)
                continue
            chat.append({"role": "assistant" if role == "assistant" else "user",
                         "content": content})
        if not chat:
            chat = [{"role": "user", "content": ""}]
        return "\n\n".join(p for p in system_parts if p), chat

    def _post_with_status(self, url: str, payload: Dict[str, Any],
                          headers: Dict[str, str],
                          timeout: Optional[float] = None) -> tuple:
        """Like ``_post`` but keeps the HTTP code so callers can classify it."""
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(
                    request, timeout=timeout or self.timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(2000).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - a body read must never raise
                detail = ""
            reason = {429: "rate_limited"}.get(exc.code, f"http_{exc.code}")
            return None, reason, exc.code, detail
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return None, f"unreachable:{type(exc).__name__}", 0, ""
        if len(raw) > _MAX_RESPONSE_BYTES:
            return None, "response_too_large", 0, ""
        try:
            return json.loads(raw.decode("utf-8")), "", 200, ""
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "bad_json", 0, ""

    # -- completion -------------------------------------------------------
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

        system_text, chat = self.split_system(messages)
        payload: Dict[str, Any] = {
            "model": resolved,
            "max_tokens": max(1, int(max_tokens)),  # required by the API
            "temperature": max(0.0, float(temperature)),
            "messages": chat,
        }
        if system_text:
            payload["system"] = system_text
        # NOTE: the Messages API has no response_format. Structured output is
        # carried by the system instruction and enforced by the caller's
        # schema validator, exactly as for Ollama.

        raw, reason, code, detail = self._post_with_status(
            self.endpoint, payload,
            headers={"x-api-key": self.api_key,
                     "anthropic-version": self.api_version},
            timeout=timeout)
        latency = int((time.monotonic() - started) * 1000)

        if raw is None:
            lowered = (detail or "").lower()
            billing = any(hint in lowered for hint in self._BILLING_HINTS)
            if code in self._PERMANENT_CODES or (code in (400, 404) and billing):
                self.mark_exhausted(reason)
                reason = (reason if code == 429
                          else ("billing_required" if billing else reason))
            return ChatResult(ok=False, backend=self.name, model=resolved,
                              attempts=[{"backend": self.name, "reason": reason}],
                              latency_ms=latency)

        text = _extract_anthropic_text(raw)
        if text is None:
            return ChatResult(ok=False, backend=self.name, model=resolved,
                              attempts=[{"backend": self.name,
                                         "reason": "unexpected_shape"}],
                              latency_ms=latency)
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        # Normalise to the OpenAI names the token meter already reads.
        usage = {**(usage or {})}
        if "input_tokens" in usage:
            usage.setdefault("prompt_tokens", usage["input_tokens"])
        if "output_tokens" in usage:
            usage.setdefault("completion_tokens", usage["output_tokens"])
        return ChatResult(ok=True, text=text, backend=self.name,
                          model=resolved, model_digest=str(
                              raw.get("model") or resolved),
                          usage=usage, latency_ms=latency)


def _extract_anthropic_text(raw: Any) -> Optional[str]:
    """Join the text blocks of a Messages API response. Never raises."""
    if not isinstance(raw, dict):
        return None
    blocks = raw.get("content")
    if not isinstance(blocks, list):
        return None
    parts = [b.get("text") for b in blocks
             if isinstance(b, dict) and b.get("type") == "text"]
    texts = [p for p in parts if isinstance(p, str)]
    return "".join(texts) if texts else None


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


__all__ = ["AnthropicBackend", "OllamaBackend", "OpenRouterBackend",
           "DisabledBackend"]
