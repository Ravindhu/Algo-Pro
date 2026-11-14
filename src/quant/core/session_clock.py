"""IST clock and continuous-session helpers (pure port of Blue core/time_utils.py, stage 6.5).

Flow stage: Trading calendar, Session guards
Status: PHASE 3 IMPLEMENTED (pure; the clock and calendar are passed in, no file reads)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/time_utils.py.
Not ported on purpose: Blue's holiday-file loader. The holiday list lives in quant/core/calendar.py
(TradingCalendar). This module takes a calendar object and does not read any file.

Fail-closed difference from Blue (decision 0022): when no calendar is given, or the calendar is
not verified, the session is reported closed. Blue reported weekdays as open with no holiday list.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # zoneinfo or tzdata missing
    IST = None

SESSION_OPEN = dtime(9, 15)
SESSION_CLOSE = dtime(15, 30)
DEFAULT_ENTRY_CUTOFF = "14:45"


def ist_now() -> datetime:
    """Current time in IST. Naive local time if zoneinfo is unavailable."""
    if IST is not None:
        return datetime.now(IST)
    return datetime.now()


def parse_hhmm(value: str) -> dtime:
    """'HH:MM' (24-hour) to datetime.time. Bad input raises ValueError."""
    h, m = value.strip().split(":")
    return dtime(int(h), int(m))


def is_past_cutoff(value: str, now: Optional[datetime] = None) -> bool:
    """True when the IST time is at or after the given 'HH:MM'."""
    if now is None:
        now = ist_now()
    return now.time() >= parse_hhmm(value)


def _day_status(now: datetime, calendar: Any) -> tuple:
    """(ok, reason) for the calendar part. Weekend, holiday, or no verified list means not ok."""
    if now.weekday() >= 5:
        return False, f"{now.strftime('%A')} - market closed"
    if calendar is None:
        return False, "no trading calendar given; cannot verify the trading day"
    if not calendar.verified:
        return False, "trading calendar not verified (empty holiday list); do not trade"
    day: date = now.date()
    if day in calendar.holidays:
        return False, f"{day.isoformat()} is an exchange holiday"
    return True, "weekday"


def market_session_state(
    now: Optional[datetime] = None,
    entry_cutoff: str = DEFAULT_ENTRY_CUTOFF,
    calendar: Any = None,
) -> dict:
    """Classify the IST time against the continuous session.

    Returns {"open", "state", "reason", "now"}. state is one of:
      closed_day (weekend, holiday, or calendar not usable), before_open, open,
      no_new_entries (past the entry cutoff, before close: exits only), after_close.

    This is a clock check, not a market-data check. Pair it with a live quote before trading.
    """
    now = now or ist_now()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    ok, why = _day_status(now, calendar)
    if not ok:
        return {"open": False, "state": "closed_day", "reason": why, "now": stamp}
    t = now.time()
    if t < SESSION_OPEN:
        return {"open": False, "state": "before_open",
                "reason": f"{t.strftime('%H:%M')} < 09:15", "now": stamp}
    if t >= SESSION_CLOSE:
        return {"open": False, "state": "after_close",
                "reason": f"{t.strftime('%H:%M')} >= 15:30", "now": stamp}
    try:
        cutoff = parse_hhmm(entry_cutoff)
    except Exception:
        cutoff = parse_hhmm(DEFAULT_ENTRY_CUTOFF)  # bad config: use the safe default, never "no cutoff"
    if t >= cutoff:
        return {"open": True, "state": "no_new_entries",
                "reason": f"{t.strftime('%H:%M')} >= entry cutoff {entry_cutoff}; exits only",
                "now": stamp}
    return {"open": True, "state": "open", "reason": "inside continuous session", "now": stamp}
