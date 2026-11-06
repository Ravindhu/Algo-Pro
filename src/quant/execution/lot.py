"""Exchange lot-size rounding for child orders (stage 6.5 pure port of Blue core/lot.py).

Flow stage: Algo router
Status: PHASE 3 IMPLEMENTED (pure port; lot-size lookup is injected, no broker or DB import)
Existing code to reuse or wrap (do not copy): Blue_engine/core/lot.py (lot_quantity, resolve_lot_size).
Not ported on purpose: Blue's master-DB and SDK lookups. Those are broker and data adapters.
A caller passes a lookup function instead (decision 0022).

Rounding rule: a child quantity is floored to a whole multiple of the exchange lot size.
Integer math only, so no float drift over many child orders.
"""
from __future__ import annotations

from typing import Callable, Optional

LotLookup = Callable[[str, str], Optional[int]]


def lot_quantity(quantity: int, lot_size: int) -> int:
    """Largest whole lot multiple <= quantity.

    lot_size <= 0 disables rounding: the quantity is returned unchanged (never negative).
    quantity <= 0 gives 0 when rounding is on.
    """
    if quantity <= 0 or lot_size <= 0:
        return max(0, quantity) if lot_size <= 0 else 0
    return (quantity // lot_size) * lot_size


def _positive_int(value) -> Optional[int]:
    """Int > 0, or None for anything else (including bool-like junk and floats that do not parse)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def resolve_lot_size(
    symbol: str,
    exchange: str,
    override=None,
    lookup: Optional[LotLookup] = None,
) -> Optional[int]:
    """Lot size for a symbol, or None when it is unknown. Never returns 0.

    Order:
      1. override (from config): wins. Invalid or <= 0 gives None, and the lookup is not used.
      2. lookup(symbol, exchange): the injected source. Errors and bad values give None.
      3. None: the caller decides what to do. The slicer skips rounding and logs it.
    """
    if override is not None:
        return _positive_int(override)
    if not symbol or not exchange or lookup is None:
        return None
    try:
        found = lookup(symbol, exchange)
    except Exception:
        return None
    return _positive_int(found) if found is not None else None
