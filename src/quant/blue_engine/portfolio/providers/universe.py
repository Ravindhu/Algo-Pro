"""
providers/universe.py - sleeve definitions and the default India universe.

A "sleeve" is an investable bucket the portfolio allocates across. Sleeves are
typically index/sector exposures, and each maps to one or more tradable
instruments the OpenAlgo SDK can price:

    {"BANKNIFTY": {"symbol": "NIFTY BANK", "exchange": "NSE_INDEX"},
     "IT":        {"symbol": "NIFTY IT",  "exchange": "NSE_INDEX"},
     "RELIANCE":  {"symbol": "RELIANCE",  "exchange": "NSE"}}

The engine never hardcodes a universe: it comes from config or this default.
This module also answers "which sleeve does this symbol belong to?" for the
account provider (mapping a live position back to a sleeve).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_UNIVERSE", "resolve_universe", "sleeve_for_symbol",
           "sleeve_symbols"]


#: Default India sleeve universe — the major NSE indices plus a few single-name
#: sleeves. ``exchange`` is the OpenAlgo exchange token used by ``client.history``.
DEFAULT_UNIVERSE: Dict[str, Dict[str, Any]] = {
    "NIFTY":     {"symbol": "NIFTY", "exchange": "NSE_INDEX", "cap": 0.30},
    "BANKNIFTY": {"symbol": "NIFTY BANK", "exchange": "NSE_INDEX", "cap": 0.25},
    "IT":        {"symbol": "NIFTY IT", "exchange": "NSE_INDEX", "cap": 0.20},
    "PHARMA":    {"symbol": "NIFTY PHARMA", "exchange": "NSE_INDEX", "cap": 0.15},
    "AUTO":      {"symbol": "NIFTY AUTO", "exchange": "NSE_INDEX", "cap": 0.15},
    "METALS":    {"symbol": "NIFTY METAL", "exchange": "NSE_INDEX", "cap": 0.15},
    "ENERGY":    {"symbol": "NIFTY ENERGY", "exchange": "NSE_INDEX", "cap": 0.15},
    "FMCG":      {"symbol": "NIFTY FMCG", "exchange": "NSE_INDEX", "cap": 0.15},
    "REALTY":    {"symbol": "NIFTY REALTY", "exchange": "NSE_INDEX", "cap": 0.15},
    "MIDCAP":    {"symbol": "NIFTY MIDCAP 50", "exchange": "NSE_INDEX", "cap": 0.15},
}


def resolve_universe(config: Optional[Dict[str, Any]] = None
                     ) -> Dict[str, Dict[str, Any]]:
    """Merge a configured universe over the default (config wins)."""
    out = {k: dict(v) for k, v in DEFAULT_UNIVERSE.items()}
    cfg = (config or {}).get("universe")
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if isinstance(v, dict):
                out[k] = {**out.get(k, {}), **v}
            elif isinstance(v, str):
                out[k] = {**out.get(k, {}), "symbol": v}
    return out


def sleeve_names(config: Optional[Dict[str, Any]] = None) -> List[str]:
    return list(resolve_universe(config).keys())


def sleeve_symbols(config: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, Dict[str, Any]]:
    """Return the SDK-facing map ``{sleeve: {"symbol","exchange"}}``."""
    uni = resolve_universe(config)
    return {k: {"symbol": v.get("symbol", k), "exchange": v.get("exchange",
                                                              "NSE_INDEX")}
            for k, v in uni.items()}


def sleeve_for_symbol(symbol: str, config: Optional[Dict[str, Any]] = None
                      ) -> Optional[str]:
    """Map a traded symbol back to its sleeve.

    Exact symbol match first, then a case-insensitive substring fallback, so a
    live position in "NIFTY BANK" or a futures contract resolves to "BANKNIFTY".
    Returns None when nothing matches (the engine then treats it as its own
    sleeve, or ignores it — caller's choice).
    """
    if not symbol:
        return None
    uni = resolve_universe(config)
    up = symbol.upper().strip()
    for sleeve, meta in uni.items():
        if str(meta.get("symbol", "")).upper() == up:
            return sleeve
    for sleeve, meta in uni.items():
        sym = str(meta.get("symbol", "")).upper()
        if sym and (sym in up or up in sym):
            return sleeve
    # Fall back to a bare name match (e.g. "RELIANCE" sleeve matches symbol).
    for sleeve in uni:
        if sleeve.upper() in up:
            return sleeve
    return None