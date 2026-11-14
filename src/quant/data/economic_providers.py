"""Economic providers wrapper: the provider contract and a degrade-never-halt read, DELEGATED to Blue_engine.

Flow stage: Economic providers
Status: PHASE 5 IMPLEMENTED (wrap; contract only; the quant layer fetches nothing)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/economics/providers/base.py
(value_block)

The network providers (RBI, MoSPI, FII, India) are NOT built or called here. A caller
passes a provider in. This wrapper only reads it and says how much to trust it.

FAIL-CLOSED RULES
-----------------
- A provider that raises, returns no mapping, or returns no ``meta`` block: ``ok=False``.
- A missing ``meta.stale`` is treated as stale. Unknown freshness is never fresh.
- Never raises.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["read_macro", "to_block"]


def read_macro(provider: Any) -> Dict[str, Any]:
    """Call an injected provider once and report ``{ok, stale, data, warnings}``."""
    if provider is None:
        return {"ok": False, "stale": True, "data": {}, "warnings": ["no provider supplied"]}
    try:
        payload = provider.fetch()
    except Exception as exc:  # noqa: BLE001 - providers must not halt the caller
        return {"ok": False, "stale": True, "data": {},
                "warnings": [f"provider raised: {exc!r}"]}
    if not isinstance(payload, dict):
        return {"ok": False, "stale": True, "data": {},
                "warnings": ["provider returned no mapping"]}
    data = {k: v for k, v in payload.items() if k != "meta"}
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return {"ok": False, "stale": True, "data": data,
                "warnings": ["no meta block: data is untrusted"]}
    warnings: List[str] = [str(w) for w in (meta.get("warnings") or [])]
    return {
        "ok": bool(meta.get("ok", False)),
        "stale": bool(meta.get("stale", True)),
        "data": data,
        "warnings": warnings,
    }


def to_block(value: Any, previous: Any = None, unit: Optional[str] = None,
             as_of: Optional[str] = None, source: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Build the canonical ``{value, previous, unit, as_of, source}`` block. None if Blue is missing."""
    try:
        fn = load("economics.providers.base").value_block
    except BlueEngineUnavailable:
        return None
    return fn(value, previous, unit, as_of, source)
