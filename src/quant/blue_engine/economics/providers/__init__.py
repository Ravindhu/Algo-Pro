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
RbiReferenceRateProvider  official FBIL USD/INR fix via rbi.org.in (no key)
MospiProvider          live CPI / GDP / unemployment (official MoSPI MCP)
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
from .mospi_provider import MospiProvider
from .openalgo_provider import OpenAlgoMacroProvider
from .rbi_provider import RbiReferenceRateProvider
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

      1. FiiFlowProvider          - intraday flows/VIX (short TTL)
      2. RbiReferenceRateProvider - official FBIL USD/INR fix
      3. MospiProvider            - CPI, GDP, unemployment (official)
      4. OpenAlgoMacroProvider    - commodities via the SDK
      5. IndiaMacroProvider       - slow official prints (free key opt.)

    Ordering notes
    --------------
    * RB I/FBIL sits ABOVE the SDK because most retail brokers (mstock
      included) carry no CDS currency data, so the SDK cannot price USD/INR at
      all. That is how the 'currency' signal once sat at 84.5 while USD/INR
      traded near 95.9.

    * MoSPI sits above the SDK for the same class of reason: inflation, GDP and
      unemployment are not broker data. They are official statistical releases
      that only MoSPI publishes, and before this layer existed all three were
      hand-typed constants that drifted (inflation was stuck at 2.5% while the
      real print was 4.82%).

    * Deliberately NOT wired: 'bond_return' and 'india_vix'.
      - The SDK's NIFTYGS10YR quotes 2644-2647, which is a bond INDEX level,
        not a yield (~6.68%). Scoring one as the other pins the signal at -1.
      - The SDK's INDIAVIX quote is internally inconsistent (ltp 38.88 with
        prev_close 12.16), so it cannot be trusted as a risk input.
      - FBIL/CCIL render via JS; NSE allIndices exposes only index levels;
        FRED is unreachable from this host. So no scriptable yield source
        exists here and these stay dated static values.

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
    # FBIL publishes once a business day; RBI reflects it the same evening.
    # 6h keeps a long-running process from holding yesterday's fix and stays
    # polite to a public government endpoint.
    rbi_ttl = float(config.get("rbi_ttl_seconds", 21600.0))
    rbi_enabled = bool(config.get("rbi_enabled", True))
    # MoSPI series are monthly/quarterly releases, so a 6h TTL is already far
    # more often than they change. Polling harder only loads their server.
    mospi_ttl = float(config.get("mospi_ttl_seconds", 21600.0))
    mospi_enabled = bool(config.get("mospi_enabled", True))

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

    # 2. Official USD/INR (no key, no broker entitlement).
    if rbi_enabled:
        try:
            layers.append(CachedProvider(
                RbiReferenceRateProvider(config.get("rbi"), log=log),
                ttl_seconds=rbi_ttl, cache_dir=cache_dir, log=log))
        except Exception as exc:  # noqa: BLE001
            if log:
                log(f"[providers] rbi provider unavailable: {exc!r}")

    # 3. Official MoSPI statistics (CPI / GDP / unemployment).
    if mospi_enabled:
        try:
            layers.append(CachedProvider(
                MospiProvider(config.get("mospi"), log=log),
                ttl_seconds=mospi_ttl, cache_dir=cache_dir, log=log))
        except Exception as exc:  # noqa: BLE001
            if log:
                log(f"[providers] mospi provider unavailable: {exc!r}")

    # 4. SDK proxies (needs a client).
    if client is not None:
        try:
            layers.append(CachedProvider(
                OpenAlgoMacroProvider(client, config.get("openalgo"), log=log),
                ttl_seconds=sdk_ttl, cache_dir=cache_dir, log=log))
        except Exception as exc:  # noqa: BLE001
            if log:
                log(f"[providers] openalgo provider unavailable: {exc!r}")

    # 5. Official prints (optional: degrades without requests / key).
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
    "RbiReferenceRateProvider", "MospiProvider",
    "build_default_stack", "value_block",
    "get_secret", "has_secret", "redact", "DEFAULT_CACHE_DIR",
]