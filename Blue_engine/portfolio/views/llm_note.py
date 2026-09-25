"""
views/llm_note.py - GPT/Ollama structured views + a written PM note (opt-in).

Two jobs, both *safe by design*:

1. **Structured view extraction** from free text (broker notes, filings, macro
   commentary) → the canonical view schema, which feeds Black-Litterman.
2. **The daily PM note** — a short written commentary drafted *from the run's
   numbers*, not from the live book.

Security (from AGENTS.md)
-------------------------
* The key is resolved only via ``economics.providers.secrets`` (env → _restored/.env);
  it is never hardcoded, never logged (use ``redact``).
* Prompts contain **no account numbers, no positions, no PII** — only public text
  and generic sleeve names.
* Every response is **cached by hash of the input**, so we never pay twice and
  results reproduce.
* No key / no ``openai`` / no Ollama → ``method='disabled'`` + a warning; the
  pipeline keeps running (the same degrade-never-halt rule everywhere else).

This mirrors ``economics/nlp/llm_scorer.py`` deliberately: one credential path, one
caching story, one JSON-only prompt contract across the codebase.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .base import make_view
from ...core.results import clamp, safe_float

logger = logging.getLogger(__name__)

__all__ = ["extract_views", "write_pm_note", "available", "cache_path",
           "VIEW_PROMPT"]

# Blue_engine/portfolio/views/llm_note.py -> repo root is 4 parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_DIR = _REPO_ROOT / "data" / "portfolio_llm"

# NOTE: the JSON exemplar's literal braces are doubled (``{{``/``}}``) because
# this string is consumed by ``str.format`` below; a single brace would make
# ``.format`` raise KeyError. Keep them doubled if you edit the exemplar.
VIEW_PROMPT = (
    "You are a sell-side quantitative analyst. Read the text and extract "
    "trading views. Reply with ONLY a JSON array; no prose, no markdown. "
    "Each element: "
    '{{"sleeve": "<index or sector name>", "view_return": <float decimal>, '
    '"confidence": <float 0-1>, "horizon_days": <int>, '
    '"rationale": "<short>"}}. '
    "view_return is the expected return over the horizon as a decimal "
    "(e.g. 0.012 for +1.2%). Use only the sleeve names provided. "
    "If the text implies no view, return [].\n"
    "ALLOWED SLEEVES: {sleeves}\n"
    "TEXT:\n{text}"
)

NOTE_PROMPT = (
    "You are a portfolio manager writing a terse daily desk note. Use ONLY the "
    "numbers provided; invent nothing. 3-5 sentences, no bullet lists, no "
    "heading. Mention gross exposure, the dominant risk sleeve, and the single "
    "biggest change vs the previous target.\n"
    "NUMBERS (JSON):\n{numbers}"
)


# --------------------------------------------------------------------------- #
# Availability & caching
# --------------------------------------------------------------------------- #

def _secret(name: str) -> Optional[str]:
    try:
        from ...economics.providers.secrets import get_secret
        return get_secret(name)
    except Exception:  # noqa: BLE001
        return os.getenv(name)


def available() -> Dict[str, Any]:
    """Report which LLM backends are usable (never reads the key value aloud)."""
    out = {"openai": False, "ollama": False, "method": "disabled"}
    if _secret("OPENAI_API_KEY"):
        try:
            import openai  # noqa: F401
            out["openai"] = True
            out["method"] = "openai"
        except Exception:  # noqa: BLE001
            out["openai_key"] = True  # key present but SDK missing
    if os.getenv("OLLAMA_HOST"):
        out["ollama"] = True
        if out["method"] == "disabled":
            out["method"] = "ollama"
    return out


def cache_path(text: str, *, cache_dir: Optional[Path] = None,
               tag: str = "views") -> Path:
    """Deterministic cache path from a hash of the input text."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    d = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    return d / f"{tag}_{digest}.json"


def _read_cache(path: Path) -> Optional[Any]:
    try:
        if path.is_file():
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:  # noqa: BLE001
        return None
    return None


def _write_cache(path: Path, payload: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
    except Exception as exc:  # noqa: BLE001 - cache is best-effort
        logger.debug("llm cache write failed: %s", exc)


# --------------------------------------------------------------------------- #
# LLM call (JSON only)
# --------------------------------------------------------------------------- #

def _call_llm(prompt: str, *, system: str = "You reply with JSON only.",
              timeout: float = 30.0) -> Dict[str, Any]:
    """Call the configured LLM. Returns ``{"ok", "text", "method", "warnings"}``."""
    avail = available()
    if avail["method"] == "openai":
        try:
            import openai
            key = _secret("OPENAI_API_KEY")
            client = openai.OpenAI(api_key=key)
            model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            resp = client.chat.completions.create(
                model=model, timeout=timeout,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": prompt}],
                temperature=0.2)
            return {"ok": True, "text": resp.choices[0].message.content,
                    "method": "openai", "warnings": []}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": "", "method": "openai",
                    "warnings": [f"openai call failed: {exc!r}"]}
    if avail["method"] == "ollama":
        try:
            import urllib.request
            host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
            model = os.getenv("OLLAMA_MODEL", "llama3.1")
            payload = json.dumps({"model": model, "prompt": prompt,
                                  "stream": False}).encode()
            req = urllib.request.Request(f"{host}/api/generate", data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode())
            return {"ok": True, "text": body.get("response", ""),
                    "method": "ollama", "warnings": []}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": "", "method": "ollama",
                    "warnings": [f"ollama call failed: {exc!r}"]}
    return {"ok": False, "text": "", "method": "disabled",
            "warnings": ["no LLM backend configured (OPENAI_API_KEY or OLLAMA_HOST)"]}


