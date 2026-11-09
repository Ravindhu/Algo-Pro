"""Call-auction window checks (pure port of Blue core/auction_windows.py, stage 6.5, decision 0024).

Flow stage: Session guards
Status: PHASE 3 IMPLEMENTED (pure; the time is passed in, no clock or env read)
Existing code to reuse or wrap (do not copy): Blue_engine/core/auction_windows.py.

Differences from Blue (decision 0024):
  - No clock read. The caller passes `now`. Blue imported its own clock inside each function.
  - No environment read. Windows come from config (or the default). Blue read POV_SME_AUCTION_WINDOWS.
  - No `is_in_call_auction_window` alias. It is the same function under a second name.
Matching is minute-based and both ends are inclusive, as in Blue. Owner to confirm (decision 0024).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional, Tuple
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Default: one window covering the continuous session (matches Blue's default).
DEFAULT_SME_AUCTION_WINDOWS: Tuple[Tuple[str, str], ...] = (("09:15", "15:30"),)

_WINDOW_RE = re.compile(r"^\s*(\d{1,2}:\d{2})\s*[-,]\s*(\d{1,2}:\d{2})\s*$")


def _minutes(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":", 1))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid HH:MM: {value!r}")
    return hour * 60 + minute


def _format_hhmm(total_minutes: int) -> str:
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def parse_auction_windows(raw: Any = None) -> Tuple[Tuple[str, str], ...]:
    """Validate and normalise windows. Accepts None, a comma string, a list of strings, pairs or dicts.

    None gives DEFAULT_SME_AUCTION_WINDOWS. Raises ValueError on any bad entry, an end not after
    its start, or an empty list.
    """
    if raw is None:
        return DEFAULT_SME_AUCTION_WINDOWS
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("auction_windows must contain at least one window")

    windows = []
    for item in raw:
        if isinstance(item, (tuple, list)) and len(item) == 2 and all(isinstance(x, str) for x in item):
            start_raw, end_raw = item[0], item[1]
        else:
            if isinstance(item, dict):
                item = f"{item.get('start', item.get('from', ''))}-{item.get('end', item.get('to', ''))}"
            match = _WINDOW_RE.match(str(item))
            if not match:
                raise ValueError("auction windows must use HH:MM-HH:MM")
            start_raw, end_raw = match.groups()
        start_min = _minutes(start_raw)
        end_min = _minutes(end_raw)
        if start_min >= end_min:
            raise ValueError("auction window end must be after its start")
        windows.append((_format_hhmm(start_min), _format_hhmm(end_min)))
    return tuple(windows)


def _minute_of_day(now: datetime) -> int:
    """Minute of day in IST. Aware datetimes are converted; naive ones are taken as IST already."""
    current = now
    if current.tzinfo is not None:
        current = current.astimezone(IST).replace(tzinfo=None)
    return current.hour * 60 + current.minute


def current_auction_window(now: datetime, windows: Any = None) -> Optional[Tuple[str, str]]:
    """The first configured window containing `now`, or None."""
    minute = _minute_of_day(now)
    for start, end in parse_auction_windows(windows):
        if _minutes(start) <= minute <= _minutes(end):
            return (start, end)
    return None


def is_within_auction_window(now: datetime, windows: Any = None) -> bool:
    """True when `now` falls inside any configured window."""
    return current_auction_window(now, windows) is not None
