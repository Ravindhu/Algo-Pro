"""
nlp/event_calendar.py - known event-risk dates (budget, MPC, Fed, expiry).

Event risk is not directional - it is *uncertainty*. Around scheduled events
(RBI MPC, Union Budget, US Fed, monthly F&O expiry) realised volatility rises
and gap risk increases, so a disciplined desk trims gross *into* the event and
re-adds after. This module answers "is a known event inside the next N days?"
with a pure, dependency-free calendar.

Dates are **data**, not code: pass them in via config, or use the built-in
generator for the *recurring* ones (monthly expiry, MPC-cycle approximation).
Fixed dates (budget day) should be configured per year.

Purity: ``datetime`` only, no I/O, no external calendar API.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence


def _parse(d: Any) -> Optional[date]:
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _last_thursday(year: int, month: int) -> date:
    """Last Thursday of a month - the NSE F&O monthly expiry convention."""
    if month == 12:
        first_next = date(year + 1, 1, 1)
    else:
        first_next = date(year, month + 1, 1)
    d = first_next - timedelta(days=1)
    while d.weekday() != 3:  # 3 == Thursday
        d -= timedelta(days=1)
    return d


def monthly_expiries(as_of: date, *, months_ahead: int = 2) -> List[date]:
    """Upcoming monthly F&O expiries (last Thursday) from ``as_of``."""
    out: List[date] = []
    y, m = as_of.year, as_of.month
    for _ in range(months_ahead + 1):
        exp = _last_thursday(y, m)
        if exp >= as_of:
            out.append(exp)
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def upcoming_events(as_of: Optional[date] = None, *,
                    within_days: int = 10,
                    fixed_events: Optional[Sequence[Any]] = None,
                    include_expiry: bool = True,
                    mpc_dates: Optional[Sequence[Any]] = None
                    ) -> Dict[str, Any]:
    """Events inside the next ``within_days`` calendar days.

    Parameters
    ----------
    as_of : reference date (defaults to today).
    fixed_events : one-off configured dates (budget, etc.), any parseable form.
    mpc_dates : RBI policy dates (from config; RBI publishes them yearly).
    include_expiry : add monthly F&O expiries automatically.

    Returns::

        {"as_of": iso, "within_days": int, "events": [{"date","name","in_days"}],
         "nearest": {...}|None, "event_risk": 0.0..1.0}

    ``event_risk`` is a soft score in [0,1] that the engine can use to trim
    gross: 1.0 when a major scheduled event is 0-1 days away, decaying with
    distance. It is *uncertainty*, not direction.
    """
    today = _parse(as_of) if as_of else date.today()
    if today is None:
        today = date.today()
    horizon = today + timedelta(days=int(within_days))

    events: List[Dict[str, Any]] = []

    def _add(d: Optional[date], name: str, major: bool) -> None:
        if d is None or not (today <= d <= horizon):
            return
        events.append({"date": d.isoformat(), "name": name,
                       "in_days": (d - today).days, "major": major})

    for raw in (fixed_events or []):
        _add(_parse(raw), "fixed", True)
    for raw in (mpc_dates or []):
        _add(_parse(raw), "RBI_MPC", True)
    if include_expiry:
        for exp in monthly_expiries(today):
            _add(exp, "FNO_EXPIRY", False)

    events.sort(key=lambda e: e["in_days"])
    nearest = events[0] if events else None

    # Event-risk decay: 1.0 at day 0, ~0 at the horizon edge.
    risk = 0.0
    for e in events:
        weight = 1.0 if e["major"] else 0.6
        decay = max(0.0, 1.0 - (e["in_days"] / max(1, within_days)))
        risk = max(risk, weight * decay)

    return {"as_of": today.isoformat(), "within_days": int(within_days),
            "events": events, "nearest": nearest, "event_risk": round(risk, 4)}


def available() -> bool:
    return True


__all__ = ["upcoming_events", "monthly_expiries", "available"]