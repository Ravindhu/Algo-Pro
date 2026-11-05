"""Merged watchlist: a screener preset plus extra symbols, deduped. Moved from the removed console (2026-11-05).

Used by the optimizer data cache (optimizer/data_cache.py: watchlist_for_ui).
Pure logic: no UI, no network.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence


def resolve_watchlist(
    *,
    preset: Optional[str] = None,
    extra_symbols: Optional[Sequence[Dict[str, str]]] = None,
    exchange_filter: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Return the merged watchlist (preset + extras, deduped)."""
    try:
        from blue_engine.screener.presets import resolve_preset
    except Exception:
        resolve_preset = None  # type: ignore[assignment]

    out: List[Dict[str, str]] = []
    seen = set()
    if preset and resolve_preset:
        for r in resolve_preset(preset):
            sym = str(r.get("symbol", "")).strip().upper()
            exch = str(r.get("exchange", "")).strip().upper()
            if sym and exch and (sym, exch) not in seen:
                if exchange_filter and exch != exchange_filter.upper():
                    continue
                seen.add((sym, exch))
                out.append({"symbol": sym, "exchange": exch})
    if extra_symbols:
        for r in extra_symbols:
            try:
                sym = str(r["symbol"]).strip().upper()
                exch = str(r["exchange"]).strip().upper()
            except (KeyError, TypeError):
                continue
            if sym and exch and (sym, exch) not in seen:
                if exchange_filter and exch != exchange_filter.upper():
                    continue
                seen.add((sym, exch))
                out.append({"symbol": sym, "exchange": exch})
    return out
