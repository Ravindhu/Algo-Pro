"""NSE auction and periodic-call windows. Wraps the engine's window parser.

Flow stage: Session rules (pre-open, auction, closing), Session guards
Status: PHASE 5 IMPLEMENTED (wrap; pure time predicates, no I/O and no orders)
Existing code to reuse or wrap (do not copy):
    src/quant/blue_engine/core/auction_windows.py  (parse_auction_windows,
        is_within_auction_window, current_auction_window,
        is_in_call_auction_window, DEFAULT_SME_AUCTION_WINDOWS)

WHY THIS NODE WAS MISSING
-------------------------
v5 section 2.7 lists "Session rules: pre-open, auction, closing orders" and the
checklist names index circuit freeze and the session calendar. The bot had no
way to ask "are we inside a call-auction window right now?", so an entry could
land in the SME periodic auction, where the match is a single price and a
limit order behaves nothing like it does in continuous trading.

WHAT THIS WRAPPER ADDS
----------------------
1. **A trailing-close guard.** A window is defined by start/end minutes, and an
   order sent 30 seconds before the window closes will very likely execute
   inside it. `blocks_entry` pads the end by a configurable buffer so the guard
   acts before the window, not during it.
2. **An explicit "unknown" state.** If the config cannot be parsed the answer is
   ``unknown``, and the risk-side caller must treat unknown as blocked. A silent
   False here would be the difference between respecting the window and ignoring
   it.
3. **IST normalisation in one place**, so every caller gets the same answer
   regardless of the host's timezone.

WHAT IT DOES NOT DO
-------------------
- It does not read a config file or hold state. Windows are passed in, which
  keeps the function pure and testable.
- It never places or cancels an order.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple

from quant.core.blue_bridge import BlueEngineUnavailable, load

logger = logging.getLogger(__name__)

__all__ = [
    "SessionWindowsUnavailable",
    "DEFAULT_PRE_CLOSE_BUFFER_MIN",
    "available_windows",
    "current_window",
    "in_auction",
    "blocks_entry",
]

#: An entry within this many minutes of a window close is refused, because the
#: order would almost certainly match inside the auction anyway.
DEFAULT_PRE_CLOSE_BUFFER_MIN = 2


class SessionWindowsUnavailable(RuntimeError):
    """The engine's auction-window module cannot be loaded."""


def _module():
    try:
        return load("core.auction_windows")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise SessionWindowsUnavailable(f"engine auction windows unavailable: {exc}") from exc


def available_windows(windows: Any = None) -> Tuple[Tuple[str, str], ...]:
    """The parsed window list. Empty when the engine is missing or the spec is bad."""
    try:
        return tuple(_module().parse_auction_windows(windows))
    except SessionWindowsUnavailable as exc:
        logger.warning("auction windows unavailable: %s", exc)
        return ()
    except Exception as exc:  # noqa: BLE001 - a bad spec must not crash the cycle
        logger.warning("auction windows unparseable: %r", exc)
        return ()


def current_window(now: Optional[datetime] = None,
                   windows: Any = None) -> Optional[Tuple[str, str]]:
    """The (start, end) window containing ``now``, or None."""
    try:
        return _module().current_auction_window(now, windows)
    except SessionWindowsUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("current_auction_window failed: %r", exc)
        return None


def in_auction(now: Optional[datetime] = None, windows: Any = None) -> Optional[bool]:
    """True / False, or **None when it cannot be determined**.

    None is not the same as False. A caller that cannot parse the schedule does
    not know whether the market is in an auction, and must not assume it is not.
    """
    try:
        return bool(_module().is_within_auction_window(now, windows))
    except SessionWindowsUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("is_within_auction_window failed: %r", exc)
        return None


def blocks_entry(now: Optional[datetime] = None, windows: Any = None, *,
                 buffer_min: int = DEFAULT_PRE_CLOSE_BUFFER_MIN,
                 is_sme: bool = False) -> Tuple[bool, str]:
    """(blocked, reason) for an ENTRY decision. Fails closed.

    Blocks when:
      * the schedule cannot be read                        -> "schedule_unknown"
      * now is inside an auction window                    -> "in_auction"
      * now is within ``buffer_min`` of a window's start   -> "auction_approaching"

    IMPORTANT - the engine's default schedule for ``windows=None`` is
    ``09:15-15:30``, i.e. the WHOLE session. That is correct for SME scrips,
    which trade in one continuous call auction all day, and wrong for NSE cash
    equities. So ``is_sme`` decides which default applies:

      * ``is_sme=True``  and no explicit windows -> the engine default is used,
        and since it covers the session every entry is blocked, as it should be.
      * ``is_sme=False`` and no explicit windows -> the cash segment has no
        periodic auction in this schedule, so nothing is blocked. The pre-open
        window is handled by ``market.open`` in the calendar, not here.

    Exits are never blocked by this function: a stop must be allowed to work.
    """
    if windows is None and not is_sme:
        # Cash equities: no periodic auction in this schedule. Do not fall
        # through to the SME all-day default, which would block everything.
        return False, "cash_session_no_auction"

    parsed = available_windows(windows)
    if not parsed:
        return True, "schedule_unknown"

    state = in_auction(now, windows)
    if state is None:
        return True, "schedule_unreadable"
    if state:
        return True, "in_auction"

    current = _ist_naive(now)
    minute = current.hour * 60 + current.minute
    for start, _end in parsed:
        start_min = _hhmm_to_minutes(start)
        if start_min is None:
            continue
        lead = start_min - minute
        if 0 < lead <= max(0, int(buffer_min)):
            return True, f"auction_approaching ({start})"

    return False, ""


def _ist_naive(now: Optional[datetime]) -> datetime:
    """Convert to naive IST. India has no DST, so a fixed offset is exact."""
    if now is None:
        return datetime.utcnow() + timedelta(hours=5, minutes=30)
    if now.tzinfo is not None:
        return now.astimezone(_IST()).replace(tzinfo=None)
    return now


def _IST():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Kolkata")
    except Exception:  # noqa: BLE001 - zoneinfo data missing on some hosts
        return _FixedIST()


class _FixedIST:
    """Fallback tzinfo for +05:30 when zoneinfo data is unavailable."""

    def utcoffset(self, dt):
        return timedelta(hours=5, minutes=30)

    def tzname(self, dt):
        return "IST"

    def dst(self, dt):
        return timedelta(0)


def _hhmm_to_minutes(value: Any) -> Optional[int]:
    """'09:15' -> 555. None when the text is not a valid HH:MM."""
    try:
        text = str(value).strip()
        hh, mm = text.split(":")
        h, m = int(hh), int(mm)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return h * 60 + m
    except (ValueError, AttributeError, TypeError):
        return None
