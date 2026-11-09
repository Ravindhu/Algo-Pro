"""Screener engine wrapper: universe selection is DELEGATED to Blue_engine.

Flow stage: Liquidity screener, Filings and circulars
Status: PHASE 5 IMPLEMENTED (wrap; extraction + offline evaluation local, scraping in Blue)
Existing code to reuse or wrap (do not copy): Blue_engine/screener/screener_engine.py
(ScreenerEngine), Blue_engine/screener/filters.py, Blue_engine/screener/valuations.py

WHAT IS WRAPPED AND WHAT IS LOCAL
---------------------------------
- Wrapped: saved-screen scraping, company-statement enrichment, annual reports and
  valuation inputs. All of it lives in Blue and is loaded through
  ``core.blue_bridge.load``. Nothing here re-implements a scraper.
- Local: ``select_universe``, which turns an engine payload into an ordered symbol
  list. ``data/screen_rules.py`` already holds the three-valued rule evaluator,
  so the offline path needs no Blue import at all.
- The engine is constructed with an injected provider, so a test (or an offline
  run) never touches the network. Only ``fetch_live`` reaches screener.in, and it
  says so in its name.

BOUNDARY (AGENTS.md rule 3)
---------------------------
Frontline outputs are inputs only. A universe list can never become an order:
this module imports no execution client and returns no intent.

LONG-ONLY INVESTMENT SYSTEM (owner, 2026-11-09)
-----------------------------------------------
This is an investment algo, not a trading book: BUY and ADD only, never short.
The universe is the first place that can keep that promise - short-side screens
(high short interest, falling-knife shorts, inverse ETFs) would otherwise reach
the signal stage as ordinary candidates. ``LONG_ONLY_EXCLUDES`` drops them here.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["ScreenerUnavailable", "fetch_live", "select_universe", "POLITE_DEFAULTS"]

#: Transport policy copied from Blue's defaults. Surfaced so the dashboard can
#: show the rate the scraper is running at.
POLITE_DEFAULTS: Dict[str, Any] = {
    "min_interval_ms": 400,
    "max_symbols": 250,
    "max_pages": 40,
    "cache_ttl_ms": 21_600_000,
}


#: Instruments that exist only to be shorted or leveraged short. A long-only
#: book must never receive them as a candidate, whatever screen produced them.
LONG_ONLY_EXCLUDES = {
    "INVERSE", "INVERS", "BEAR", "SHORT", "ULTRA_SHORT",
}

#: Suffixes/symbols used by inverse (bear) index products in India.
_SHORT_SYMBOL_PREFIXES = ("NIFTYBEES-INV", "BANKBEAR", "SBIBEAR", "GOLDBEES-INV")


class ScreenerUnavailable(RuntimeError):
    """Blue's screener engine cannot be loaded (for example, a missing extra)."""


def _engine_cls():
    try:
        module = load("screener.screener_engine")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise ScreenerUnavailable(f"Blue screener unavailable: {exc}") from exc
    return module.ScreenerEngine


def fetch_live(config: Optional[Dict[str, Any]] = None,
               provider: Optional[Callable[[], Dict[str, Any]]] = None,
               **kwargs: Any) -> Dict[str, Any]:
    """Run Blue's screener engine once and return ``{ok, data, warnings, error}``.

    ``provider`` wins over the network, exactly as in Blue: pass a callable that
    returns ``{"candidates": [...], "warnings": [...]}`` and the run is offline.
    With no provider and no config the run scrapes screener.in. Never raises.
    """
    cls = _engine_cls()
    engine = cls(dict(config or {}), provider=provider, **kwargs)
    result = engine.run({})
    return {
        "ok": bool(getattr(result, "ok", False)),
        "engine": getattr(result, "engine", "screener"),
        "data": dict(getattr(result, "data", {}) or {}),
        "warnings": list(getattr(result, "warnings", []) or []),
        "error": getattr(result, "error", None),
    }


def _is_short_side(symbol: str, names: Dict[str, str]) -> bool:
    """True when a symbol's name or ticker marks it as a short/inverse product."""
    if symbol in names and any(tag in names[symbol].upper() for tag in LONG_ONLY_EXCLUDES):
        return True
    return symbol.startswith(_SHORT_SYMBOL_PREFIXES)


def select_universe(payload: Dict[str, Any], *,
                    max_symbols: Optional[int] = None,
                    exclude: Optional[Sequence[str]] = None,
                    long_only: bool = True) -> List[str]:
    """Ordered symbols from a screener payload; the final gate before research.

    Rules (each one fails closed):
    1. ``selected`` wins, then ``symbols``. Missing or not a list -> empty list.
    2. A symbol that is blank, not a string, or the wrong type is DROPPED. The
       size of the payload is never trusted as the size of the universe.
    3. ``asset_blacklist`` symbols are removed. A blacklist entry is a verified
       exclusion (delisted, merged, surveillance flag), so it always wins.
    4. Short-side products are removed (``long_only``, default True). A long-only
       investment book cannot use an inverse ETF or a bear product.
    5. Duplicates are collapsed, first occurrence kept (the scraper orders by
       market cap, which is the liquidity order we want).
    6. The cap is applied last, so it can only shrink the list.
    """
    raw = payload.get("selected")
    if not isinstance(raw, list):
        raw = payload.get("symbols")
    if not isinstance(raw, list):
        return []

    blocked = {str(s).strip().upper()
               for s in (exclude or [])
               if isinstance(s, str) and s.strip()}
    listed = payload.get("asset_blacklist")
    if isinstance(listed, list):
        blocked |= {str(s).strip().upper() for s in listed}

    # Optional symbol -> company name map, used only for the short-side check.
    names: Dict[str, str] = {}
    for key in ("fundamentals", "company_snapshots"):
        block = payload.get(key)
        if isinstance(block, dict):
            for sym, row in block.items():
                if isinstance(row, dict) and row.get("name"):
                    names[str(sym).strip().upper()] = str(row["name"])

    out: List[str] = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        symbol = item.strip().upper()
        if not symbol or symbol in seen or symbol in blocked:
            continue
        if long_only and _is_short_side(symbol, names):
            continue
        seen.add(symbol)
        out.append(symbol)

    if max_symbols is not None:
        if max_symbols < 0:
            raise ValueError("max_symbols must be zero or more")
        out = out[:max_symbols]
    return out
