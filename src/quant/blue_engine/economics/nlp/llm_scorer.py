"""
nlp/llm_scorer.py - opt-in GPT / local-LLM structured scoring (M7).

Turns central-bank text (RBI MPC statement/minutes, budget speech) into a
**strict JSON** object the engine can consume. Design constraints a desk
demands:

  1. **Opt-in** - nothing here runs unless a key (``OPENAI_API_KEY``) or a local
     endpoint (``OLLAMA_HOST``) is configured. No key -> ``method='disabled'``.
  2. **Strict JSON out** - the prompt forbids prose; we parse and validate the
     schema and reject anything that does not conform.
  3. **Cached by content hash** - the same document is never scored (or paid
     for) twice; results are reproducible from disk.
  4. **No PII / no positions** in the prompt - only public text. The key is read
     via the shared secrets resolver, never hardcoded, never logged.

The lexical fallback in :mod:`.sentiment` is always available; this layer is an
*enhancement*, so its absence never blocks the pipeline.

TRANSPORT (2026-10-29)
----------------------
This module no longer owns HTTP. It builds a :class:`LLMClient` over the shared
backend chain, so it inherits the same order and the same quota rules as the
frontline agent (Anthropic -> Ollama -> OpenRouter). The previous version
*preferred* Ollama whenever ``OLLAMA_HOST`` was set, which is backwards for a
paid desk; the chain resolves it once, in one place.

Deliberately unchanged: the on-disk content-hash cache, the strict schema, and
the ``method`` vocabulary of the return value. Callers depend on all three.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from ...core.llm.backends import OllamaBackend, OpenRouterBackend
from ...core.llm.protocol import ChatMessage
from ...core.llm.router import LLMClient
# AUDIT 2026-10-29: this was ``...providers.secrets`` -> blue_engine.providers,
# which does not exist (secrets lives under economics/). Importing this module
# therefore raised ModuleNotFoundError; the economic engine imports it lazily,
# so the failure only appeared on the first call.
from ..providers.secrets import get_secret, has_secret, redact

# Default cache: <repo>/data/llm_cache/  (keyed by sha256 of the prompt text).
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_DIR = _REPO_ROOT / "data" / "llm_cache"

SCHEMA_KEYS = ("hawkish", "inflation_focus", "growth_focus", "guidance",
               "confidence")

# AUDIT 2026-10-29: the JSON exemplar's literal braces are doubled (``{{"``)
# because this string is consumed by ``str.format``. With single braces the
# ``.score()`` call raised KeyError('"hawkish"') on EVERY document, so the LLM
# scorer never actually ran - the module only ever returned the lexical
# fallback. Keep them doubled if you edit the exemplar.
_PROMPT_TEMPLATE = (
    "You are a macro analyst. Read the central-bank text and return ONLY a "
    "JSON object with exactly these keys: "
    '{{"hawkish": float in [-1,1], "inflation_focus": float in [0,1], '
    '"growth_focus": float in [0,1], "guidance": one of '
    '["hawkish","neutral","dovish"], "confidence": float in [0,1]}}. '
    "hawkish > 0 means more hawkish (rate-tightening bias). No prose, JSON "
    "only.\n\nTEXT:\n{text}"
)


class _ProbeOpenRouter(OpenRouterBackend):
    """OpenRouter chain member that sends every request through a probe.

    Kept so a caller injecting ``http_post`` observes the identical
    request/response shape it did before the transport migration.
    """

    def __init__(self, http_post: Callable[..., Any], model: str, timeout: float):
        super().__init__(api_key="probe", model=model, timeout=timeout)
        self._probe = http_post

    def available(self) -> bool:
        return True  # the probe decides; it is the test/caller's transport

    def _post(self, url: str, payload: Dict[str, Any],
              headers: Optional[Dict[str, str]] = None,
              timeout: Optional[float] = None) -> Tuple[Any, str]:
        try:
            raw = self._probe(url, json=payload, headers=headers or {},
                              timeout=timeout or self.timeout)
        except Exception:  # noqa: BLE001 - probes fail like transports do
            return None, "probe_failed"
        if hasattr(raw, "json"):
            try:
                raw = raw.json()
            except Exception:  # noqa: BLE001
                return None, "bad_json"
        if not isinstance(raw, dict):
            return None, "bad_json"
        return raw, ""


def _cache_file(text: str, cache_dir: Path) -> Path:
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    return cache_dir / f"llm_{h}.json"


def _validate(obj: Any) -> Optional[Dict[str, Any]]:
    """Reject anything that is not the exact expected schema."""
    if not isinstance(obj, dict):
        return None
    if not all(k in obj for k in SCHEMA_KEYS):
        return None
    try:
        out = {
            "hawkish": max(-1.0, min(1.0, float(obj["hawkish"]))),
            "inflation_focus": max(0.0, min(1.0, float(obj["inflation_focus"]))),
            "growth_focus": max(0.0, min(1.0, float(obj["growth_focus"]))),
            "guidance": str(obj["guidance"]),
            "confidence": max(0.0, min(1.0, float(obj["confidence"]))),
        }
    except (TypeError, ValueError):
        return None
    if out["guidance"] not in ("hawkish", "neutral", "dovish"):
        return None
    return out


class LLMScorer:
    """Structured scorer over an OpenAI-compatible chat endpoint.

    Works with OpenAI (``OPENAI_API_KEY``) or a local Ollama server
    (``OLLAMA_HOST``), which is the zero-cost option for a desk.
    """

    name = "llm_scorer"

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 http_post: Optional[Callable[..., Any]] = None,
                 cache_dir: Optional[Path] = None,
                 client: Optional[LLMClient] = None):
        self.config = dict(config or {})
        self._log_fn = log
        self._http_post = http_post
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR

        self.api_key = get_secret("OPENAI_API_KEY")
        self.ollama_host = get_secret("OLLAMA_HOST")
        self.model = self.config.get("model", "gpt-4o-mini")
        self.timeout = float(self.config.get("timeout", 30.0))
        self.client = client or self._build_client()

    def log(self, msg: str) -> None:
        if self._log_fn:
            self._log_fn(f"[{self.name}] {msg}")

    # ---- availability --------------------------------------------------- #
    def enabled(self) -> bool:
        return any(b.available() for b in self.client.backends)

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled(),
            "openai_key": redact("OPENAI_API_KEY", self.api_key)
            if self.api_key else "<unset>",
            "ollama_host": self.ollama_host or "<unset>",
            "model": self.model,
            "chain": self.client.status(),
        }

    # ---- transport ------------------------------------------------------ #
    def _build_client(self) -> LLMClient:
        """Ollama -> OpenRouter over the shared chain.

        ``http_post`` compatibility: a caller that injects a transport probe
        gets an OpenRouter-only chain routed through it, so existing probes keep
        observing the same request/response shape.
        """
        if self._http_post is not None:
            backends: Sequence[Any] = [
                _ProbeOpenRouter(self._http_post, self.model, self.timeout)]
        elif self.ollama_host:
            backends = [
                OllamaBackend(
                    host=self.ollama_host,
                    model=str(self.config.get("ollama_model") or "llama3.1"),
                    timeout=self.timeout),
                OpenRouterBackend(
                    api_key=get_secret("OPENROUTER_API_KEY"),
                    model=str(self.config.get("openrouter_model") or self.model),
                    timeout=self.timeout),
            ]
        else:
            backends = []
        return LLMClient.for_named_site(backends, self.config)

    # ---- scoring -------------------------------------------------------- #
    def score(self, text: str, *, use_cache: bool = True) -> Dict[str, Any]:
        """Score one document. NEVER raises; always returns a dict.

        Returns::

            {"method": "llm"|"disabled"|"cached"|"error"|"invalid_response",
             "hawkish": float, ...}|{"method": ..., "error": ...}
        """
        text = (text or "").strip()
        if not text:
            return {"method": "empty"}
        if not self.enabled():
            return {"method": "disabled",
                    "reason": "no LLM backend configured"}

        path = _cache_file(text, self.cache_dir)
        if use_cache and path.is_file():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    obj = json.load(fh)
                if _validate(obj):
                    obj = dict(obj)
                    obj["method"] = "cached"
                    return obj
            except Exception:  # noqa: BLE001
                pass

        result = self.client.complete(
            [ChatMessage("user",
                         _PROMPT_TEMPLATE.format(text=text[:6000]))],
            temperature=0.0,                  # deterministic scoring
            max_tokens=600, timeout=self.timeout,
            json_schema={"type": "object"})
        if not result.ok:
            self.log(f"chain failed: {result.method}")
            return {"method": "error", "error": "no response",
                    "attempts": result.attempts}
        content = result.text

        # The model must return JSON; strip accidental code fences defensively.
        content = str(content).strip().removeprefix("```json").removeprefix(
            "```").removesuffix("```").strip()
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            return {"method": "invalid_response", "raw": content[:200]}

        validated = _validate(obj)
        if validated is None:
            return {"method": "invalid_response", "raw": content[:200]}

        validated["method"] = "llm"
        if use_cache:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding="utf-8") as fh:
                    json.dump(validated, fh, indent=2)
            except Exception:  # noqa: BLE001
                pass
        return validated


def available() -> bool:
    """True when any configured backend could answer, or a cloud key exists."""
    return bool(has_secret("OPENAI_API_KEY") or has_secret("OPENROUTER_API_KEY")
                or has_secret("ANTHROPIC_API_KEY") or has_secret("OLLAMA_HOST"))


__all__ = ["LLMScorer", "available", "SCHEMA_KEYS"]