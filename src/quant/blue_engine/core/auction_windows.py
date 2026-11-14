"""
auction_windows.py - Periodic Call Auction (PCAS) session window logic.

Responsibility
--------------
Given a configured set of ``"HH:MM-HH:MM"`` windows, answer three
questions the POV loop needs to stay compliant on SME / PCAS listings
(Feature 5 - SME / Periodic Call Auction Support):

  1. Is a given instant inside ANY configured call-auction window?
     The slicer asks this on every poll and refuses to place a
     continuous-stream child order outside one.
  2. Which window (if any) is the current instant in? Used for
     logging and for the "AuctionWindowGuard" depth-aware guard.
  3. Was a raw user-supplied window list syntactically valid?

Like :mod:`blue_engine.core.symbol_meta`, this module is pure: no
SDK, no DB, no network. It uses only :class:`datetime` and a small
regex. That keeps it trivial to unit-test and re-usable by any
future slicer (TWAP, Iceberg, etc.).

Default windows
---------------
NSE SME/Periodic Call Auction sessions are run in discrete windows
spread across the trading day. The defaults here match the standard
schedule but can be overridden in the JSON config. The defaults are
exposed as a tuple of ``(start, end)`` strings so callers can pass
them straight to the parser.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Defaults and the environment override


# ---------------------------------------------------------------------------
# Small time helpers
# ---------------------------------------------------------------------------
def _minutes(value: str) -> int:
    """Convert an ``"HH:MM"`` string into minutes-since-midnight.

    Raises ``ValueError`` for malformed input. Kept private because
    callers should always go through :func:`parse_auction_windows`,
    which validates the entire structure first.
    """
    hour, minute = (int(part) for part in value.split(":", 1))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid HH:MM: {value!r}")
    return hour * 60 + minute


def _format_hhmm(total_minutes: int) -> str:
    """Inverse of :func:`_minutes` for display purposes."""
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


# ---------------------------------------------------------------------------
# Public API - window parsing
# ---------------------------------------------------------------------------
def parse_auction_windows(raw: Any) -> tuple[tuple[str, str], ...]:
    """Validate and normalise a configured list of auction windows.

    Accepts a variety of input shapes so the JSON can stay flexible:

      * ``None``  -> falls back to :data:`DEFAULT_SME_AUCTION_WINDOWS`,
        or the ``POV_SME_AUCTION_WINDOWS`` env var if set.
      * ``str``   -> a comma-separated list of ``"HH:MM-HH:MM"`` entries.
      * ``list``  -> each entry may be a string ``"HH:MM-HH:MM"`` or a
        ``dict`` with ``start``/``from`` and ``end``/``to`` keys.

    Args:
        raw: A window spec (str, list, tuple, dict, or None).

    Returns:
        A tuple of ``(start, end)`` tuples, each as canonical
        ``"HH:MM"`` strings. Empty input that survives validation
        is treated as "no windows configured" -> empty tuple.

    Raises:
        ValueError: when an entry is malformed, the end is not after
            the start, or the input is structurally invalid.
    """
    if raw is None:
        env_value = os.getenv("POV_SME_AUCTION_WINDOWS")
        if not env_value:
            return DEFAULT_SME_AUCTION_WINDOWS
        raw = env_value
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("auction_windows must contain at least one window")

    windows: list[tuple[str, str]] = []
    for item in raw:
        # Pre-parsed pair: ((start, end),) - skip string parsing.
        if isinstance(item, (tuple, list)) and len(item) == 2 \
                and all(isinstance(x, str) for x in item):
            start_raw, end_raw = item[0], item[1]
        elif isinstance(item, dict):
            item = (
                f"{item.get('start', item.get('from', ''))}"
                f"-{item.get('end', item.get('to', ''))}"
            )
            match = _WINDOW_RE.match(str(item))
            if not match:
                raise ValueError("auction windows must use HH:MM-HH:MM")
            start_raw, end_raw = match.groups()
        else:
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

# ---------------------------------------------------------------------------
# Standard NSE SME/PCAS session: a single continuous window from the
# market open to close. Each tuple member is a ``"HH:MM"`` string.
DEFAULT_SME_AUCTION_WINDOWS: tuple[tuple[str, str], ...] = (("09:15", "15:30"),)

# A syntactically-valid window looks like "09:15-15:30" or
# "09:15-15:30" (a dash or a comma works; we normalise below).
_WINDOW_RE = re.compile(r"^\s*(\d{1,2}:\d{2})\s*[-,]\s*(\d{1,2}:\d{2})\s*$")



# ---------------------------------------------------------------------------
# Public API - window predicates
# ---------------------------------------------------------------------------
def is_within_auction_window(
    now: datetime | None = None,
    windows: Any = None,
) -> bool:
    """Return ``True`` if ``now`` falls inside any configured auction window.

    The "now" argument defaults to the current IST time so callers do
    not need to import timezone helpers. If the configured ``windows``
    is ``None`` the parser falls back to the default SME schedule.

    Args:
        now:     Optional reference instant (timezone-aware or naive).
        windows: Window spec (anything :func:`parse_auction_windows`
                 accepts). Defaults to the standard SME schedule.

    Returns:
        ``True`` when the current minute is inside a configured
        window, ``False`` otherwise.
    """
    from blue_engine.core.time_utils import ist_now

    current = now or ist_now()
    if current.tzinfo is not None:
        from zoneinfo import ZoneInfo
        current = current.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    minute = current.hour * 60 + current.minute
    parsed = parse_auction_windows(windows)
    return any(
        _minutes(start) <= minute <= _minutes(end) for start, end in parsed
    )


def current_auction_window(
    now: datetime | None = None,
    windows: Any = None,
) -> tuple[str, str] | None:
    """Return the configured window that contains ``now``, or ``None``.

    The return value is the raw ``(start, end)`` pair from the config,
    which makes it useful for log lines and audit trails.

    Args:
        now:     Optional reference instant.
        windows: Window spec (anything :func:`parse_auction_windows` accepts).

    Returns:
        The ``(start, end)`` tuple containing the current minute, or
        ``None`` if no window is active.
    """
    from blue_engine.core.time_utils import ist_now

    current = now or ist_now()
    if current.tzinfo is not None:
        from zoneinfo import ZoneInfo
        current = current.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    minute = current.hour * 60 + current.minute
    parsed = parse_auction_windows(windows)
    return next(
        (
            (start, end)
            for start, end in parsed
            if _minutes(start) <= minute <= _minutes(end)
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Public alias
# ---------------------------------------------------------------------------
def is_in_call_auction_window(
    now: datetime | None = None, windows: Any = None
) -> bool:
    """Compatibility alias for the periodic-auction window predicate.

    Same as :func:`is_within_auction_window`. Kept for callers that
    prefer the longer, more explicit spelling.
    """
    return is_within_auction_window(now, windows)


__all__ = [
    "DEFAULT_SME_AUCTION_WINDOWS",
    "parse_auction_windows",
    "is_within_auction_window",
    "is_in_call_auction_window",
    "current_auction_window",
]
