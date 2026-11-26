"""Route bar and daily-stat reads to the exchange a stock is listed on (slice 4, 2026-11-25).

Status: PHASE 3 IMPLEMENTED (exchange routing, slice 4)

Owner rule: the signal uses NSE bars for an NSE-listed stock, and BSE bars only for a
BSE-only stock. A position's exit checks use the exchange it is held on.

Status: IMPLEMENTED (tests in tests/unit/test_exchange_bars.py). Plan:
docs/IMPLEMENTATION_PLAN_2026-11-25_BOT_EXCHANGE_CARRY.md

Read only. Every method returns None when the exchange is unknown or its feed is unusable.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Dict, Optional

#: ``listing(symbol) -> "NSE" | "BSE" | None``
Listing = Callable[[str], Optional[str]]


class ExchangeBars:
    """Wraps one bar reader per exchange, e.g. ``{"NSE": LiveBars(..), "BSE": LiveBars(.., exchange="BSE")}``."""

    # audit fix 7 (2026-11-26): listing is held for a SHORT window, not all day.
    # A one-sided book at the open used to lock the symbol to one exchange for the day.
    LISTING_TTL_SECONDS = 300.0

    def __init__(self, bars_by_exchange: Dict[str, object], listing: Listing) -> None:
        self._by_exchange = dict(bars_by_exchange)
        self._listing = listing
        self._cache: Dict[str, tuple] = {}  # symbol -> (listing, epoch when recorded)

    def _exchange_for(self, symbol: str, now: datetime, exchange: Optional[str]) -> Optional[str]:
        if exchange is not None:
            return exchange if exchange in self._by_exchange else None
        stamp = now.timestamp()
        hit = self._cache.get(symbol)
        if hit is None or stamp - hit[1] > self.LISTING_TTL_SECONDS:
            try:
                value = self._listing(symbol)
            except Exception:  # noqa: BLE001 - no listing = no signal data (fail closed)
                value = None
            # A failed check is retried on the next read; only a real answer is held for the TTL.
            if value is not None:
                self._cache[symbol] = (value, stamp)
            else:
                self._cache.pop(symbol, None)
                return None
        return self._cache[symbol][0] if self._cache[symbol][0] in self._by_exchange else None

    def bars(self, symbol: str, now: datetime, exchange: Optional[str] = None):
        ex = self._exchange_for(symbol, now, exchange)
        return None if ex is None else self._by_exchange[ex].bars(symbol, now)

    def daily_stats(self, symbol: str, now: datetime, exchange: Optional[str] = None) -> Optional[Dict[str, float]]:
        ex = self._exchange_for(symbol, now, exchange)
        return None if ex is None else self._by_exchange[ex].daily_stats(symbol, now)
