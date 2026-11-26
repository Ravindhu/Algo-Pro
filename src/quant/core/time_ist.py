"""Clock helper for the live path (audit 2026-11-26).

Flow stage: Clock
Status: PHASE 3 IMPLEMENTED (naive IST conversion, used before every bar or screen comparison).

The live clock is timezone-aware IST. The bar feeds and the screen file are naive IST. Comparing an
aware and a naive datetime raises TypeError, so every comparison converts first.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def naive_ist(value: datetime) -> datetime:
    """An aware value becomes naive IST. A naive value is returned unchanged."""
    if value.tzinfo is None:
        return value
    return value.astimezone(IST).replace(tzinfo=None)
