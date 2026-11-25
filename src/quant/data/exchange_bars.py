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

    def __init__(self, bars_by_exchange: Dict[str, object], listing: Listing) -> None:
        self._by_exchange = dict(bars_by_exchange)
        self._listing = listing
        self._day: Optional[object] = None
        self._cache: Dict[str, Optional[str]] = {}

    def _exchange_for(self, symbol: str, now: datetime, exchange: Optional[str]) -> Optional[str]:
        if exchange is not None:
            return exchange if exchange in self._by_exchange else None
        if self._day != now.date():  # listing is re-checked once per trading day
            self._cache, self._day = {}, now.date()
        if symbol not in self._cache:
            try:
                self._cache[symbol] = self._listing(symbol)
            except Exception:  # noqa: BLE001 - no listing = no signal data (fail closed)
                self._cache[symbol] = None
        return self._cache[symbol] if self._cache[symbol] in self._by_exchange else None

    def bars(self, symbol: str, now: datetime, exchange: Optional[str] = None):
        ex = self._exchange_for(symbol, now, exchange)
        return None if ex is None else self._by_exchange[ex].bars(symbol, now)

    def daily_stats(self, symbol: str, now: datetime, exchange: Optional[str] = None) -> Optional[Dict[str, float]]:
        ex = self._exchange_for(symbol, now, exchange)
        return None if ex is None else self._by_exchange[ex].daily_stats(symbol, now)
