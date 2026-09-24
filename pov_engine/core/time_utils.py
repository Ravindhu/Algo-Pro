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
