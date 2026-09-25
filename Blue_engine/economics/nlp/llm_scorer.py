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
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ...providers.secrets import get_secret, has_secret, redact

try:  # optional dependency
    import requests  # type: ignore
    _HAS_REQUESTS = True
except Exception:  # noqa: BLE001
    requests = None  # type: ignore
    _HAS_REQUESTS = False

# Default cache: <repo>/data/llm_cache/  (keyed by sha256 of the prompt text).
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_DIR = _REPO_ROOT / "data" / "llm_cache"

SCHEMA_KEYS = ("hawkish", "inflation_focus", "growth_focus", "guidance",
               "confidence")

_PROMPT_TEMPLATE = (
    "You are a macro analyst. Read the central-bank text and return ONLY a "
    "JSON object with exactly these keys: "
    '{"hawkish": float in [-1,1], "inflation_focus": float in [0,1], '
    '"growth_focus": float in [0,1], "guidance": one of '
    '["hawkish","neutral","dovish"], "confidence": float in [0,1]}. '
    "hawkish > 0 means more hawkish (rate-tightening bias). No prose, JSON "
    "only.\n\nTEXT:\n{text}"
)


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
                 cache_dir: Optional[Path] = None):
        self.config = dict(config or {})
        self._log_fn = log
        self._http_post = http_post
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR

        self.api_key = get_secret("OPENAI_API_KEY")
        self.ollama_host = get_secret("OLLAMA_HOST")
        self.model = self.config.get("model", "gpt-4o-mini")
        self.timeout = float(self.config.get("timeout", 30.0))

    def log(self, msg: str) -> None:
        if self._log_fn:
            self._log_fn(f"[{self.name}] {msg}")

    # ---- availability --------------------------------------------------- #
    def enabled(self) -> bool:
        return bool(self.api_key or self.ollama_host)

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled(),
            "requests": _HAS_REQUESTS,
            "openai_key": redact("OPENAI_API_KEY", self.api_key)
            if self.api_key else "<unset>",
            "ollama_host": self.ollama_host or "<unset>",
            "model": self.model,
        }

    # ---- endpoint ------------------------------------------------------- #
    def _endpoint(self) -> Optional[str]:
        if self.ollama_host:
            host = self.ollama_host.rstrip("/")
            return f"{host}/v1/chat/completions"
        if self.api_key:
            base = self.config.get("base_url", "https://api.openai.com/v1")
            return f"{base.rstrip('/')}/chat/completions"
        return None

    def _post(self, url: str, payload: Dict[str, Any],
              headers: Dict[str, str]) -> Optional[Any]:
        poster = self._http_post
        if poster is None:
            if not _HAS_REQUESTS:
                return None
            poster = requests.post  # type: ignore[union-attr]
        try:
            resp = poster(url, json=payload, headers=headers,
                          timeout=self.timeout)
            return resp.json() if hasattr(resp, "json") else resp
        except Exception as exc:  # noqa: BLE001
            self.log(f"request failed: {exc!r}")
            return None

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
                    "reason": "no OPENAI_API_KEY or OLLAMA_HOST"}

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

        url = self._endpoint()
        if url is None:
            return {"method": "disabled"}
        headers = {"Content-Type": "application/json"}
        if self.api_key and not self.ollama_host:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "temperature": 0.0,               # deterministic scoring
            "messages": [
                {"role": "user",
                 "content": _PROMPT_TEMPLATE.format(text=text[:6000])},
            ],
        }
        raw = self._post(url, payload, headers)
        if raw is None:
            return {"method": "error", "error": "no response"}

        content = None
        try:
            content = raw["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001
            return {"method": "error", "error": "unexpected response shape"}

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
    return _HAS_REQUESTS or has_secret("OPENAI_API_KEY")


__all__ = ["LLMScorer", "available", "SCHEMA_KEYS"]