"""Tests for engulf_signal and EngulfSequenceStrategy (phase F: bot wiring, paper only).

Flow stage: Strategy
Assumed rules (owner to confirm): entry = touched LTF key level; stop = lowest low of the
LTF OCL candles C1..C3; target = 3R; ATR14 on LTF closed bars.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from quant.strategies import owner_patterns as op
from quant.strategies.engulf_sequence_strategy import EngulfSequenceStrategy


def _ltf(n=30):
    start = datetime(2026, 11, 18, 9, 15)
    idx = [start + timedelta(minutes=5 * i) for i in range(n)]
    lows = [100.0 + i * 0.1 for i in range(n)]
    return pd.DataFrame({"open": lows, "high": [x + 1 for x in lows],
                         "low": lows, "close": [x + 0.5 for x in lows]}, index=idx)


def _fake_seq(ltf, ocl_pos=10, entry=102.0):
    return {"pattern_time": None, "touch_time": None, "sweep_time": None,
            "ocl_time": ltf.index[ocl_pos], "trigger_time": ltf.index[-1], "entry": entry}


def test_engulf_signal_builds_buy_levels(monkeypatch):
    ltf = _ltf()
    monkeypatch.setattr(op, "engulf_sequence", lambda m, h, l: _fake_seq(ltf))
    sig = op.engulf_signal(None, None, ltf)
    # stop = min low of C1..C3 = lows at positions 8, 9, 10
    assert sig["side"] == "BUY"
    assert sig["entry"] == 102.0
    assert sig["sl_dist"] == pytest.approx(102.0 - ltf["low"].iloc[8])
    assert sig["tp_dist"] == pytest.approx(3 * sig["sl_dist"])
    assert sig["atr"] > 0
    assert sig["strategy"] == "engulfing"
    assert sig["time"] == str(ltf.index[-1]) or sig["time"] == str(_fake_seq(ltf)["trigger_time"])


def test_signal_time_is_trigger_time_not_last_bar(monkeypatch):
    ltf = _ltf()
    monkeypatch.setattr(op, "engulf_sequence", lambda m, h, l: _fake_seq(ltf))
    sig = op.engulf_signal(None, None, ltf)
    assert sig["time"] == str(ltf.index[-1])  # fake trigger is the last bar
    later = pd.concat([ltf, _ltf(31).iloc[-1:]])
    monkeypatch.setattr(op, "engulf_sequence", lambda m, h, l: {**_fake_seq(ltf), "trigger_time": ltf.index[-1]})
    assert op.engulf_signal(None, None, later)["time"] == str(ltf.index[-1])


def test_engulf_signal_none_without_sequence(monkeypatch):
    monkeypatch.setattr(op, "engulf_sequence", lambda m, h, l: None)
    assert op.engulf_signal(None, None, _ltf()) is None


def test_engulf_signal_refuses_entry_at_or_below_stop(monkeypatch):
    ltf = _ltf()
    monkeypatch.setattr(op, "engulf_sequence", lambda m, h, l: _fake_seq(ltf, entry=100.0))
    assert op.engulf_signal(None, None, ltf) is None


def test_engulf_signal_refuses_ocl_too_early(monkeypatch):
    ltf = _ltf()
    monkeypatch.setattr(op, "engulf_sequence", lambda m, h, l: _fake_seq(ltf, ocl_pos=1))
    assert op.engulf_signal(None, None, ltf) is None


class _FakeFeed:
    def __init__(self, frames):
        self.frames = frames
        self.calls = []

    def closed_frames(self, symbol, now):
        self.calls.append((symbol, now))
        return self.frames


def test_strategy_passes_closed_frames_to_signal(monkeypatch):
    seen = {}

    def fake(m, h, l):
        seen["args"] = (m, h, l)
        return None

    import quant.strategies.engulf_sequence_strategy as mod
    monkeypatch.setattr(mod, "engulf_signal", fake)
    feed = _FakeFeed({"mtf": "M", "htf": "H", "ltf": "L"})
    strat = EngulfSequenceStrategy(feed)
    now = datetime(2026, 11, 18, 10, 0)
    assert strat.signal_for("ABC", None, now) is None
    assert feed.calls == [("ABC", now)]
    assert seen["args"] == ("M", "H", "L")


def test_strategy_none_when_feed_not_ready():
    strat = EngulfSequenceStrategy(_FakeFeed(None))
    assert strat.signal_for("ABC", None, datetime(2026, 11, 18, 10, 0)) is None


def test_plain_signal_is_not_used():
    assert EngulfSequenceStrategy(_FakeFeed(None)).signal(None) is None


def test_load_timeframes_missing_file_gives_none(tmp_path):
    from quant.core.setup_store import load_timeframes
    assert load_timeframes(tmp_path / "missing.yaml") == {"mtf": None, "htf": None, "ltf": None}


def test_load_timeframes_reads_saved_values(tmp_path):
    from quant.core.setup_store import load_timeframes
    f = tmp_path / "settings.yaml"
    f.write_text("trade_setup:\n  timeframes:\n    htf: 1h\n    mtf: 1d\n    ltf: 5m\n", encoding="utf-8")
    assert load_timeframes(f) == {"mtf": "1d", "htf": "1h", "ltf": "5m"}
