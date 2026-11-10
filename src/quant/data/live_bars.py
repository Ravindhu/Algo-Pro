"""Live bars and daily stats from OpenAlgo read-only history. Fails closed.

Flow stage: Market inputs, Data and features, Stale-feed and missing-bar checks
Status: PHASE 3 IMPLEMENTED (read only; no order route; returns None on any doubt)

Rules:
- Any error, non-success reply, missing column, too few bars, or stale last bar gives None.
  The caller then skips the symbol. Nothing is guessed.
- The last bar in the window may still be forming. The strategies read closed bars only,
  so this module does not trim it.
- Daily stats (ADV, previous close) use only days before today, so today's volume is never
  used to size or to check the same day (no look-ahead).

Assumption to confirm on the live account: OpenAlgo accepts interval "D" for daily bars and
returns timestamps as strings with a date or datetime column.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import pandas as pd

IST_NAME = "Asia/Kolkata"
TIME_KEYS = ("datetime", "timestamp", "date", "time")
NEEDED = ("open", "high", "low", "close", "volume")
MIN_BARS = 30
ADV_DAYS = 20


def rows_to_frame(rows: Any) -> Optional[pd.DataFrame]:
    """Raw OpenAlgo rows -> DataFrame indexed by naive IST time. None if the rows are unusable."""
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    keys = set(rows[0].keys())
    ts_key = next((k for k in TIME_KEYS if k in keys), None)
    if ts_key is None or not set(NEEDED) <= keys:
        return None
    df = pd.DataFrame(rows)
    idx = pd.to_datetime(df[ts_key], errors="coerce")
    if getattr(idx.dt, "tz", None) is not None:
        idx = idx.dt.tz_convert(IST_NAME).dt.tz_localize(None)
    df = df[list(NEEDED)].apply(pd.to_numeric, errors="coerce")
    df.index = pd.DatetimeIndex(idx)
    df = df[df.index.notna()].dropna().sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if df.empty:
        return None
    return df


def is_fresh(df: pd.DataFrame, now: datetime, max_age_minutes: float) -> bool:
    """True when the last bar is not older than max_age_minutes at `now` (naive IST)."""
    last = df.index[-1]
    return (now - last.to_pydatetime()) <= timedelta(minutes=max_age_minutes) and last <= pd.Timestamp(now)


class LiveBars:
    """Wraps the read-only client for one exchange and interval. Every method returns None on doubt."""

    def __init__(self, reader, api_key: str, exchange: str = "NSE", interval: str = "5m",
                 lookback_days: int = 5, max_bar_age_minutes: float = 15.0) -> None:
        if not api_key:
            raise ValueError("LiveBars needs an API key")
        self._reader = reader
        self._key = api_key
        self.exchange = exchange
        self.interval = interval
        self.lookback_days = int(lookback_days)
        self.max_bar_age_minutes = float(max_bar_age_minutes)

    def _window(self, symbol: str, interval: str, days: int, now: datetime) -> Optional[pd.DataFrame]:
        start = (now - timedelta(days=days)).date().isoformat()
        end = now.date().isoformat()
        try:
            reply = self._reader.history(self._key, symbol, self.exchange, interval, start, end)
        except Exception:
            return None
        if not isinstance(reply, dict) or reply.get("status") != "success":
            return None
        return rows_to_frame(reply.get("data"))

    def bars(self, symbol: str, now: datetime) -> Optional[pd.DataFrame]:
        """Intraday bars for the strategies, or None when the feed is unusable or stale."""
        df = self._window(symbol, self.interval, self.lookback_days, now)
        if df is None or len(df) < MIN_BARS:
            return None
        if not is_fresh(df, now, self.max_bar_age_minutes):
            return None
        return df

    def daily_stats(self, symbol: str, now: datetime) -> Optional[Dict[str, float]]:
        """{'adv_shares', 'prev_close'} from days before today only. None if not enough history."""
        df = self._window(symbol, "D", ADV_DAYS * 2, now)
        if df is None:
            return None
        past = df[df.index.date < now.date()]
        if len(past) < 5:
            return None
        adv = float(past["volume"].tail(ADV_DAYS).mean())
        prev_close = float(past["close"].iloc[-1])
        if adv <= 0 or prev_close <= 0:
            return None
        return {"adv_shares": adv, "prev_close": prev_close}
