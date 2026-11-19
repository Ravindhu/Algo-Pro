"""Closed bars for the three trade setup timeframes (MTF, HTF, LTF). Read only, fails closed.

Flow stage: Market inputs, Data and features, Closed bars MTF/HTF/LTF
Status: PHASE 1 IMPLEMENTED (2026-11-19). Not wired into the bot cycle yet.

- Labels come from the trade setup: 1m 3m 5m 10m 15m 30m 1h 1d. OpenAlgo calls daily "D".
- Only CLOSED bars are returned. The bar still forming is dropped (intraday: last row; daily: today).
- Intraday: the last closed bar must have ended within max_bar_age_minutes of now.
- Daily: the last closed day must be at most daily_max_stale_days old (weekends and holidays).
- Any error, missing data, too few bars or stale data gives None for the whole set. Nothing is guessed.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Optional

import pandas as pd

from quant.data.live_bars import MIN_BARS, rows_to_frame

ROLES = ("mtf", "htf", "ltf")
OPENALGO_INTERVAL = {"1m": "1m", "3m": "3m", "5m": "5m", "10m": "10m", "15m": "15m",
                     "30m": "30m", "1h": "1h", "1d": "D"}
MINUTES = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}
LOOKBACK_DAYS = {"1d": 120}  # daily needs about 120 calendar days for 30+ trading days
DEFAULT_LOOKBACK_DAYS = 5


class MultiTimeframeBars:
    """Loads closed bars for the MTF, HTF and LTF labels through a read-only history reader."""

    def __init__(self, reader, api_key: str, timeframes: Dict[str, str], exchange: str = "NSE",
                 max_bar_age_minutes: float = 15.0, daily_max_stale_days: int = 4) -> None:
        if not api_key:
            raise ValueError("MultiTimeframeBars needs an API key")
        for role in ROLES:
            if timeframes.get(role) not in OPENALGO_INTERVAL:
                raise ValueError(f"{role} timeframe must be one of: {', '.join(OPENALGO_INTERVAL)}")
        self._reader = reader
        self._key = api_key
        self._exchange = exchange
        self._labels = {role: timeframes[role] for role in ROLES}
        self._max_age = float(max_bar_age_minutes)
        self._daily_stale = int(daily_max_stale_days)

    def _window(self, symbol: str, label: str, now: datetime) -> Optional[pd.DataFrame]:
        days = LOOKBACK_DAYS.get(label, DEFAULT_LOOKBACK_DAYS)
        start = (now - timedelta(days=days)).date().isoformat()
        end = now.date().isoformat()
        try:
            reply = self._reader.history(self._key, symbol, self._exchange,
                                         OPENALGO_INTERVAL[label], start, end)
        except Exception:
            return None
        if not isinstance(reply, dict) or reply.get("status") != "success":
            return None
        return rows_to_frame(reply.get("data"))

    def _closed(self, symbol: str, label: str, now: datetime) -> Optional[pd.DataFrame]:
        df = self._window(symbol, label, now)
        if df is None:
            return None
        if label == "1d":
            closed = df[df.index.date < now.date()]
            if len(closed) < MIN_BARS:
                return None
            if (now.date() - closed.index[-1].date()).days > self._daily_stale:
                return None
            return closed
        closed = df.iloc[:-1]  # the last row is the bar still forming
        if len(closed) < MIN_BARS:
            return None
        closed_end = closed.index[-1] + timedelta(minutes=MINUTES[label])
        age = now - closed_end
        if not (timedelta(0) <= age <= timedelta(minutes=self._max_age)):
            return None
        return closed

    def closed_frames(self, symbol: str, now: datetime) -> Optional[Dict[str, pd.DataFrame]]:
        """{"mtf": df, "htf": df, "ltf": df} with closed bars only, or None if any feed is unusable."""
        out: Dict[str, pd.DataFrame] = {}
        for role in ROLES:
            df = self._closed(symbol, self._labels[role], now)
            if df is None:
                return None
            out[role] = df
        return out
