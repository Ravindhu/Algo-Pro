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
    from quant.core.time_ist import naive_ist  # audit 2026-11-26: an aware clock is converted, never compared raw
    now = naive_ist(now)
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
        """{'adv_shares', 'prev_close', 'lot_size'} from days before today only.

        lot_size comes from the broker's instrument master through OpenAlgo (search_symbol);
        it differs per segment and is 1 for most NSE cash equities but larger for SME-listed
        stocks. If no usable lot size is found the value is 0, and the bot refuses the entry
        (fail closed). A guessed 1 would size SME orders wrongly (review gap 2026-11-26).
        """
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
        return {"adv_shares": adv, "prev_close": prev_close, "lot_size": self._lot_size(symbol)}

    def _lot_size(self, symbol: str) -> int:
        """Broker lot size: OpenAlgo /symbol (exact) first, then /search. 0 = unknown (refuse)."""
        for fetch in (self._symbol_reply, self._search_reply):
            try:
                reply = fetch(symbol)
            except Exception:  # noqa: BLE001 - a failed lookup must not stop the bot; try the next source
                continue
            lot = _lot_from_reply(reply, symbol)
            if lot is not None:
                return lot
        return 0

    def _symbol_reply(self, symbol: str):
        return self._reader.symbol_info(self._key, symbol, self.exchange)

    def _search_reply(self, symbol: str):
        return self._reader.search_symbol(self._key, symbol, self.exchange)
        if not isinstance(reply, dict) or reply.get("status") != "success":
            return 1
        rows = reply.get("data")
        if not isinstance(rows, list):
            return 1
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("symbol", "")).upper() != symbol.upper():
                continue
            lot = row.get("lotsize", row.get("lot_size"))
            try:
                lot = int(lot)
            except (TypeError, ValueError):
                continue
            if lot >= 1:
                return lot
        return 1


def _lot_from_reply(reply, symbol: str):
    """Lot size from an OpenAlgo reply (a row list or one record). None when unusable."""
    if not isinstance(reply, dict) or reply.get("status") != "success":
        return None
    data = reply.get("data")
    rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_symbol = str(row.get("symbol", "") or "").upper()
        if row_symbol and row_symbol != symbol.upper():
            continue
        lot = row.get("lotsize", row.get("lot_size"))
        try:
            lot = int(lot)
        except (TypeError, ValueError):
            continue
        if lot >= 1:
            return lot
    return None

