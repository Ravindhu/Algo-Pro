"""Tests for the 1-minute feed, paper exits (trail, breakeven, target, stops) and the paper runtime (2026-11-08).

No network, no broker. The real risk gate and the real risk limits are used where it matters.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from quant.core.calendar import TradingCalendar
from quant.core.config import RiskLimits
from quant.data.minute_feed import (StaleData, adv_from_daily, aggregate, bucket_start,
                                    build_frames, make_fetcher, normalize_1m)
from quant.portfolio.tiers import TierTable
from quant.strategies.paper_runtime import build_paper_runtime, run_loop, seconds_to_next_minute
from quant.strategies.positions import PaperPositionManager
from quant.strategies.protect import Protection
from quant.strategies.scanner import ListAudit

ROOT = Path(__file__).resolve().parents[2]
LIMITS = RiskLimits.from_yaml(ROOT / "config" / "risk_limits.yaml")
TEST_LIMITS = dataclasses.replace(LIMITS, max_symbols_per_portfolio=5)
TIERS_OFF = TierTable(enabled=False, bands=(), max_multiplier=2.0)
CALENDAR = TradingCalendar(holidays=set(), verified=True)
DAY = "2026-11-09"


def minute_bars(start: str, n: int, o=100.0, h=100.0, l=100.0, c=100.0, vol=1000.0) -> pd.DataFrame:
    idx = pd.date_range(f"{DAY} {start}", periods=n, freq="1min")
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": vol}, index=idx)


# ---------- 1-minute feed ----------

def test_bucket_start_aligns_to_nine_fifteen():
    assert bucket_start(datetime(2026, 11, 9, 9, 21), 5) == pd.Timestamp("2026-11-09 09:20")
    assert bucket_start(datetime(2026, 11, 9, 9, 29), 15) == pd.Timestamp("2026-11-09 09:15")
    assert bucket_start(datetime(2026, 11, 9, 9, 30), 15) == pd.Timestamp("2026-11-09 09:30")


def test_aggregate_ohlcv_by_hand():
    df = normalize_1m(pd.DataFrame(
        {"open": [10, 11, 12], "high": [13, 15, 14], "low": [9, 10, 8], "close": [11, 12, 13],
         "volume": [100, 200, 300]},
        index=pd.date_range(f"{DAY} 09:15", periods=3, freq="1min")))
    out = aggregate(df, 5)
    row = out.iloc[0]
    assert (row.open, row.high, row.low, row.close, row.volume) == (10, 15, 8, 13, 600)


def test_build_frames_last_row_is_forming_and_only_closed_minutes_used():
    bars = minute_bars("09:15", 6)                       # 09:15 .. 09:20
    frames = build_frames(bars, datetime(2026, 11, 9, 9, 21, 30))
    five = frames["5minute"]
    assert list(five.index.strftime("%H:%M")) == ["09:15", "09:20"]   # 09:15 done, 09:20 forming
    assert len(five.iloc[:-1]) == 1
    assert five.iloc[-1]["open"] == 100.0                             # only the 09:20 minute is in it


def test_build_frames_15_minute_forming_bucket():
    frames = build_frames(minute_bars("09:15", 6), datetime(2026, 11, 9, 9, 21))
    fifteen = frames["15minute"]
    assert list(fifteen.index.strftime("%H:%M")) == ["09:15"]         # the only bucket is forming


def test_stale_data_fails_closed():
    bars = minute_bars("09:15", 5)                       # 09:15 .. 09:19; 09:20 missing
    with pytest.raises(StaleData):
        build_frames(bars, datetime(2026, 11, 9, 9, 21))


def test_adv_needs_enough_days_and_uses_last_window():
    days = pd.DataFrame({"volume": [1, 2, 3, 4, 5, 6, 7]},
                        index=pd.date_range("2026-11-02", periods=7, freq="D"))
    assert adv_from_daily(days, date(2026, 11, 9), window=3) == pytest.approx((5 + 6 + 7) / 3)
    assert adv_from_daily(days.iloc[:3], date(2026, 11, 9)) is None


def test_fetcher_uses_injected_history_and_builds_adv():
    calls = []

    def history(**kw):
        calls.append(kw["interval"])
        if kw["interval"] == "1m":
            return minute_bars("09:15", 3)
        return pd.DataFrame({"volume": [1000.0] * 6},
                            index=pd.date_range("2026-11-02", periods=6, freq="D"))

    fetch = make_fetcher(history, ["RELIANCE"], adv_window=20)
    bars, adv = fetch(datetime(2026, 11, 9, 10, 0))
    assert set(calls) == {"1m", "D"}
    assert len(bars["RELIANCE"]) == 3
    assert adv["RELIANCE"] == pytest.approx(1000.0)


# ---------- exits (trailing stop, breakeven, target, disaster stop, HTF) ----------

class FakeStrat:
    name = "ocl"
    htf_tf = "15minute"
    ltf_tf = "5minute"

    def __init__(self, atr_trail=False):
        self.p = SimpleNamespace(protection=Protection(atr_trail_enabled=atr_trail), atr_period=14)
        self.exit_flag = False
        self.exits = []
        self.entered = 0

    def exit_check(self, frames):
        return self.exit_flag

    def on_exit(self, reason, price):
        self.exits.append((reason, price))

    def on_entry(self):
        self.entered += 1

    def signal_frames(self, frames):
        return None


def sig(tp_dist=None, time="2026-11-09 09:10:00"):
    return {"entry_ref": 100.0, "trigger": 97.0, "htf_sl": 96.0, "sl_price": 94.0,
            "tp_dist": tp_dist, "exit_mode": "FIXED" if tp_dist else "DYNAMIC", "time": time}


def ltf_frames(rows, start="09:15"):
    """rows = (open, high, low, close) for 5-minute bars from 09:15. A forming row is added last."""
    idx = pd.date_range(f"{DAY} {start}", periods=len(rows) + 1, freq="5min")
    data = list(rows) + [(rows[-1][3], rows[-1][3], rows[-1][3], rows[-1][3])]
    o, h, l, c = zip(*data)
    return {"5minute": pd.DataFrame({"open": o, "high": h, "low": l, "close": c,
                                     "volume": 1000.0}, index=idx),
            "15minute": pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                                      "volume": 1.0},
                                     index=pd.date_range(f"{DAY} 09:15", periods=1, freq="15min"))}


def approve_all(intent):
    """Stand-in for PovExecutor.submit_child that approves every exit (gate tests live elsewhere)."""
    return SimpleNamespace(sent=True, reasons=())


def open_pos(strat, s):
    audit = ListAudit()
    mgr = PaperPositionManager(audit, submit=approve_all)
    mgr.open(key="k1", strategy=strat, symbol="RELIANCE", qty=10, sig=s, bar_time=s["time"])
    return mgr


def test_stop_out_on_close_below_htf_floor():
    strat = FakeStrat()
    mgr = open_pos(strat, sig())
    frames = ltf_frames([(100, 100.5, 99.5, 95.5)])          # closes 95.5 < floor 96, low above broker 94
    mgr.update({"RELIANCE": frames})
    assert strat.exits == [("SL", 95.5)]
    assert mgr.closed_trades[0]["reason"] == "SL"


def test_disaster_stop_fills_at_stop_or_open_whichever_is_lower():
    strat = FakeStrat()
    mgr = open_pos(strat, sig())
    mgr.update({"RELIANCE": ltf_frames([(95, 96, 93.5, 95.5)])})   # low 93.5 <= broker stop 94
    assert strat.exits == [("DISASTER", 94.0)]


def test_target_hit_fills_at_target_or_open_whichever_is_higher():
    strat = FakeStrat()
    mgr = open_pos(strat, sig(tp_dist=3.0))                      # target 103
    mgr.update({"RELIANCE": ltf_frames([(101, 103.5, 101, 102.5)])})
    assert strat.exits == [("TP", 103.0)]
    assert mgr.closed_trades[0]["pnl"] == pytest.approx(30.0)    # (103 - 100) * 10


def test_breakeven_moves_stop_then_gets_stopped_there():
    strat = FakeStrat(atr_trail=False)
    mgr = open_pos(strat, sig())                                 # be trigger 98.5, be floor 97.194
    mgr.update({"RELIANCE": ltf_frames([(98, 99, 97.9, 98.8)])})   # high 99 arms breakeven
    assert strat.exits == []
    mgr.update({"RELIANCE": ltf_frames([(98, 98, 97.9, 98.8), (98.8, 98.9, 97.5, 97.0)])})
    assert strat.exits == [("SL", 97.0)]                         # close 97.0 is below the 97.194 stop


def test_htf_floor_exit_uses_strategy_check():
    strat = FakeStrat()
    mgr = open_pos(strat, sig())
    strat.exit_flag = True
    mgr.update({"RELIANCE": ltf_frames([(100, 100.2, 99.8, 100.1)])})
    assert strat.exits == [("HTF", 100.1)]


def test_same_bar_is_never_processed_twice():
    strat = FakeStrat()
    mgr = open_pos(strat, sig())
    frames = ltf_frames([(100, 100.5, 99.5, 95.5)])
    mgr.update({"RELIANCE": frames})
    mgr.update({"RELIANCE": frames})
    assert len(strat.exits) == 1


# ---------- runtime ----------

class OnceStrat(FakeStrat):
    """Signals once, on the first call with frames."""

    def __init__(self, s):
        super().__init__()
        self.s = s
        self.called = 0
        self.htf_tf = "15minute"
        self.ltf_tf = "5minute"

    def signal_frames(self, frames):
        self.called += 1
        return self.s if self.called == 1 else None


def make_runtime(tmp_path, strat):
    return build_paper_runtime(strategies={"RELIANCE": [strat]}, limits=TEST_LIMITS, tiers=TIERS_OFF,
                               calendar=CALENDAR, capital=1_000_000.0, risk_fraction=0.0025,
                               skip_dates=[], ledger_path=tmp_path / "ledger.json",
                               kill_path=tmp_path / "KILL")


ADV = {"RELIANCE": 5_000_000.0}


def test_runtime_enters_then_exits_on_stop(tmp_path):
    entry_sig = sig(time="2026-11-09 09:55:00")
    strat = OnceStrat(entry_sig)
    rt = make_runtime(tmp_path, strat)
    bars = minute_bars("09:15", 45)                             # 09:15 .. 09:59, price 100
    rt.cycle(datetime(2026, 11, 9, 10, 0), {"RELIANCE": bars}, ADV)
    assert len(rt.sender.fills) == 1 and rt.sender.fills[0]["qty"] == 10
    assert rt.positions.open_notional() == pytest.approx(1000.0)
    assert strat.entered == 1

    crash = minute_bars("10:00", 5, o=96, h=96, l=95, c=95)     # 10:00 bucket closes at 95 < floor 96
    bars2 = pd.concat([bars, crash])
    rt.cycle(datetime(2026, 11, 9, 10, 5), {"RELIANCE": bars2}, ADV)
    assert rt.positions.closed_trades and rt.positions.closed_trades[0]["reason"] == "SL"
    assert rt.positions.open_notional() == 0.0
    assert strat.exits == [("SL", 95.0)]


def test_runtime_skips_outside_session(tmp_path):
    rt = make_runtime(tmp_path, OnceStrat(sig()))
    rt.cycle(datetime(2026, 11, 9, 8, 50), {"RELIANCE": minute_bars("09:15", 45)}, ADV)
    assert rt.sender.fills == []
    assert rt.audit.records[-1]["kind"] == "cycle_skipped"


def test_runtime_skips_stale_symbol_and_keeps_running(tmp_path):
    rt = make_runtime(tmp_path, OnceStrat(sig()))
    rt.cycle(datetime(2026, 11, 9, 10, 0), {"RELIANCE": minute_bars("09:15", 40)}, ADV)  # 09:55..09:59 missing
    assert rt.sender.fills == []
    assert any(r["kind"] == "stale_data" for r in rt.audit.records)


def test_loop_runs_on_the_minute_and_survives_a_failed_fetch(tmp_path):
    rt = make_runtime(tmp_path, OnceStrat(sig()))
    sleeps = []
    calls = {"n": 0}

    def fetch(now):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("history down")
        return {}, {}

    cycles = run_loop(rt, fetch, now_fn=lambda: datetime(2026, 11, 9, 8, 0, 30), sleep_fn=sleeps.append,
                      stop_fn=lambda: calls["n"] >= 2)
    assert cycles == 2
    assert any(r["kind"] == "fetch_failed" for r in rt.audit.records)
    assert sleeps[0] == pytest.approx(30.0)


def test_seconds_to_next_minute():
    assert seconds_to_next_minute(datetime(2026, 11, 9, 9, 15, 30, 500000)) == pytest.approx(29.5)
