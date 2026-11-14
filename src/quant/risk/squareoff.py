"""Square-off for MIS positions and futures rollover before expiry.

Flow stage: Square-off and rollover
Status: PHASE 3 IMPLEMENTED; false-wrap header removed, checks added 2026-11-14
Existing code to reuse or wrap (do not copy): none in the engine.
    The previous header pointed at ``blue_engine/core/auction_windows.py``, which
    is about SME periodic auctions and has nothing to do with square-off. That was
    the fourth false-wrap header found in this audit.

WHY THIS MATTERS
----------------
An MIS (intraday) position that is not squared off before the broker's cutoff is
squared off BY THE BROKER, at whatever price happens to be there, with a penalty
charge. That is the worst possible exit: unplanned, unpriced and unfavourable.

WHAT WAS WRONG
--------------
1. ``should_square_off`` compared a raw ``datetime.time`` with ``>=``. That is
   correct, but it treated ANY product string other than exactly "MIS" as
   never-square-off. A broker that reports "MIS" with spaces, or "INTRADAY", would
   have left positions to the broker. The product is now normalised.
2. ``cutoff`` defaulted to 15:15 with a comment calling it a placeholder. A wrong
   default here is a real loss, so it is now an explicit constant with the reason
   stated: NSE cash closes at 15:30 and brokers typically cut MIS a few minutes
   before, so 15:15 is a safe default that should still be set from config.
3. ``roll_due`` returned True for an ALREADY-EXPIRED contract, which would try to
   roll something that no longer trades. Now reported separately.

These functions decide WHEN to act. They do not place the order: the exit goes
through ``risk/gate.py`` like every other order.
"""
from __future__ import annotations

from datetime import date, time
from typing import Any, Dict

__all__ = [
    "DEFAULT_MIS_CUTOFF",
    "MIS_PRODUCTS",
    "should_square_off",
    "roll_due",
    "roll_urgency",
]

#: Broker MIS cutoff. NSE cash closes 15:30; brokers cut MIS a few minutes before
#: to allow their own square-off. SET THIS FROM CONFIG per broker.
DEFAULT_MIS_CUTOFF = time(15, 15)

#: Product codes that carry an intraday square-off obligation, normalised.
MIS_PRODUCTS = {"MIS", "INTRADAY", "I"}


def should_square_off(now: time, product: str, cutoff: time = DEFAULT_MIS_CUTOFF) -> bool:
    """True when an intraday position must be closed now.

    The product is normalised (stripped, upper-cased) so a broker reporting
    ``" mis"`` or ``"Intraday"`` is not silently left un-square-off.
    """
    if not isinstance(product, str):
        return False
    code = product.strip().upper()
    if code not in MIS_PRODUCTS:
        return False
    if not isinstance(now, time) or not isinstance(cutoff, time):
        return False
    return now >= cutoff


def roll_due(expiry: date, today: date, roll_days: int = 3) -> bool:
    """True when a contract expires within ``roll_days`` AND still trades."""
    if not isinstance(expiry, date) or not isinstance(today, date):
        return False
    if roll_days < 0:
        raise ValueError("roll_days must be >= 0")
    days_left = (expiry - today).days
    if days_left < 0:
        return False          # already expired: nothing to roll
    return days_left <= roll_days


def roll_urgency(expiry: date, today: date, roll_days: int = 3) -> Dict[str, Any]:
    """Roll status with the numbers, so the dashboard can show it.

        {"due": bool, "days_left": int, "expired": bool, "detail": str}
    """
    if not isinstance(expiry, date) or not isinstance(today, date):
        return {"due": False, "days_left": None, "expired": False,
                "detail": "invalid dates"}
    days_left = (expiry - today).days
    expired = days_left < 0
    if expired:
        return {"due": False, "days_left": days_left, "expired": True,
                "detail": f"expired {-days_left} day(s) ago; nothing to roll"}
    due = days_left <= roll_days
    return {"due": due, "days_left": days_left, "expired": False,
            "detail": (f"roll due: {days_left} day(s) to expiry"
                       if due else f"{days_left} day(s) to expiry")}
