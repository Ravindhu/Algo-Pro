"""Closed bars for MTF, HTF and LTF (read only). Plan: docs/IMPLEMENTATION_PLAN_2026-11-18_STRATEGY_SPEC.md."""
from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "quant"))

from quant.data.mtf_bars import MultiTimeframeBars  # noqa: E402

NOW = datetime(2026, 11, 10, 10, 15)  # IST


def _intraday(n, last_start, minutes):
    rows = []
    for i in range(n):
        t = last_start - timedelta(minutes=minutes * (n - 1 - i))
        rows.append({"datetime": t.strftime("%Y-%m-%d %H:%M:%S"), "open": 100.0, "high": 101.0,
                     "low": 99.0, "close": 100.5, "volume": 1000.0})
    return rows


def _daily(n, end_day):
    rows = []
    for i in range(n):
        d = end_day - timedelta(days=n - 1 - i)
        rows.append({"datetime": d.strftime("%Y-%m-%d 00:00:00"), "open": 99.0, "high": 101.0,
                     "low": 98.0, "close": 100.0, "volume": 1000.0})
    return rows


class _Reader:
    def __init__(self, by_interval, status="success", raise_on=None):
        self.by_interval = by_interval
        self.status = status
        self.raise_on = raise_on
        self.calls = []

    def history(self, api_key, symbol, exchange, interval, start_date, end_date):
        self.calls.append(interval)
        if self.raise_on == interval:
            raise OSError("network")
        return {"status": self.status, "data": self.by_interval.get(interval)}


LABELS = {"mtf": "1d", "htf": "1h", "ltf": "5m"}


def _good_reader():
    # 5m: 40 bars, last row still forming (starts 10:10, now 10:15). Closed bars end at 10:10.
    five = _intraday(40, NOW - timedelta(minutes=5), 5)
    # 1h: 40 bars, last row forming (starts 10:00). Last closed bar ends 10:00 -> age 15 min: ok.
    hour = _intraday(40, NOW.replace(minute=0, second=0), 60)
    # 1d: 40 closed days ending 2026-11-09, plus today's forming row (2026-11-10)
    day = _daily(40, datetime(2026, 11, 9)) + [{"datetime": "2026-11-10 00:00:00", "open": 99.0,
                                               "high": 101.0, "low": 98.0, "close": 100.0, "volume": 1.0}]
    return _Reader({"5m": five, "1h": hour, "D": day})


def test_returns_closed_bars_for_all_three_roles():
    feed = MultiTimeframeBars(_good_reader(), "key", LABELS)
    out = feed.closed_frames("RELIANCE", NOW)
    assert out is not None
    assert set(out) == {"mtf", "htf", "ltf"}
    assert len(out["ltf"]) == 39          # 40 bars minus the forming one
    assert len(out["htf"]) == 39
    assert out["mtf"].index[-1].date().isoformat() == "2026-11-09"  # today's row dropped


def test_daily_uses_D_and_intraday_uses_own_interval():
    reader = _good_reader()
    MultiTimeframeBars(reader, "key", LABELS).closed_frames("RELIANCE", NOW)
    assert set(reader.calls) == {"D", "1h", "5m"}


def test_missing_feed_gives_none():
    reader = _Reader({"5m": _intraday(40, NOW - timedelta(minutes=5), 5)}, status="error")
    assert MultiTimeframeBars(reader, "key", LABELS).closed_frames("RELIANCE", NOW) is None


def test_exception_from_reader_gives_none():
    reader = _good_reader()
    reader.raise_on = "1h"
    assert MultiTimeframeBars(reader, "key", LABELS).closed_frames("RELIANCE", NOW) is None


def test_too_few_bars_gives_none():
    short = {"5m": _intraday(20, NOW - timedelta(minutes=5), 5),
             "1h": _intraday(40, NOW.replace(minute=0), 60),
             "D": _daily(40, datetime(2026, 11, 9))}
    assert MultiTimeframeBars(_Reader(short), "key", LABELS).closed_frames("RELIANCE", NOW) is None


def test_stale_intraday_gives_none():
    stale = {"5m": _intraday(40, NOW - timedelta(minutes=60), 5),
             "1h": _intraday(40, NOW.replace(minute=0), 60),
             "D": _daily(40, datetime(2026, 11, 9))}
    assert MultiTimeframeBars(_Reader(stale), "key", LABELS).closed_frames("RELIANCE", NOW) is None


def test_stale_daily_gives_none():
    old = {"5m": _intraday(40, NOW - timedelta(minutes=5), 5),
           "1h": _intraday(40, NOW.replace(minute=0), 60),
           "D": _daily(40, datetime(2026, 10, 20))}
    assert MultiTimeframeBars(_Reader(old), "key", LABELS).closed_frames("RELIANCE", NOW) is None


def test_bad_label_is_refused():
    with pytest.raises(ValueError, match="htf timeframe"):
        MultiTimeframeBars(_good_reader(), "key", {"mtf": "1d", "htf": "4h", "ltf": "5m"})


def test_missing_api_key_is_refused():
    with pytest.raises(ValueError):
        MultiTimeframeBars(_good_reader(), "", LABELS)