def _parse_json_array(text: str) -> Optional[List[Any]]:
    """Extract the first JSON array from a model reply (robust to stray prose)."""
    if not text:
        return None
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        val = json.loads(text[start:end + 1])
        return val if isinstance(val, list) else None
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def extract_views(text: str, *, sleeves: Sequence[str], horizon_days: int = 20,
                  cache_dir: Optional[Path] = None,
                  max_confidence: float = 0.7) -> Dict[str, Any]:
    """Turn free text into validated BL views via the LLM (cached).

    Returns ``{"ok", "views", "method", "warnings"}``. No backend -> ``ok=False``
    with a warning; the caller falls back to lexical NLP views.
    """
    sleeves = list(sleeves or [])
    prompt = VIEW_PROMPT.format(sleeves=", ".join(sleeves), text=(text or "")[:6000])
    path = cache_path(prompt, cache_dir=cache_dir, tag="views")
    cached = _read_cache(path)
    if isinstance(cached, dict) and cached.get("views") is not None:
        return {**cached, "cache": "hit"}

    resp = _call_llm(prompt)
    if not resp.get("ok"):
        return {"ok": False, "views": [], "method": resp.get("method", "disabled"),
                "warnings": resp.get("warnings", [])}

    raw = _parse_json_array(resp["text"])
    views: List[Dict[str, Any]] = []
    warnings = list(resp.get("warnings", []))
    if raw is None:
        warnings.append("LLM reply was not a JSON array; ignoring")
    else:
        allowed = {s.lower(): s for s in sleeves}
        for item in raw:
            if not isinstance(item, dict):
                continue
            sleeve = str(item.get("sleeve", "")).strip()
            resolved = allowed.get(sleeve.lower(), sleeve if sleeve in sleeves else None)
            if not resolved:
                warnings.append(f"view for unknown sleeve '{sleeve}' dropped")
                continue
            conf = safe_float(item.get("confidence"))
            conf = 0.5 if conf is None else clamp(conf, 0.001, max_confidence)
            v = make_view([resolved],
                          view_return=safe_float(item.get("view_return")) or 0.0,
                          confidence=conf,
                          horizon_days=int(safe_float(item.get("horizon_days"))
                                           or horizon_days),
                          source="llm",
                          label=f"{resolved} (LLM)",
                          rationale=str(item.get("rationale", ""))[:200])
            if v:
                views.append(v)
    out = {"ok": True, "views": views, "method": resp.get("method"),
           "warnings": warnings, "cache": "miss"}
    _write_cache(path, out)
    return out


def write_pm_note(numbers: Dict[str, Any], *,
                  cache_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Draft a short PM note from aggregate numbers (no positions, no PII).

    ``numbers`` should be aggregate stats only: gross, target_weights, risk vol,
    dominant contributor, top drift. Returns ``{"ok", "note", "method",
    "warnings"}``. No backend -> a deterministic templated note (so the PM Note
    tab is never empty).
    """
    # Build a sanitised, PII-free numbers blob.
    safe = {
        "gross": safe_float(numbers.get("gross")),
        "cash_weight": safe_float(numbers.get("cash_weight")),
        "portfolio_vol": safe_float((numbers.get("risk") or {}).get("portfolio_vol")),
        "dominant": (numbers.get("risk") or {}).get("dominant_contributor"),
        "regime": numbers.get("regime"),
        "target_weights": {k: round(safe_float(v) or 0.0, 4)
                           for k, v in (numbers.get("target_weights") or {}).items()},
        "top_drift": numbers.get("top_drift"),
    }
    prompt = NOTE_PROMPT.format(numbers=json.dumps(safe, default=str))
    path = cache_path(prompt, cache_dir=cache_dir, tag="note")
    cached = _read_cache(path)
    if isinstance(cached, dict) and cached.get("note"):
        return {**cached, "cache": "hit"}

    resp = _call_llm(prompt, system="You are a concise portfolio manager.")
    if resp.get("ok") and resp.get("text"):
        out = {"ok": True, "note": resp["text"].strip(),
               "method": resp.get("method"), "warnings": resp.get("warnings", []),
               "cache": "miss"}
    else:
        note = _templated_note(safe)
        out = {"ok": False, "note": note, "method": "template",
               "warnings": resp.get("warnings", []), "cache": "miss"}
    _write_cache(path, out)
    return out


def _templated_note(nums: Dict[str, Any]) -> str:
    """Deterministic fallback note when no LLM is available."""
    gross = nums.get("gross")
    vol = nums.get("portfolio_vol")
    dom = nums.get("dominant")
    regime = nums.get("regime")
    parts = []
    if regime:
        parts.append(f"Regime reads {regime}.")
    if gross is not None:
        parts.append(f"Target gross exposure {gross:.2f}x.")
    if vol is not None:
        parts.append(f"Forecast portfolio volatility {vol:.1%} annualised.")
    if dom:
        parts.append(f"{dom} is the dominant risk contributor.")
    if nums.get("cash_weight"):
        parts.append(f"Cash {nums['cash_weight']:.1%}.")
    return " ".join(parts) or "No numbers supplied for the desk note."