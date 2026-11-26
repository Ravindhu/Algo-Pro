"""One screen refresh: read candidates, fetch each quote in parallel, write the bot's screen file.

Flow stage: Liquidity screener -> screen file
Status: PHASE 3 IMPLEMENTED (read-only; no order path; runs inside the bot's loop every 5 minutes).

Why parallel, and why no cache: each refresh asks the broker for every candidate's quote. The quote
also carries the ASM/GSM surveillance flag, which can change during the day, so a cached flag could
let a stock placed under surveillance keep trading. Every run therefore re-fetches. Parallel workers
keep a run to seconds-to-a-minute for a few hundred symbols.

Exit codes (same meaning as scripts/ops/refresh_screen.py):
    0  wrote at least one tradable symbol
    1  band source unreachable (every quote failed for a transport reason)
    2  wrote the file but nothing passed the band filter
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from quant.data import screener_engine
from quant.data.circuit_band import enrich_candidates

DEFAULT_WORKERS = 4   # parallel quote requests; keep low unless the broker allows more


def _enrich_one(row: Dict[str, Any], quote_fn: Callable[[str, str], Any], exchange: str) -> Dict[str, Any]:
    result = enrich_candidates([row], lambda s, _e: quote_fn(s, exchange), exchange=exchange)
    out = result["candidates"][0]
    out["_failed"] = bool(result["failed"])
    return out


def enrich_parallel(rows: List[Dict[str, Any]], quote_fn: Callable[[str, str], Any], exchange: str,
                    workers: int = DEFAULT_WORKERS) -> List[Dict[str, Any]]:
    """Enrich every row with its band and flag. Returns rows in the same order. Never raises for one row."""
    if not rows:
        return []
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        return list(pool.map(lambda r: _enrich_one(r, quote_fn, exchange), rows))


def refresh_once(candidates: List[Dict[str, Any]], quote_fn: Callable[[str, str], Any], target,
                 *, lookup_bse: Optional[Callable[[str], Optional[str]]] = None,
                 bse_quote_fn: Optional[Callable[[str, str], Any]] = None,
                 workers: int = DEFAULT_WORKERS) -> Dict[str, Any]:
    """Enrich, screen and write the screen file atomically. Returns a summary dict (no exceptions for data)."""
    from quant.data.bse_symbol import split_for_screen

    nse_list, bse_list, unresolved = split_for_screen(candidates, lookup_bse or (lambda code: None))
    nse_rows = enrich_parallel(nse_list, quote_fn, "NSE", workers)
    bse_rows = enrich_parallel(bse_list, bse_quote_fn or quote_fn, "BSE", workers) if bse_list else []
    all_rows = nse_rows + bse_rows

    failed = [r for r in all_rows if r.pop("_failed", False)]
    transport = [r for r in failed if "quote" in str(r.get("band_reason") or "")]
    payload = {"candidates": all_rows, "symbols": [r["symbol"] for r in all_rows]}
    doc = screener_engine.screen_rows(payload, max_symbols=len(all_rows) or 1)
    wrote = screener_engine.write_screen_file(payload, target, max_symbols=len(candidates) or 1)
    unreachable = bool(failed) and len(transport) == len(all_rows)
    code = 0 if wrote > 0 else (1 if unreachable else 2)
    return {
        "candidates": len(candidates),
        "enriched": len(all_rows) - len(failed),
        "selected": doc["counts"]["selected"],
        "dropped": doc["counts"]["dropped"],
        "wrote": wrote,
        "unresolved": unresolved,
        "exit_code": code,
    }


def load_candidates(path, symbols: Optional[str] = None) -> List[Dict[str, Any]]:
    """Explicit symbols win; otherwise the selected rows of the saved Screener.in screen. Never raises."""
    import json

    if symbols:
        return [{"symbol": s.strip().upper(), "selected": True} for s in symbols.split(",") if s.strip()]
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    return [dict(r) for r in (payload.get("results") or []) if isinstance(r, dict) and r.get("selected")]


def make_screen_refresher(reader, api_key: str, *, source, target) -> Callable[[Any], Dict[str, Any]]:
    """The callable the bot's loop runs every 5 minutes. Read-only: quotes and the symbol master only."""
    from quant.data.bse_symbol import openalgo_bse_lookup

    def refresh(_now) -> Dict[str, Any]:
        candidates = load_candidates(source)
        if not candidates:
            return {"exit_code": 2, "reason": f"no candidates in {source}"}
        return refresh_once(candidates, lambda s, e: reader.quotes(api_key, s, e), target,
                            lookup_bse=openalgo_bse_lookup)
    return refresh
