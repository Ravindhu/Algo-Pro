"""Event calendar wrapper: known event-risk dates, DELEGATED to Blue_engine.

Flow stage: Event calendar
Status: PHASE 5 IMPLEMENTED (wrap; pure calendar; blocks new entries, never places or sizes an order)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/economics/nlp/event_calendar.py
(upcoming_events)

Event risk is uncertainty, not direction. This wrapper answers one question for the
entry gate: is a major event (budget, RBI policy, Fed) inside the next N days?
If so, new entries are blocked. Monthly F&O expiry is reported but does NOT block.
Exits are never blocked by this check.

FAIL-CLOSED RULES
-----------------
- Blue cannot load, or the call raises: ``blocked=True`` (no new entries).
- Event dates come from the caller's config. The wrapper fetches nothing.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable, Dict, Optional, Sequence

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["event_window", "DEFAULT_WITHIN_DAYS"]

#: Entry blackout horizon in calendar days, unless the caller sets another.
DEFAULT_WITHIN_DAYS = 2


def _upcoming() -> Optional[Callable[..., Dict[str, Any]]]:
    try:
        return load("economics.nlp.event_calendar").upcoming_events
    except BlueEngineUnavailable:
        return None


def event_window(as_of: Optional[date] = None, *,
                 within_days: int = DEFAULT_WITHIN_DAYS,
                 fixed_events: Optional[Sequence[Any]] = None,
                 mpc_dates: Optional[Sequence[Any]] = None,
                 include_expiry: bool = True) -> Dict[str, Any]:
    """Return ``{ok, blocked, events, nearest, event_risk, reason}``. Never raises.

    ``blocked`` is True when a MAJOR event falls inside the window. ``ok`` False
    means the calendar could not be read, and ``blocked`` is then True.
    """
    failed = {"ok": False, "blocked": True, "events": [], "nearest": None,
              "event_risk": 1.0}
    fn = _upcoming()
    if fn is None:
        return {**failed, "reason": "Blue event calendar unavailable: entries blocked"}
    try:
        result = fn(as_of, within_days=within_days, fixed_events=fixed_events,
                    include_expiry=include_expiry, mpc_dates=mpc_dates)
    except Exception as exc:  # noqa: BLE001 - the wrapper must not raise
        return {**failed, "reason": f"event calendar failed: {exc!r}: entries blocked"}
    if not isinstance(result, dict):
        return {**failed, "reason": "event calendar returned no mapping: entries blocked"}
    events = list(result.get("events") or [])
    major = [e for e in events if e.get("major")]
    return {
        "ok": True,
        "blocked": bool(major),
        "events": events,
        "nearest": result.get("nearest"),
        "event_risk": float(result.get("event_risk") or 0.0),
        "reason": f"major event in {within_days} days: entries blocked" if major else "",
    }
