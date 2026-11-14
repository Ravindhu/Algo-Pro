"""LLM token governor wrapper: prompt size and cache keys, DELEGATED to Blue_engine.

Flow stage: LLM token governor
Status: PHASE 5 IMPLEMENTED (wrap; accounting only: no model call, no network, advice path only)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/llm/tokens.py

The router that calls model backends (core/llm/router.py) is NOT wrapped here. This
module only counts tokens, compacts text and builds cache keys for the advice path.

FAIL-CLOSED RULES
-----------------
- Blue unavailable: ``budget`` reports ``within=False``. ``compact`` and ``cache_key``
  return None, so a caller cannot send a prompt that was never measured.
- Never raises.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["budget", "compact", "cache_key"]


def _tokens():
    try:
        return load("core.llm.tokens")
    except BlueEngineUnavailable:
        return None


def budget(text: str, max_tokens: int) -> Dict[str, Any]:
    """Estimate tokens for ``text`` and say whether it fits ``max_tokens``."""
    mod = _tokens()
    if mod is None:
        return {"ok": False, "tokens": None, "within": False,
                "reason": "token estimator unavailable: prompt not sent"}
    try:
        n = int(mod.estimate_tokens(text))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "tokens": None, "within": False,
                "reason": f"token estimate failed: {exc!r}"}
    within = n <= int(max_tokens)
    return {"ok": True, "tokens": n, "within": within,
            "reason": "" if within else f"{n} tokens is over the {max_tokens} budget"}


def compact(text: str, max_chars: int) -> Optional[str]:
    """Shorten ``text`` to about ``max_chars`` (Blue keeps the tail). None on failure."""
    mod = _tokens()
    if mod is None:
        return None
    try:
        out = mod.compact_text(text, max_chars)
    except Exception:  # noqa: BLE001
        return None
    # Blue returns (compacted_text, chars_elided); only the text is used here.
    if isinstance(out, tuple) and out and isinstance(out[0], str):
        return out[0]
    return None


def cache_key(messages: Sequence[Any], *, model: str,
              chain: Optional[Sequence[str]] = None,
              json_schema: Any = None, salt: Optional[str] = None) -> Optional[str]:
    """Deterministic cache key for a prompt. None on failure."""
    mod = _tokens()
    if mod is None:
        return None
    try:
        return mod.cache_key(messages, model=model, chain=chain,
                             json_schema=json_schema, salt=salt)
    except Exception:  # noqa: BLE001
        return None
