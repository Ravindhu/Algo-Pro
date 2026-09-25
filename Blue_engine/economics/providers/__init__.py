"""
providers - data adapters for the economic engine.

The ONLY part of ``blue_engine.economics`` that performs I/O. Everything else
(features, models, scoring, regime, the engine) is pure and testable offline.

Public API
----------
MacroProvider          abstract contract (fetch never raises; meta block)
CachedProvider         TTL + filesystem cache decorator
CompositeProvider      merge many providers -> one macro dict (priority order)
OpenAlgoMacroProvider  macro proxies from the OpenAlgo SDK (no extra creds)
IndiaMacroProvider     official Indian prints (RBI / data.gov.in), free-key
FiiFlowProvider        FII/DII flows, PCR, India VIX (NSE public)
build_default_stack()  assemble the production stack from config + SDK client

See ``ECONOMIC_ENGINE_PLAN.md`` sections 3-4 for the source/cost table and the
architecture rationale.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .base import CachedProvider, MacroProvider, value_block
from .composite import CompositeProvider
from .fii_provider import FiiFlowProvider
from .india_provider import IndiaMacroProvider
from .openalgo_provider import OpenAlgoMacroProvider
from .secrets import get_secret, has_secret, redact

# Default cache directory: <repo>/data/macro_cache/ (survives engine reloads).
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_DIR = _REPO_ROOT / "data" / "macro_cache"


def build_default_stack(client: Any = None,
                        config: Optional[Dict[str, Any]] = None,
                        log: Optional[Callable[[str], None]] = None,
                        cache_dir: Optional[Path] = None
                        ) -> Optional[MacroProvider]:
    """Assemble the production provider stack.

    Priority (first non-null wins per signal):

      1. FiiFlowProvider     - freshest, intraday flows/VIX (short TTL)
      2. OpenAlgoMacroProvider - FX/commodities/indices from the SDK (no creds)
      3. IndiaMacroProvider  - slow official prints, long TTL (free key opt.)

    Each is wrapped in :class:`CachedProvider`. Returns a single
    :class:`CompositeProvider`, or ``None`` when no client is available and no
    provider can be constructed (the engine then degrades to config macro).

    Nothing here reads a config *file*; the caller passes a plain dict. This
    keeps the module testable and side-effect-free at import time.
    """
    config = dict(config or {})
    cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR

    fii_ttl = float(config.get("fii_ttl_seconds", 300.0))
    sdk_ttl = float(config.get("openalgo_ttl_seconds", 900.0))
    macro_ttl = float(config.get("india_ttl_seconds", 21600.0))  # 6h

    layers: List[MacroProvider] = []

    # 1. Flows (optional: needs requests OR an injected http_get).
    try:
        layers.append(CachedProvider(FiiFlowProvider(config.get("fii"),
                                                     log=log),
                                     ttl_seconds=fii_ttl,
                                     cache_dir=cache_dir, log=log))
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"[providers] fii provider unavailable: {exc!r}")

    # 2. SDK proxies (needs a client).
    if client is not None:
        try:
            layers.append(CachedProvider(
                OpenAlgoMacroProvider(client, config.get("openalgo"), log=log),
                ttl_seconds=sdk_ttl, cache_dir=cache_dir, log=log))
        except Exception as exc:  # noqa: BLE001
            if log:
                log(f"[providers] openalgo provider unavailable: {exc!r}")

    # 3. Official prints (optional: degrades without requests / key).
    try:
        layers.append(CachedProvider(IndiaMacroProvider(config.get("india"),
                                                        log=log),
                                     ttl_seconds=macro_ttl,
                                     cache_dir=cache_dir, log=log))
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"[providers] india provider unavailable: {exc!r}")

    if not layers:
        return None
    return CompositeProvider(layers, config=config, log=log)


__all__ = [
    "MacroProvider", "CachedProvider", "CompositeProvider",
    "OpenAlgoMacroProvider", "IndiaMacroProvider", "FiiFlowProvider",
    "build_default_stack", "value_block",
    "get_secret", "has_secret", "redact", "DEFAULT_CACHE_DIR",
]