"""NSE cash-equity pre-open session phase, from the time passed in. Pure: no clock read.

Flow stage: Session guards
Status: PHASE 3 IMPLEMENTED
Source: NSE circular 61/2019 (NSE/CMTR/41180, 31 May 2019): cash pre-open order entry 09:00-09:08
(system random closure in the 7th-8th minute), matching 09:08-09:12, buffer 09:12-09:15, open 09:15.
The 7 Sep 2026 pre-open change is for the equity derivatives (F&O) segment only, per secondary
sources. Check the cash circular before live use.

Policy in this module (conservative, owner-visible):
- Entries (limit orders) are accepted only before 09:07:00. Between 09:07:00 and 09:08:00 the
  close could come at any moment, so the phase is "entry_uncertain" and entries are refused.
- Market orders are refused in pre-open. They are allowed only in the normal session.
"""
from __future__ import annotations

from datetime import time

ENTRY_OPEN = "entry_open"
ENTRY_UNCERTAIN = "entry_uncertain"
MATCHING = "matching"
BUFFER = "buffer"
OPEN = "open"
CLOSED = "closed"


def pre_open_phase(now: time) -> str:
    if now < time(9, 0):
        return CLOSED
    if now < time(9, 7):
        return ENTRY_OPEN
    if now < time(9, 8):
        return ENTRY_UNCERTAIN
    if now < time(9, 12):
        return MATCHING
    if now < time(9, 15):
        return BUFFER
    return OPEN


def order_allowed(phase: str, kind: str, order_type: str) -> bool:
    """kind: 'entry' | 'exit' | 'protection'. order_type: 'LIMIT' | 'MARKET'."""
    if phase == OPEN:
        return True
    if phase == ENTRY_OPEN:
        return order_type == "LIMIT"  # limit only in pre-open, for every kind of order
    return False
