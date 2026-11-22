"""Weekly (1w) and monthly (1M) trade setup inputs (owner request 2026-11-22). Closed bars only, read only."""
from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "quant"))

from quant.core import setup_store  # noqa: E402
from quant.data.mtf_bars import MultiTimeframeBars  # noqa: E402


def _rows(dates):
    return [{"datetime": d.strftime("%Y-%m-%d 00:00:00"), "open": 99.0, "high": 101.0,
             "low": 98.0, "close": 100.0, "volume": 1000.0} for d in dates]


class _Reader:
    def __init__(self, by_interval):
        self.by_interval = by_interval

    def history(self, api_key, symbol, exchange, interval, start_date, end_date):
        return {"status": "success", "data": self.by_interval.get(interval)}


def _bars(label, now, step_days, n):
    """n bars ending before the current period of `label` (the bar still forming is excluded by the rule)."""
    if label == "1w":
        this_monday = now.date() - timedelta(days=now.date().weekday())
        return [this_monday - timedelta(weeks=n - k) for k in range(n)]
    first = now.date().replace(day=1)
    out = []
    month, year = first.month, first.year
    for k in range(n, 0, -1):
        m = month - k
        y = year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        out.append(datetime(y, m, 1).date())
    return out


def _frames(label, now):
    dates = _bars(label, now, 0, 40)
    # one extra row for the period that is still forming (its date is inside the current period)
    forming = now.date() - timedelta(days=now.date().weekday()) if label == "1w" else now.date().replace(day=1)
    rows = _rows(dates) + _rows([forming])
    return rows


def _mtf(label, now, reader):
    m = MultiTimeframeBars(reader, "key", {"mtf": label, "htf": "1h", "ltf": "5m"})
    return m._closed("NSE:TEST", label, now)


def test_weekly_and_monthly_are_allowed_inputs():
    assert "1w" in setup_store.TIMEFRAMES and "1M" in setup_store.TIMEFRAMES
    assert "1d" in setup_store.TIMEFRAMES  # daily was already there
    assert setup_store.TIMEFRAMES["1d"] < setup_store.TIMEFRAMES["1w"] < setup_store.TIMEFRAMES["1M"]


def test_weekly_mtf_order_is_checked_like_the_others():
    setup_store.check_timeframe_order({"ltf": "1d", "htf": "1w", "mtf": "1M"})  # ok
    with pytest.raises(ValueError):
        setup_store.check_timeframe_order({"ltf": "1M", "htf": "1w", "mtf": "1d"})


def test_weekly_closed_bars_exclude_the_forming_week():
    now = datetime(2026, 11, 25, 10, 0)  # a Wednesday: the current week started Monday 2026-11-23
    closed = _mtf("1w", now, _Reader({"W": _frames("1w", now)}))
    assert closed is not None
    assert closed.index[-1].date() < datetime(2026, 11, 23).date()


def test_monthly_closed_bars_exclude_the_forming_month():
    now = datetime(2026, 11, 22, 9, 0)  # November is forming
    closed = _mtf("1M", now, _Reader({"M": _frames("1M", now)}))
    assert closed is not None
    assert closed.index[-1].date() < datetime(2026, 11, 1).date()


def test_monthly_too_few_months_gives_no_data():
    now = datetime(2026, 11, 22, 9, 0)
    short = _rows([datetime(2026, 9, 1).date(), datetime(2026, 10, 1).date()])
    assert _mtf("1M", now, _Reader({"M": short})) is None
