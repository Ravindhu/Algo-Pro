"""
providers/composite.py - fuse many providers into one macro dict.

The economic engine consumes a *single* macro mapping. In production that
mapping is assembled from several providers (SDK proxies, official prints,
flows), each of which may succeed, partially fail, or be unavailable. This
module is the merge layer that:

  * queries providers in **priority order**,
  * keeps the **first non-null** value for each signal (higher-priority wins),
  * unions the warnings so a partial outage is visible, not silent,
  * records **provenance** (which provider produced each signal) for forensics,
  * merges the nested ``meta`` blocks.

It is intentionally pure-ish: it performs no I/O itself; the providers do. That
keeps the merge logic trivially unit-testable with fake providers.

Usage::

    from blue_engine.economics.providers import (
        MacroProvider, CompositeProvider, CachedProvider)
    provider = CompositeProvider([flows, sdk, official, config_fallback])
    macro = provider.fetch()          # one dict, warnings merged, provenance in meta
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .base import MacroProvider

#: Reserved key that carries provenance/provenance/warnings; never a signal.
META_KEY = "meta"


class CompositeProvider(MacroProvider):
    """Merge several providers into one macro mapping (priority order)."""

    name = "composite"

    def __init__(self, providers: List[MacroProvider],
                 config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 *, required: Optional[List[str]] = None):
        super().__init__(config, log)
        #: Providers are queried in this order; earlier wins on conflicts.
        self.providers: List[MacroProvider] = list(providers or [])
        #: Signals which, if ALL missing, flag the result ``ok=False``. The
        #: engine treats an empty macro block as "neutral by default", so this
        #: is only used for reporting, never to raise.
        self.required: List[str] = list(required or [])

    def _fetch(self) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        provenance: Dict[str, str] = {}
        warnings: List[str] = []
        per_source: Dict[str, Any] = {}

        for provider in self.providers:
            payload = provider.fetch()          # never raises (base contract)
            meta = payload.get(META_KEY) or {}
            ok = bool(meta.get("ok", False))
            per_source[provider.name] = {
                "ok": ok,
                "stale": bool(meta.get("stale", False)),
                "latency_ms": meta.get("latency_ms"),
                "warnings": list(meta.get("warnings") or []),
            }
            warnings.extend(meta.get("warnings") or [])

            for key, val in payload.items():
                if key == META_KEY:
                    continue
                # First non-null wins (priority order preserved).
                if key not in merged or merged.get(key) in (None, {}):
                    merged[key] = val
                    provenance[key] = provider.name
                elif val not in (None, {}):
                    # Keep the higher-priority value but remember it existed
                    # twice (useful when debugging a surprising reading).
                    provenance.setdefault(f"{key}@alt", provider.name)

        missing = [k for k in self.required
                   if k not in merged or merged[k] in (None, {})]
        if missing:
            warnings.append(f"required signals missing: {missing}")

        merged[META_KEY] = {
            "ok": not missing,
            "stale": any(v.get("stale") for v in per_source.values()),
            "warnings": warnings,
            "source": self.name,
            "provenance": provenance,
            "providers": per_source,
        }
        return merged

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "providers": [p.name for p in self.providers],
            "required": self.required,
        }


__all__ = ["CompositeProvider", "META_KEY"]