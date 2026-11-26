"""Clock normalisation for the live path (audit fix 1, 2026-11-26).

Bar timestamps and the screen's as_of are naive IST. now_ist() returns an IST-aware clock.
Subtracting one from the other raises TypeError, so every cycle died before exits ran, and
read_screen silently returned no symbols. Every entry point normalises to naive IST first.

Flow stage: Clock (shared)
Status: PHASE 3 IMPLEMENTED (pure conversion, no I/O).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def to_naive_ist(value: datetime) -> datetime:
    """Naive datetime in IST. An aware value is converted to IST first; a naive one is taken as IST."""
    if value.tzinfo is None:
        return value
    return value.astimezone(IST).replace(tzinfo=None)
