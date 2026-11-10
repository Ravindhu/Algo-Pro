"""Flow stage: Market inputs, Data and features, Stale-feed and missing-bar checks.
Status: TESTS for PHASE 3 IMPLEMENTED live bars and the read-only history call.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from quant.data.live_bars import LiveBars, is_fresh, rows_to_frame
from quant.execution.readonly_client import OpenAlgoReadOnly

NOW = datetime(2026, 11, 10, 10, 15)


def _intraday_rows(n=40, end=NOW - timedelta(minutes=5), key="datetime"):
    rows = []
    for i in range(n):
        t = end - timedelta(minutes=5 * (n - 1 - i))
        rows.append({key: t.strftime("%Y-%m-%d %H:%M:%S"), "open": 100.0, "high": 101.0,
                     "low": 99.0, "close": 100.5, "volume": 1000.0 + i})
    return rows


def _daily_rows(today_volume=999_999.0):
    rows = []
    for d in range(1, 11):
        day = datetime(2026, 11, 10) - timedelta(days=d)
        rows.append({"datetime": day.strftime("%Y-%m-%d 00:00:00"), "open": 99.0, "high": 101.0,
                     "low": 98.0, "close": 100.0 + d, "volume": 1000.0})
    rows.append({"datetime": "2026-11-10 00:00:00", "open": 99.0, "high": 101.0, "low": 98.0,
                 "close": 150.0, "volume": today_volume})
    return rows


class _Reader:
    def __init__(self, intraday=None, daily=None, status="success", raise_on_history=False):
        self.intraday = intraday
        self.daily = daily
        self.status = status
        self.raise_on_history = raise_on_history
        self.calls = []

    def history(self, api_key, symbol, exchange, interval, start_date, end_date):
        self.calls.append((symbol, interval, start_date, end_date))
        if self.raise_on_history:
            raise OSError("network")
        data = self.daily if interval == "D" else self.intraday
        return {"status": self.status, "data": data}


# --- read-only history call ---

def test_history_payload_and_path():
    seen = {}

    def transport(url, payload):
        seen["url"], seen["payload"] = url, payload
        return {"status": "success", "data": []}

    reader = OpenAlgoReadOnly("http://host/", transport)
    reader.history("KEY", "INFY", "NSE", "5m", "2026-11-05", "2026-11-10")
    assert seen["url"] == "http://host/api/v1/history"
    assert seen["payload"]["interval"] == "5m"
    assert seen["payload"]["apikey"] == "KEY"


def test_history_refuses_empty_key():
    reader = OpenAlgoReadOnly("http://host", lambda u, p: {})
    with pytest.raises(ValueError):
        reader.history("", "INFY", "NSE", "5m", "a", "b")


def test_read_client_has_no_order_method():
    for name in ("place", "place_child", "cancel", "modify"):
        assert not hasattr(OpenAlgoReadOnly, name)


# --- row parsing ---

def test_rows_to_frame_sorts_and_types_columns():
    rows = list(reversed(_intraday_rows(5)))
    df = rows_to_frame(rows)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.is_monotonic_increasing
    assert df["volume"].dtype.kind == "f"


def test_rows_to_frame_rejects_missing_columns_and_empty():
    assert rows_to_frame([]) is None
    assert rows_to_frame([{"datetime": "2026-11-10 10:00:00", "open": 1}]) is None
    assert rows_to_frame("not a list") is None


def test_rows_to_frame_accepts_timestamp_key():
    df = rows_to_frame(_intraday_rows(3, key="timestamp"))
    assert df is not None and len(df) == 3


def test_rows_to_frame_converts_tz_aware_to_ist_naive():
    rows = [{"datetime": "2026-11-10T04:45:00+00:00", "open": 1, "high": 1, "low": 1,
             "close": 1, "volume": 1}]
    df = rows_to_frame(rows)
    assert df.index[0] == pd.Timestamp("2026-11-10 10:15:00")


# --- freshness ---

def test_is_fresh_within_limit_and_stale_beyond():
    df = rows_to_frame(_intraday_rows(3))
    assert is_fresh(df, NOW, 15) is True
    assert is_fresh(df, NOW + timedelta(minutes=30), 15) is False


def test_future_bar_is_not_fresh():
    df = rows_to_frame(_intraday_rows(3, end=NOW + timedelta(minutes=5)))
    assert is_fresh(df, NOW, 15) is False


# --- LiveBars ---

def test_bars_returns_frame_for_fresh_feed():
    lb = LiveBars(_Reader(intraday=_intraday_rows(40)), "KEY")
    df = lb.bars("INFY", NOW)
    assert df is not None and len(df) == 40


def test_bars_none_on_error_status_or_exception():
    assert LiveBars(_Reader(intraday=_intraday_rows(40), status="error"), "KEY").bars("INFY", NOW) is None
    assert LiveBars(_Reader(raise_on_history=True), "KEY").bars("INFY", NOW) is None


def test_bars_none_when_too_few_or_stale():
    assert LiveBars(_Reader(intraday=_intraday_rows(10)), "KEY").bars("INFY", NOW) is None
    stale = _intraday_rows(40, end=NOW - timedelta(hours=2))
    assert LiveBars(_Reader(intraday=stale), "KEY").bars("INFY", NOW) is None


def test_bars_requests_intraday_interval():
    reader = _Reader(intraday=_intraday_rows(40))
    LiveBars(reader, "KEY", interval="5m").bars("INFY", NOW)
    assert reader.calls[0][1] == "5m"


def test_daily_stats_exclude_today():
    lb = LiveBars(_Reader(daily=_daily_rows()), "KEY")
    stats = lb.daily_stats("INFY", NOW)
    assert stats["adv_shares"] == pytest.approx(1000.0)  # today's 999,999 is not used
    assert stats["prev_close"] == pytest.approx(101.0)   # yesterday's close


def test_daily_stats_none_when_too_little_history():
    short = _daily_rows()[-3:]
    assert LiveBars(_Reader(daily=short), "KEY").daily_stats("INFY", NOW) is None


def test_daily_stats_none_on_error():
    assert LiveBars(_Reader(daily=_daily_rows(), status="error"), "KEY").daily_stats("INFY", NOW) is None


def test_livebars_needs_key():
    with pytest.raises(ValueError):
        LiveBars(_Reader(), "")
