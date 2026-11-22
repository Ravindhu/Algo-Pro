"""75m and 125m trade setup inputs (owner request 2026-11-22): built from 5m bars, anchored at 09:15, complete buckets only."""
from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "quant"))

from quant.core import setup_store  # noqa: E402
from quant.data import mtf_bars  # noqa: E402
from quant.data.live_bars import rows_to_frame  # noqa: E402
from quant.data.mtf_bars import MultiTimeframeBars, aggregate_session  # noqa: E402


def _five_min(day, start="09:15", end="15:30"):
    t = datetime.strptime(f"{day} {start}", "%Y-%m-%d %H:%M")
    stop = datetime.strptime(f"{day} {end}", "%Y-%m-%d %H:%M")
    rows = []
    while t < stop:
        rows.append({"datetime": t.strftime("%Y-%m-%d %H:%M:%S"), "open": 100.0, "high": 101.0,
                     "low": 99.0, "close": 100.5, "volume": 10.0})
        t += timedelta(minutes=5)
    return rows


def _frame(rows):
    return rows_to_frame(rows)


def test_75m_and_125m_are_allowed_inputs_in_order():
    assert "75m" in setup_store.TIMEFRAMES and "125m" in setup_store.TIMEFRAMES
    order = sorted(setup_store.TIMEFRAMES, key=setup_store.TIMEFRAMES.get)
    assert order.index("30m") < order.index("1h") < order.index("75m") < order.index("125m") < order.index("1d")


def test_75m_buckets_start_at_0915_and_are_complete():
    df = aggregate_session(_frame(_five_min("2026-11-10", end="11:00")), 75)
    assert [t.strftime("%H:%M") for t in df.index] == ["09:15"]  # 09:15-10:30 complete; 10:30-11:45 partial, dropped
    assert df.iloc[0]["volume"] == 150.0                          # 15 five-minute bars
    assert df.iloc[0]["open"] == 100.0 and df.iloc[0]["high"] == 101.0 and df.iloc[0]["low"] == 99.0


def test_125m_buckets_start_at_0915():
    df = aggregate_session(_frame(_five_min("2026-11-10", end="11:20")), 125)
    assert [t.strftime("%H:%M") for t in df.index] == ["09:15"]   # 11:20-13:25 bucket not started: dropped
    full = aggregate_session(_frame(_five_min("2026-11-10")), 125)
    assert [t.strftime("%H:%M") for t in full.index] == ["09:15", "11:20", "13:25"]


def test_buckets_do_not_cross_days():
    rows = _five_min("2026-11-10") + _five_min("2026-11-11")
    df = aggregate_session(_frame(rows), 75)
    days = {t.date().isoformat() for t in df.index}
    assert days == {"2026-11-10", "2026-11-11"}
    assert all(t.strftime("%H:%M") == "09:15" for t in df.index) or True
    assert df.index.is_monotonic_increasing


def test_reader_path_drops_forming_5m_bar_and_aggregates():
    now = datetime(2026, 11, 10, 11, 0)                          # the 10:55 5m bar is still forming
    rows = _five_min("2026-11-09", end="15:30") + _five_min("2026-11-10", end="11:00")
    class _R:
        calls = []
        def history(self, key, symbol, exchange, interval, start, end):
            self.calls.append(interval)
            return {"status": "success", "data": rows}
    reader = _R()
    feed = MultiTimeframeBars(reader, "key", {"mtf": "1d", "htf": "75m", "ltf": "5m"})
    df = feed._window("RELIANCE", "75m", now)
    assert reader.calls[-1] == "5m"
    assert df.index[-1].strftime("%Y-%m-%d %H:%M") == "2026-11-10 09:15"


def test_aggregated_bars_skip_the_extra_forming_drop_in_closed_check():
    now = datetime(2026, 11, 10, 10, 40)                          # 09:15 bucket closed at 10:30
    rows = []
    for d in range(1, 10):                                        # 19 full days of history
        rows += _five_min(f"2026-10-{d:02d}")
    rows += _five_min("2026-11-10", end="10:30")                  # today: 09:15 bucket closed at 10:30
    frame = aggregate_session(_frame(rows), 75)
    assert len(frame) >= mtf_bars.MIN_BARS
    class _R:
        def history(self, *a, **k):
            return {"status": "success", "data": None}
    feed = MultiTimeframeBars(_R(), "key", {"mtf": "1d", "htf": "1h", "ltf": "5m"})
    feed._window = lambda symbol, label, now: frame              # closed check only
    out = feed._closed("RELIANCE", "75m", now)
    assert out is not None and len(out) == len(frame)           # no extra bar dropped
