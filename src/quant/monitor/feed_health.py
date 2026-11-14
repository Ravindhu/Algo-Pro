"""Quote-feed health: stale-frame counting and a PAUSED latch. Wraps the engine.

Flow stage: Feed heartbeats and reconnect, Stale-feed and missing-bar checks
Status: PHASE 5 IMPLEMENTED (wrap; state only, never sends an order)
Existing code to reuse or wrap (do not copy):
    src/quant/blue_engine/core/quote_health.py  (QuoteHealthMonitor,
        quote_frame_is_valid, DEFAULT_QUOTE_FAILURE_THRESHOLD)

WHY THIS NODE WAS MISSING
-------------------------
v5 2.2 and checklist item 4 name feed heartbeats and reconnect handling, and
item 12 names stale-feed checks before signals. The bot had a bar reader that
returned nothing on stale data - correct, but with no counter, no latch and no
reason, so the dashboard could not distinguish "quiet market" from "feed died".

This module gives the pipeline that state: consecutive failures, a threshold,
and a PAUSED latch that clears on the first good frame.

WHAT THIS WRAPPER ADDS
----------------------
1. **A per-symbol view.** The engine monitor is one counter. The bot needs to
   know WHICH symbol's feed went stale, so `FeedHealthBook` keeps one monitor per
   symbol and reports the worst state.
2. **An explicit entry guard** (`blocks_entries`) that fails closed: a paused
   feed blocks new entries. Exits are never blocked - a stop must still work on
   the last known bar, which the exit path already handles separately.
3. **A dashboard snapshot** in a stable shape.

WHAT IT DOES NOT DO
-------------------
- It does not read quotes, reconnect sockets, or send anything. It only counts
  what the caller reports.
- It never blocks an exit.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

logger = logging.getLogger(__name__)

__all__ = [
    "FeedHealthUnavailable",
    "DEFAULT_THRESHOLD",
    "quote_is_valid",
    "FeedHealthBook",
]


class FeedHealthUnavailable(RuntimeError):
    """The engine's quote-health monitor cannot be loaded."""


def _module():
    try:
        return load("core.quote_health")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise FeedHealthUnavailable(f"engine quote health unavailable: {exc}") from exc


def _default_threshold() -> int:
    try:
        return int(_module().DEFAULT_QUOTE_FAILURE_THRESHOLD)
    except Exception:  # noqa: BLE001
        return 3


DEFAULT_THRESHOLD = _default_threshold()


def quote_is_valid(quote_resp: Any) -> bool:
    """Engine's validity rule: status, ltp>0, depth present. False when unknown."""
    try:
        return bool(_module().quote_frame_is_valid(quote_resp))
    except FeedHealthUnavailable:
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("quote_frame_is_valid failed: %r", exc)
        return False


class FeedHealthBook:
    """One quote-health monitor per symbol, plus an aggregate view.

    Not thread-safe by design: the bot calls it from its single cycle loop. The
    underlying engine monitors each carry their own lock, so concurrent reads of
    ``snapshot`` remain safe.
    """

    def __init__(self, threshold: Optional[int] = None) -> None:
        self.threshold = int(threshold if threshold is not None else DEFAULT_THRESHOLD)
        self._monitors: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._unavailable = False

    def _monitor(self, symbol: str):
        """Lazily create the engine monitor for one symbol."""
        with self._lock:
            existing = self._monitors.get(symbol)
            if existing is not None:
                return existing
            try:
                monitor = _module().QuoteHealthMonitor(threshold=self.threshold)
            except FeedHealthUnavailable as exc:
                if not self._unavailable:
                    logger.warning("feed health unavailable, not tracking: %s", exc)
                    self._unavailable = True
                return None
            self._monitors[symbol] = monitor
            return monitor

    def record_success(self, symbol: str) -> None:
        """A good frame arrived: reset that symbol's counter and clear PAUSED."""
        monitor = self._monitor(symbol)
        if monitor is not None:
            monitor.record_success()

    def record_failure(self, symbol: str, reason: str) -> bool:
        """A bad/stale frame. True when this frame crossed the threshold."""
        monitor = self._monitor(symbol)
        if monitor is None:
            return False
        newly_paused = bool(monitor.record_failure(reason))
        if newly_paused:
            logger.warning("feed PAUSED for %s after %d failures: %s",
                           symbol, self.threshold, reason)
        return newly_paused

    def observe(self, symbol: str, quote_resp: Any, reason: str = "invalid frame") -> bool:
        """Check a raw quote response and record the outcome. True if it was good."""
        if quote_is_valid(quote_resp):
            self.record_success(symbol)
            return True
        self.record_failure(symbol, reason)
        return False

    def is_paused(self, symbol: str) -> bool:
        """True when that symbol's feed has latched PAUSED."""
        monitor = self._monitors.get(symbol)
        return bool(monitor and monitor.should_pause())

    def blocks_entries(self, symbols=None) -> Dict[str, Any]:
        """Entry guard. Fails closed when feed health cannot be tracked.

            {"blocked": bool, "paused_symbols": [...], "detail": str}

        Exits are never covered by this: a stop must work on the last known bar.
        """
        if self._unavailable:
            return {"blocked": True, "paused_symbols": [],
                    "detail": "feed health unavailable (fails closed)"}
        targets = list(symbols) if symbols is not None else list(self._monitors)
        paused = [s for s in targets if self.is_paused(s)]
        if paused:
            return {"blocked": True, "paused_symbols": paused,
                    "detail": f"feed paused for {', '.join(paused)}"}
        return {"blocked": False, "paused_symbols": [], "detail": ""}

    def snapshot(self) -> Dict[str, Any]:
        """Stable shape for the dashboard. Never raises."""
        rows = {}
        for symbol, monitor in self._monitors.items():
            try:
                rows[symbol] = dict(monitor.snapshot())
            except Exception as exc:  # noqa: BLE001
                rows[symbol] = {"paused": True, "failures": None, "threshold": self.threshold,
                                "last_reason": f"snapshot failed: {exc}"}
        paused = [s for s, r in rows.items() if r.get("paused")]
        return {
            "available": not self._unavailable,
            "threshold": self.threshold,
            "tracked": len(rows),
            "paused": paused,
            "symbols": rows,
            "detail": ("feed health unavailable" if self._unavailable
                       else (f"paused: {', '.join(paused)}" if paused else "all feeds ok")),
        }
