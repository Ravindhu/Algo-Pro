"""Session guards: market-halt handler, event blackout, overnight gap check.

Flow stage: Market-halt handler, Event blackout, Overnight gap check
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/auction_windows.py
"""
from __future__ import annotations

from datetime import date
from typing import Iterable, Optional, Set


class HaltHandler:
    """Index circuit freeze. Entries stay blocked until the freeze is cleared by a person or the feed."""

    def __init__(self) -> None:
        self.frozen: Set[str] = set()

    def freeze(self, index: str) -> None:
        self.frozen.add(index)

    def resume(self, index: str) -> None:
        self.frozen.discard(index)

    def is_frozen(self, index: str) -> bool:
        return index in self.frozen


def index_move_freezes(halt: "HaltHandler", index: str, prev_close: float, last: float,
                       limit: float = 0.10) -> bool:
    """Fallback freeze: if the index moved by the circuit limit (default 10%), freeze it (fail closed).

    Used when no halt message arrives. Returns True when the index is frozen after this check.
    """
    if prev_close > 0 and abs(prev_close - last) / prev_close >= limit:
        halt.freeze(index)
    return halt.is_frozen(index)


def in_event_blackout(day: date, blackout_days: Iterable[date]) -> bool:
    """Results, RBI policy, Budget and Fed days. No new entries on these dates."""
    return day in set(blackout_days)


def carry_allowed(product: str, adv_ok: bool) -> bool:
    """MIS (intraday) positions never carry overnight. Delivery carry needs liquidity."""
    return product != "MIS" and adv_ok


def gap_exit_required(prev_close: float, open_px: float, gap_limit: float = 0.10) -> Optional[str]:
    gap = open_px / prev_close - 1
    if abs(gap) > gap_limit:
        return f"overnight gap {gap:+.1%} beyond limit {gap_limit:.0%}: review exit"
    return None
