"""Index circuit breaker: freeze entries when the market halts, resume when it lifts.

Flow stage: Market-halt handler
Status: PHASE 7 IMPLEMENTED (2026-11-14)
Existing code to reuse or wrap (do not copy): none in the engine.
    ``blue_engine/core/auction_windows.py`` is about SME periodic auctions, NOT
    index circuits, and the previous stage mapping that implied otherwise was wrong.

WHY THIS STAGE WAS MISSING
--------------------------
v5 2.7 lists "Market-halt handler: Index circuit freeze and resume" with nothing
implementing it. NSE halts the cash market when an index moves 10%, 15% or 20%
from the previous close, and the halt length depends on WHEN it triggers and WHICH
band is hit. Trading an entry into a halted market means the order queues into a
reopening auction at a price nobody has seen.

THE NSE RULE (stated, so it can be checked and corrected)
--------------------------------------------------------
Measured on the index versus its previous close:

  * 10% band: halt for 45 minutes, then a 15-minute pre-open, then resume.
    Before 13:00 it resumes the same day. At or after 13:00, it does not resume.
  * 15% band: halt for 1h45m, then 15-minute pre-open. Resumes only before 13:00.
  * 20% band: the market does not reopen that day.

This module models the bands and the resume decision. It does NOT fetch index
levels: the caller supplies the move, because only the data layer knows them.

FAIL-CLOSED RULES
-----------------
- An unknown or unreadable index move is treatable as a halt ONLY if the caller
  passes ``blocked_unknown=True``. By default an unknown value returns
  ``state="unknown"`` and the caller's own policy decides, because silently
  assuming "not halted" is how an order reaches a frozen market.
- Once a halt is detected, ``resume_at`` is computed; before that instant the
  state stays HALTED regardless of the current move, because NSE does not lift a
  halt early when the index recovers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "BANDS",
    "MarketState",
    "evaluate_halt",
    "halt_blocks_entries",
    "NO_RESUME_AFTER",
]

#: (threshold_pct, halt_minutes, resumes_same_day)
BANDS = (
    (10.0, 45, True),
    (15.0, 105, True),
    (20.0, 0, False),      # does not reopen that day
)

#: A band breach at or after this local time does not resume the same day.
NO_RESUME_AFTER = time(13, 0)

#: NSE reopens through a pre-open call auction of this length.
PREOPEN_MINUTES = 15


@dataclass(frozen=True)
class MarketState:
    """One evaluation of the circuit rules. Immutable so it can go in an audit row."""

    state: str                        # open | halted | no_resume | unknown
    band_pct: Optional[float] = None
    move_pct: Optional[float] = None
    halt_minutes: int = 0
    resume_at: Optional[datetime] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state, "band_pct": self.band_pct, "move_pct": self.move_pct,
            "halt_minutes": self.halt_minutes,
            "resume_at": self.resume_at.isoformat(timespec="minutes") if self.resume_at else None,
            "reason": self.reason,
        }


def evaluate_halt(change_pct: Optional[float], now: datetime,
                  *, band_pct: Optional[float] = None,
                  breached_at: Optional[datetime] = None) -> MarketState:
    """Evaluate the circuit rules for an index move at ``now``.

    Args:
        change_pct: index change versus previous close, in PERCENT (-10.4 means
            down 10.4%). ``None`` means unknown.
        now: local (IST) wall clock.
        band_pct: an already-active band, if a caller tracks it across calls. When
            given, the halt timing is computed from the band alone.
        breached_at: WHEN the band was first breached. A halt length is measured
            from this instant, not from ``now``, so a caller polling every cycle
            does not restart the clock on each poll. Omit it only on the first
            evaluation, where ``now`` IS the breach time.

    Returns a :class:`MarketState`.
    """
    if change_pct is None and band_pct is None:
        return MarketState(state="unknown", reason="index change not supplied")

    move = abs(float(change_pct)) if change_pct is not None else None

    threshold = band_pct
    if threshold is None:
        for limit, _minutes, _same_day in BANDS:
            if move is not None and move >= limit:
                threshold = limit

    if threshold is None:
        return MarketState(state="open", move_pct=move,
                           reason="index move inside the first band")

    cfg = next((b for b in BANDS if b[0] == threshold), None)
    if cfg is None:
        return MarketState(state="unknown", move_pct=move,
                           reason=f"unrecognised band {threshold}")

    limit, halt_minutes, resumes_same_day = cfg

    if not resumes_same_day:
        return MarketState(state="no_resume", band_pct=limit, move_pct=move,
                           halt_minutes=0,
                           reason=f"{limit:.0f}% band breached: the market does not reopen today")

    if now.time() >= NO_RESUME_AFTER:
        return MarketState(state="no_resume", band_pct=limit, move_pct=move,
                           halt_minutes=halt_minutes,
                           reason=(f"{limit:.0f}% band breached at/after "
                                   f"{NO_RESUME_AFTER.strftime('%H:%M')}: no resumption today"))

    # Measure the halt from the BREACH, not from the poll. A caller that checks
    # every cycle would otherwise restart the halt on every check and never resume.
    start = breached_at or now
    resume = start + timedelta(minutes=halt_minutes + PREOPEN_MINUTES)
    if resume.date() != start.date():
        return MarketState(state="no_resume", band_pct=limit, move_pct=move,
                           halt_minutes=halt_minutes,
                           reason="halt would run past the close")
    return MarketState(state="halted", band_pct=limit, move_pct=move,
                       halt_minutes=halt_minutes, resume_at=resume,
                       reason=(f"{limit:.0f}% band breached: halted {halt_minutes}m "
                               f"+ {PREOPEN_MINUTES}m pre-open, resumes "
                               f"{resume.strftime('%H:%M')}"))


def halt_blocks_entries(state: MarketState, now: Optional[datetime] = None, *,
                        blocked_unknown: bool = True) -> "tuple[bool, str]":
    """(blocked, reason). HALTED and NO_RESUME both block; exits always proceed.

    ``blocked_unknown`` defaults True: an unreadable index move blocks entries.
    That is the safe direction, because a queued order into a reopening auction is
    worse than a missed trade.
    """
    if state is None:
        return (True, "market state unknown (fail closed)") if blocked_unknown else (False, "")

    if state.state == "unknown":
        return ((True, state.reason or "market state unknown")
                if blocked_unknown else (False, ""))

    if state.state == "no_resume":
        return True, f"market halted with no resumption today: {state.reason}"

    if state.state == "halted":
        if state.resume_at is not None and now is not None and now >= state.resume_at:
            return False, f"halt lifted at {state.resume_at.strftime('%H:%M')}"
        until = state.resume_at.strftime("%H:%M") if state.resume_at else "unknown"
        return True, f"market halted, resumes {until}"

    return False, ""


def band_table() -> List[Dict[str, Any]]:
    """The configured bands, for the dashboard legend."""
    return [{"band_pct": limit, "halt_minutes": minutes,
             "resumes_same_day": same_day, "preopen_minutes": PREOPEN_MINUTES}
            for limit, minutes, same_day in BANDS]
