"""
time_utils.py — IST time helpers used by POV cutoff/squareoff.

No SDK calls here. Pure utility functions.
"""
from datetime import datetime, time as dtime
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = None  # type: ignore


def ist_now() -> datetime:
    """Current time in IST (Asia/Kolkata). Naive if zoneinfo is unavailable."""
    if IST is not None:
        return datetime.now(IST)
    return datetime.now()


def parse_hhmm(value: str) -> dtime:
    """Parse an 'HH:MM' 24-hour string into a datetime.time.

    Used to convert the cutoff_time and squareoff_time config fields into
    comparable time values.
    """
    h, m = value.strip().split(":")
    return dtime(int(h), int(m))


def is_past_cutoff(value: str, now: datetime | None = None) -> bool:
    """Return True if the current IST time is at or past the given 'HH:MM'.

    Args:
        value: 'HH:MM' string (e.g., '15:15')
        now:   Optional reference time; defaults to ist_now().

    Used by the POV slicer to stop accumulation at cutoff_time and start
    aggressive exit at squareoff_time.
    """
    if now is None:
        now = ist_now()
    cutoff = parse_hhmm(value)
    return now.time() >= cutoff


# ---------------------------------------------------------------------------
# Market session (audit 2026-10-29, LIVE-11)
# ---------------------------------------------------------------------------
#: NSE/BSE continuous trading session, IST.
SESSION_OPEN = dtime(9, 15)
SESSION_CLOSE = dtime(15, 30)
#: Operators should be able to stop NEW entries well before the close.
DEFAULT_ENTRY_CUTOFF = "14:45"

#: Exchange holidays. Deliberately EMPTY: a hardcoded list silently rots and a
#: wrong entry is worse than none (it would block a real session). The
#: authoritative list is published by NSE each year; load it into
#: ``Blue_engine/input/market_holidays.json`` as ``["2026-10-31", ...]``.
_HOLIDAY_PATH = None


def _holiday_set() -> set:
    """Read the optional holiday file. Missing/invalid file means "no list"."""
    global _HOLIDAY_PATH
    if _HOLIDAY_PATH is None:
        from pathlib import Path
        _HOLIDAY_PATH = (Path(__file__).resolve().parents[1] / "input"
                         / "market_holidays.json")
    try:
        import json
        raw = json.loads(_HOLIDAY_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("dates") or raw.get("holidays") or []
        return {str(d).strip() for d in (raw or []) if str(d).strip()}
    except Exception:
        return set()


def is_trading_day(now: datetime | None = None) -> tuple[bool, str]:
    """Is today a weekday that is not on the holiday list?

    Returns ``(ok, reason)``. The holiday file is optional; when absent the
    check is weekday-only and the reason says so, so an operator is never told
    "holiday verified" when it was not.
    """
    now = now or ist_now()
    if now.weekday() >= 5:                       # Sat=5, Sun=6
        return False, f"{now.strftime('%A')} - market closed"
    holidays = _holiday_set()
    if not holidays:
        return True, "weekday (no holiday file loaded)"
    if now.strftime("%Y-%m-%d") in holidays:
        return False, f"{now.strftime('%Y-%m-%d')} is an exchange holiday"
    return True, "weekday"


def market_session_state(now: datetime | None = None,
                         entry_cutoff: str = DEFAULT_ENTRY_CUTOFF
                         ) -> dict:
    """Classify the current IST time against the continuous session.

    Returns ``{"open": bool, "state": str, "reason": str, "now": iso}`` where
    ``state`` is one of ``closed_day`` (weekend/holiday), ``before_open``,
    ``open``, ``no_new_entries`` (past the entry cutoff but before the close)
    and ``after_close``.

    This is a *deterministic clock* check, not a market-data check: it cannot
    know about an unscheduled closure or a special session. Pair it with a
    live quote (the caller already does) before trading.
    """
    now = now or ist_now()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    ok, why = is_trading_day(now)
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
        cutoff = None
    if cutoff is not None and t >= cutoff:
        return {"open": True, "state": "no_new_entries",
                "reason": f"{t.strftime('%H:%M')} >= entry cutoff {entry_cutoff}; "
                          "exits only", "now": stamp}
    return {"open": True, "state": "open",
            "reason": "inside continuous session", "now": stamp}
