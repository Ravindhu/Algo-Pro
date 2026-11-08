"""1-minute bars to the strategy frames (5-minute, 15-minute), plus ADV from daily bars. Pure, no network.

Flow stage: Market inputs (bar build)
Status: PHASE 2 IMPLEMENTED (paper only, 2026-11-08; not live).

Conventions:
- Bars come from OpenAlgo history as IST timestamps. Each 1-minute bar is stamped with its START minute.
- Intraday buckets start at the NSE open (09:15 IST), so a 5-minute bucket is 09:15, 09:20, ... and a
  15-minute bucket is 09:15, 09:30, ...
- Only CLOSED minutes are used. The minute that is still running is never used. The frame's LAST row is
  the bucket still forming (it may be partial or empty). Strategies drop that row with closed().
- Fail closed: if the previous minute's bar is missing, the feed raises StaleData and the cycle is skipped.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable, Dict, Iterable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

OPEN_MINUTE = 9 * 60 + 15                     # 09:15 IST
INTERVAL_MINUTES = {"1minute": 1, "5minute": 5, "15minute": 15}
OHLCV = ["open", "high", "low", "close", "volume"]


class StaleData(RuntimeError):
    """The previous minute's bar is missing, so the frames would be out of date."""


def normalize_1m(df: pd.DataFrame) -> pd.DataFrame:
    """Sorted, de-duplicated 1-minute frame with numeric OHLCV columns and a naive IST DatetimeIndex."""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], name="timestamp"), dtype=float)
    out = df.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)
    if out.index.tz is not None:
        out.index = out.index.tz_convert("Asia/Kolkata").tz_localize(None)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    for col in OHLCV:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out[OHLCV].dropna(subset=["open", "high", "low", "close"])


def bucket_start(ts: datetime, minutes: int) -> pd.Timestamp:
    """Start of the intraday bucket (aligned to 09:15) that contains ts."""
    ts = pd.Timestamp(ts)
    minute_of_day = ts.hour * 60 + ts.minute
    offset = minute_of_day - OPEN_MINUTE
    if offset < 0:
        raise ValueError(f"{ts} is before the session open")
    start_offset = (offset // minutes) * minutes
    return ts.normalize() + pd.Timedelta(minutes=OPEN_MINUTE + start_offset)


def aggregate(df1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """OHLCV buckets of `minutes` minutes, aligned to 09:15. Empty input gives an empty frame."""
    if len(df1m) == 0:
        return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], name="timestamp"), dtype=float)
    starts = [bucket_start(t, minutes) for t in df1m.index]
    grouped = df1m.groupby(pd.DatetimeIndex(starts, name="timestamp"))
    out = pd.DataFrame({
        "open": grouped["open"].first(),
        "high": grouped["high"].max(),
        "low": grouped["low"].min(),
        "close": grouped["close"].last(),
        "volume": grouped["volume"].sum(),
    })
    return out


def current_minute(now: datetime) -> pd.Timestamp:
    return pd.Timestamp(now).floor("min")


def build_frames(df1m: pd.DataFrame, now: datetime,
                 intervals: Sequence[str] = ("5minute", "15minute")) -> Dict[str, pd.DataFrame]:
    """Frames per interval. Completed buckets first, then the bucket still forming as the last row.

    Raises StaleData when the previous minute's bar is missing (fail closed)."""
    bars = normalize_1m(df1m)
    now_min = current_minute(now)
    last_closed_min = now_min - pd.Timedelta(minutes=1)
    closed_bars = bars[bars.index <= last_closed_min]
    if len(closed_bars) == 0 or closed_bars.index[-1] != last_closed_min:
        raise StaleData(f"no bar for {last_closed_min} yet; last bar is "
                        f"{closed_bars.index[-1] if len(closed_bars) else 'none'}")
    frames: Dict[str, pd.DataFrame] = {}
    for name in intervals:
        minutes = INTERVAL_MINUTES[name]
        forming_start = bucket_start(now_min, minutes)
        completed = closed_bars[closed_bars.index < forming_start]
        forming = closed_bars[(closed_bars.index >= forming_start)]
        done = aggregate(completed, minutes)
        if len(forming):
            row = aggregate(forming, minutes)
        else:
            row = pd.DataFrame([[np.nan] * 5], columns=OHLCV,
                               index=pd.DatetimeIndex([forming_start], name="timestamp"))
        frames[name] = pd.concat([done, row])
    return frames


def adv_from_daily(daily: pd.DataFrame, today: date, window: int = 20, min_days: int = 5) -> Optional[float]:
    """Average daily volume (shares) over the last `window` completed days before today.
    Returns None when fewer than `min_days` days exist (the gate then rejects: ADV unknown)."""
    if daily is None or len(daily) == 0 or "volume" not in daily:
        return None
    d = daily.copy()
    if not isinstance(d.index, pd.DatetimeIndex):
        d.index = pd.to_datetime(d.index)
    d = d[d.index.date < today].sort_index()
    if len(d) < min_days:
        return None
    return float(pd.to_numeric(d["volume"], errors="coerce").tail(window).mean())


HistoryFn = Callable[..., pd.DataFrame]   # history(symbol=, exchange=, interval=, start_date=, end_date=)


def make_fetcher(history: HistoryFn, symbols: Iterable[str], exchange: str = "NSE",
                 lookback_days: int = 7, adv_window: int = 20) -> Callable[[datetime], tuple]:
    """Build fetch(now) -> (bars_1m_by_symbol, adv_by_symbol) on top of an injected history function.

    The history function is the OpenAlgo SDK's history() in production. It is passed in so that this
    module never talks to the network itself."""
    syms = list(symbols)

    def fetch(now: datetime) -> tuple:
        end = now.date()
        start = end - timedelta(days=lookback_days)
        bars: Dict[str, pd.DataFrame] = {}
        adv: Dict[str, float] = {}
        for sym in syms:
            df = history(symbol=sym, exchange=exchange, interval="1m",
                         start_date=start.isoformat(), end_date=end.isoformat())
            bars[sym] = normalize_1m(df) if isinstance(df, pd.DataFrame) else normalize_1m(None)
            daily_start = end - timedelta(days=adv_window * 2)
            daily = history(symbol=sym, exchange=exchange, interval="D",
                            start_date=daily_start.isoformat(), end_date=end.isoformat())
            value = adv_from_daily(daily, end, window=adv_window) if isinstance(daily, pd.DataFrame) else None
            if value is not None:
                adv[sym] = value
        return bars, adv

    return fetch
