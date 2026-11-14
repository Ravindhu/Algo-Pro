"""Lot accounting wrapper: round a child quantity down to whole exchange lots, DELEGATED to Blue_engine.

Flow stage: Lot accounting
Status: PHASE 5 IMPLEMENTED (wrap; pure integer math; never sends an order)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/lot.py (lot_quantity)

Blue's rule passes the quantity through UNCHANGED when the lot size is zero or
negative. This wrapper does not: an unknown lot size sends nothing (fail closed).
The SDK lookup (resolve_lot_size) stays in Blue and is not called here.

FAIL-CLOSED RULES
-----------------
- Lot size missing, zero, negative, or not a plain int: qty 0, ok False.
- Quantity not a plain int: qty 0, ok False.
- Below one lot after rounding: qty 0, ok True (a valid answer: nothing to send).
- Blue unavailable: qty 0, ok False.
"""
from __future__ import annotations

from typing import Any, Dict

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["floor_to_lot"]


def floor_to_lot(quantity: Any, lot_size: Any) -> Dict[str, Any]:
    """Return ``{ok, qty, lot_size, reason}``. Never raises."""
    if isinstance(lot_size, bool) or not isinstance(lot_size, int) or lot_size <= 0:
        return {"ok": False, "qty": 0, "lot_size": lot_size,
                "reason": "lot size unknown: nothing sent"}
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        return {"ok": False, "qty": 0, "lot_size": lot_size,
                "reason": "quantity is not a whole number: nothing sent"}
    try:
        mod = load("core.lot")
    except BlueEngineUnavailable as exc:
        return {"ok": False, "qty": 0, "lot_size": lot_size,
                "reason": f"Blue lot module unavailable: nothing sent ({exc})"}
    qty = int(mod.lot_quantity(quantity, lot_size))
    if qty <= 0:
        return {"ok": True, "qty": 0, "lot_size": lot_size,
                "reason": "below one lot: nothing sent"}
    return {"ok": True, "qty": qty, "lot_size": lot_size, "reason": ""}
