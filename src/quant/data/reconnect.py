"""Feed reconnect with exponential backoff. Pure schedule; connect and sleep are passed in.

Flow stage: Feed health monitor
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): none (new, flow item 4)
"""
from __future__ import annotations

from typing import Callable, List, Optional


def backoff_delays(base: float = 1.0, factor: float = 2.0, cap: float = 60.0, attempts: int = 10) -> List[float]:
    """Delays in seconds: base, base*factor, ... each capped at `cap`."""
    delays: List[float] = []
    delay = base
    for _ in range(attempts):
        delays.append(min(delay, cap))
        delay *= factor
    return delays


def reconnect(
    connect: Callable[[], None],
    sleep: Callable[[float], None],
    delays: Optional[List[float]] = None,
    alerts=None,
    component: str = "feed",
) -> bool:
    """Try connect() once, then once per delay. Returns True on success, False after giving up."""
    schedule = backoff_delays() if delays is None else delays
    for attempt, delay in enumerate(schedule, start=1):
        try:
            connect()
            return True
        except Exception as exc:
            if alerts is not None:
                alerts.emit("WARNING", component, f"reconnect attempt {attempt} failed: {exc}")
            sleep(delay)
    try:
        connect()
        return True
    except Exception as exc:
        if alerts is not None:
            alerts.emit("ERROR", component, f"reconnect gave up after {len(schedule) + 1} tries: {exc}")
        return False
