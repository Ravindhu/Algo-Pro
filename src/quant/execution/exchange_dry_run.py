"""Dry run of the exchange decision on real quotes. Reads only; never places an order.

Status: IMPLEMENTED (tests in tests/unit/test_exchange_dry_run.py). Plan:
docs/IMPLEMENTATION_PLAN_2026-11-25_BOT_EXCHANGE_CARRY.md (slice 6)

For each screened symbol it shows: the listing (signal exchange), the entry choice
(NSE unless BSE is at least 0.05% cheaper), the buy price, the exit price on the
chosen exchange, the lot size and the circuit band. Cost rules are unchanged.
The output says what WOULD happen; the entry gate still refuses BSE entries.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from quant.execution.exchange_router import choose_entry, exit_price, listing_exchange, quote_from_reply


def decide(symbol: str, nse_reply: Any, bse_reply: Any, lot_size: Optional[int] = None,
           circuit_pct: Optional[float] = None) -> Dict[str, Any]:
    """One row of the dry run. Pure: takes the raw quote replies, returns a plain dict."""
    nse, bse = quote_from_reply(nse_reply), quote_from_reply(bse_reply)
    listing = listing_exchange(nse, bse)
    entry = choose_entry(nse, bse)
    held = entry.exchange
    exit_q = exit_price(held, {"NSE": nse, "BSE": bse}) if held else None
    return {
        "symbol": symbol,
        "listing": listing,               # which exchange's bars the signal would use
        "entry_exchange": entry.exchange,  # where the buy would go
        "buy_price": entry.price,
        "entry_reason": entry.reason,
        "exit_exchange": exit_q.exchange if exit_q else None,  # exit at best bid here
        "exit_price": exit_q.price if exit_q else None,
        "lot_size": lot_size,
        "circuit_pct": circuit_pct,
        "would_trade": entry.exchange is not None,
        "bse_entry_refused_by_gate": entry.exchange == "BSE",  # slice 2 refusal still in force
    }


def run(symbols, reader, api_key: str, lot_reader=None) -> list:
    """Read quotes for each symbol on NSE and BSE and return one decision row each.

    Only read calls are made (quotes and search). A failed read becomes a missing quote,
    which fails closed in decide().
    """
    rows = []
    for sym in symbols:
        def _read(exchange):
            try:
                return reader.quotes(api_key, sym, exchange)
            except Exception:  # noqa: BLE001 - a failed read is a missing quote
                return None
        lot = None
        if lot_reader is not None:
            try:
                lot = lot_reader(api_key, sym)
            except Exception:  # noqa: BLE001
                lot = None
        rows.append(decide(sym, _read("NSE"), _read("BSE"), lot_size=lot))
    return rows
