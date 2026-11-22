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
                     "30m": "30m", "75m": "5m", "125m": "5m", "1h": "1h", "1d": "D", "1w": "W", "1M": "M"}
# 75m and 125m are not OpenAlgo intervals: they are built from 5m bars, anchored at 09:15 each day (2026-11-22).
AGGREGATE_MINUTES = {"75m": 75, "125m": 125}
SESSION_OPEN_HOUR, SESSION_OPEN_MINUTE = 9, 15
MINUTES = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "75m": 75, "125m": 125,
           "1h": 60, "1d": 1440, "1w": 10080, "1M": 43200}
LOOKBACK_DAYS = {"1d": 120, "1w": 400, "1M": 1200, "75m": 15, "125m": 20}  # daily ~120 days; weekly ~30 weeks; monthly ~30 months; 75m/125m ~30 bars
DAILY_LIKE = ("1d", "1w", "1M")  # bars with no intraday end time: closed by calendar date, not by minutes
DEFAULT_LOOKBACK_DAYS = 5


def aggregate_session(df5: pd.DataFrame, minutes: int) -> Optional[pd.DataFrame]:
    """Complete `minutes`-long bars built from 5m bars, anchored at 09:15 each day (the NSE session open).
    A bar is kept only when all of its 5m bars are present, so a partial bar never reaches a strategy.
    The 5m bar still forming must already be removed by the caller."""
    if df5 is None or df5.empty:
        return None
    k = minutes // 5
    pieces = []
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    for day, g in df5.groupby(df5.index.date):
        origin = pd.Timestamp(day) + pd.Timedelta(hours=SESSION_OPEN_HOUR, minutes=SESSION_OPEN_MINUTE)
        rs = g.resample(f"{minutes}min", origin=origin, label="left", closed="left")
        bars = rs.agg(agg)
        counts = rs["close"].count()
        pieces.append(bars[counts == k].dropna())
    out = pd.concat(pieces) if pieces else pd.DataFrame()
    return out if not out.empty else None


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
        df = rows_to_frame(reply.get("data"))
        if df is None or label not in AGGREGATE_MINUTES:
            return df
        return aggregate_session(df.iloc[:-1], AGGREGATE_MINUTES[label])  # drop the 5m bar still forming

    def _closed(self, symbol: str, label: str, now: datetime) -> Optional[pd.DataFrame]:
        df = self._window(symbol, label, now)
        if df is None:
            return None
        if label in DAILY_LIKE:
            # A bar is closed when its date is before the start of the current period: today for daily,
            # this week's Monday for weekly, the 1st of this month for monthly. This holds whichever day
            # the vendor dates the bar with.
            if label == "1d":
                period_start = now.date()
                fresh_from = None  # daily: freshness checked below by the stale-days limit
            elif label == "1w":
                period_start = now.date() - timedelta(days=now.date().weekday())
                fresh_from = period_start - timedelta(days=7)  # the last closed bar must be the previous week's
            else:
                period_start = now.date().replace(day=1)
                fresh_from = (period_start - timedelta(days=1)).replace(day=1)  # ... and the previous month's
            closed = df[df.index.date < period_start]
            if len(closed) < MIN_BARS:
                return None
            if label == "1d":
                if (now.date() - closed.index[-1].date()).days > self._daily_stale:
                    return None
            elif closed.index[-1].date() < fresh_from:
                return None
            return closed
        # 75m/125m frames come back already without the forming bar and with complete buckets only
        closed = df if label in AGGREGATE_MINUTES else df.iloc[:-1]  # the last row is the bar still forming
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
