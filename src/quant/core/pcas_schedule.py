"""PCAS (periodic call auction) session clock for illiquid scrips (decision 0025).

Flow stage: Session guards
Status: PHASE 3 IMPLEMENTED (pure; the time is passed in, no clock read)
Source: NSE/CMTR/23063 (26 Mar 2013): six 1-hour sessions 09:30-15:30, random closure in the
last minute of order entry (between the 44th and 45th minute). NSE PCAS page: 45 min entry,
8 min matching, 7 min buffer (page summary; to be confirmed against a circular).

Safety rule: no new order from minute 44 of a session onward. The closure can land anywhere in
minute 44 to 45, so that minute is treated as unsafe.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

SESSION_STARTS: Tuple[time, ...] = (
    time(9, 30), time(10, 30), time(11, 30), time(12, 30), time(13, 30), time(14, 30),
)
SESSION_LENGTH_MIN = 60
ENTRY_MIN = 45          # order collection (page summary)
SAFE_ENTRY_MIN = 44     # last minute that starts before the random closure window

STATE_ENTRY_OPEN = "entry_open"
STATE_RANDOM_CLOSE = "random_close_zone"
STATE_MATCHING = "matching"
STATE_OUTSIDE = "outside_sessions"


def _ist(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now.astimezone(IST)


def _session_start_on(day, start: time) -> datetime:
    return datetime(day.year, day.month, day.day, start.hour, start.minute, tzinfo=IST)


def session_bounds(now: datetime) -> Optional[Tuple[datetime, datetime]]:
    """(start, end) of the session containing `now`, or None when outside all sessions."""
    t = _ist(now)
    for start in SESSION_STARTS:
        s = _session_start_on(t.date(), start)
        e = s + timedelta(minutes=SESSION_LENGTH_MIN)
        if s <= t < e:
            return (s, e)
    return None


def pcas_state(now: datetime) -> dict:
    """Classify the instant: entry_open, random_close_zone, matching or outside_sessions.

    Returns {"state", "minute", "session_start", "reason"}. `minute` is the minute index inside
    the session (0-59), or None outside sessions.
    """
    t = _ist(now)
    bounds = session_bounds(t)
    if bounds is None:
        return {"state": STATE_OUTSIDE, "minute": None, "session_start": None,
                "reason": "no PCAS session at this time"}
    start, _ = bounds
    minute = int((t - start).total_seconds() // 60)
    if minute < SAFE_ENTRY_MIN:
        state, why = STATE_ENTRY_OPEN, f"entry open, minute {minute} of 60"
    elif minute < ENTRY_MIN:
        state, why = STATE_RANDOM_CLOSE, f"minute {minute}: random closure can fall here; no new orders"
    else:
        state, why = STATE_MATCHING, f"minute {minute}: matching or buffer; no new orders"
    return {"state": state, "minute": minute, "session_start": start.isoformat(), "reason": why}


def orders_allowed(now: datetime) -> bool:
    """True only inside the safe part of order entry. Fail closed: anything else is False."""
    return pcas_state(now)["state"] == STATE_ENTRY_OPEN


def session_sequence(day) -> List[Tuple[datetime, datetime]]:
    """All six (start, end) session windows for a day. Used for schedule checks and logs."""
    out = []
    for start in SESSION_STARTS:
        s = _session_start_on(day, start)
        out.append((s, s + timedelta(minutes=SESSION_LENGTH_MIN)))
    return out
