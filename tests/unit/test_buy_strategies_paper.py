"""Hand-checked tests for the four BUY strategies (paper port, 2026-11-08).

Every price in these tests is worked out by hand from the rules in the strategy docstrings.
No broker, no network, no files.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from quant.strategies import BUY_STRATEGIES, build
from quant.strategies.engulfing import body_ratio, engulfing, fib_entry_level
from quant.strategies.indicators import atr, hm_bullish, hm_direction, hm_rsi_series
from quant.strategies.ocl import OclKeylevel, OclParams, htf_setup
from quant.strategies.ocl_sell_fail import Handoff, OclSellFailBottom, OclSellFailTop
from quant.strategies.protect import Protection, Trade


def frame(rows: list[tuple[float, float, float, float]], start: str, freq: str) -> pd.DataFrame:
    """rows = (open, high, low, close). Oldest first. The LAST row is the bar still forming."""
    idx = pd.date_range(start, periods=len(rows), freq=freq)
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c,
                         "volume": [1000.0] * len(rows)}, index=idx)


# ---------- indicators ----------

def test_rsi_all_gains_is_100_and_all_losses_is_0():
    up = [float(i) for i in range(1, 20)]
    down = [float(20 - i) for i in range(20)]
    assert hm_rsi_series(up, 9)[-1] == pytest.approx(100.0)
    assert hm_rsi_series(down, 9)[-1] == pytest.approx(0.0)


def test_rsi_flat_series_is_50():
    assert hm_rsi_series([10.0] * 15, 9)[-1] == pytest.approx(50.0)


def test_hm_bullish_needs_rsi_strictly_above_ema_and_oversold_cap():
    up = [float(i) for i in range(1, 30)]             # RSI 100 and EMA(RSI) 100: equal, so not bullish
    assert hm_bullish(up, 9, 3) is False
    mixed = [10.0 + 0.5 * i + (-3.0 if i % 4 == 3 else 0.0) for i in range(40)]
    assert hm_bullish(mixed, 9, 3) == (int(hm_direction(mixed, 9, 3)[-1]) == 1)
    assert hm_bullish(mixed, 9, 3, oversold=0.0) is False   # oversold cap blocks it


def test_atr_simple_case():
    # Constant range 2 and no gaps: true range is 2 every bar, so ATR is 2.
    highs = [11.0] * 5
    lows = [9.0] * 5
    closes = [10.0] * 5
    assert atr(highs, lows, closes, period=3)[-1] == pytest.approx(2.0)
    assert atr(highs, lows, closes, period=3)[0] is None


# ---------- engulfing helpers ----------

def test_engulfing_bullish_pattern_with_body_filter():
    # Prev red: open 101, close 99.5, body 1.5 of range 1.9 = 0.79.
    # Last green engulfs it: open 99.2 <= 99.5, close 101.5 >= 101, body 2.3 of range 2.5 = 0.92.
    df = frame([(101.0, 101.2, 99.3, 99.5), (99.2, 101.6, 99.1, 101.5)], "2026-11-02 09:15", "15min")
    assert int(engulfing(df, 0.5).iloc[-1]) == 1
    assert body_ratio(df).iloc[-1] == pytest.approx(2.3 / 2.5)


def test_engulfing_body_filter_rejects_small_body():
    df = frame([(101.0, 101.2, 99.3, 99.5), (99.2, 101.6, 99.1, 101.5)], "2026-11-02 09:15", "15min")
    assert int(engulfing(df, 0.95).iloc[-1]) == 0      # 0.92 is below 0.95


def test_fib_entry_level_buy():
    # high 101.6, low 99.1, range 2.5, fib 0.886: 101.6 - 0.886 * 2.5 = 99.385
    assert fib_entry_level(101.6, 99.1, 1, 0.886) == pytest.approx(99.385)


# ---------- protection ----------

def test_breakeven_moves_stop_up_once_only():
    p = Protection(atr_trail_enabled=False, breakeven_rr=1.0, round_trip_cost_pct=0.20)
    t = Trade.open(entry=100.0, trigger=100.0, htf_sl=98.0, broker_sl=96.0, tp=None, p=p)
    # risk = 2, breakeven trigger = 102, breakeven floor = 100 * 1.002 = 100.2
    assert t.be_trigger == pytest.approx(102.0)
    assert t.on_closed_bar(high=102.5, close=102.1, atr=None, p=p) is None
    assert t.stop == pytest.approx(100.2)
    assert t.on_closed_bar(high=101.0, close=100.5, atr=None, p=p) is None
    assert t.stop == pytest.approx(100.2)              # does not go back down


def test_close_below_stop_is_sl():
    p = Protection(atr_trail_enabled=False)
    t = Trade.open(entry=100.0, trigger=100.0, htf_sl=98.0, broker_sl=96.0, tp=None, p=p)
    assert t.on_closed_bar(high=100.2, close=97.9, atr=None, p=p) == "SL"


def test_intrabar_disaster_checked_before_target():
    p = Protection()
    t = Trade.open(entry=100.0, trigger=100.0, htf_sl=98.0, broker_sl=96.0, tp=106.0, p=p)
    assert t.intrabar_exit(high=107.0, low=95.0) == "DISASTER"
    assert t.intrabar_exit(high=107.0, low=97.0) == "TP"


# ---------- OCL keylevel ----------

def ocl_frames(ltf_extra_rows=27):
    """HTF: C1 red, C2 red, C3 green closing above C2 open (C3 break). Plus a forming row.
    LTF: 27 flat bars, then last 3 closed = red, red, green touching the trigger. Plus a forming row."""
    htf_rows = [
        (100.0, 101.0, 98.8, 99.0),   # C1 red: open 100, close 99
        (99.0, 100.0, 96.0, 97.0),    # C2 red: open 99, close 97, low 96
        (97.0, 102.0, 96.5, 101.0),   # C3 green: open 97, close 101 > C2 open 99 -> break
        (101.0, 101.5, 100.5, 101.2), # forming
    ]
    flat = [(100.5, 101.0, 99.5, 100.5)] * ltf_extra_rows
    ltf_rows = flat + [
        (98.5, 98.6, 97.5, 97.8),     # red
        (97.8, 97.9, 97.0, 97.2),     # red
        (97.0, 98.0, 96.8, 97.9),     # green; low 96.8 <= trigger 97
        (97.9, 98.1, 97.7, 98.0),     # forming
    ]
    return {"15minute": frame(htf_rows, "2026-11-02 09:15", "15min"),
            "5minute": frame(ltf_rows, "2026-11-02 09:15", "5min")}


def test_ocl_htf_setup_levels_by_hand():
    p = OclParams(allow_c4_confirmation=False)
    closed = ocl_frames()["15minute"].iloc[:-1]
    s = htf_setup(closed, p)
    assert s["confirm_bar"] == "C3"
    assert s["aggressive"] == pytest.approx(99.0)      # C2 open
    assert s["conservative"] == pytest.approx(97.0)    # C3 open
    assert s["htf_sl"] == pytest.approx(96.0)          # min(C2 low 96, C3 low 96.5)


def test_ocl_c4_confirmation_uses_c3_open_as_aggressive():
    """Owner rule (2026-11-08): when C4 confirms (C3 does not break C2 open), aggressive = C3 open."""
    htf_rows = [
        (100.0, 101.0, 98.8, 99.0),   # C1 red
        (99.0, 100.0, 96.0, 97.0),    # C2 red: open 99, low 96
        (97.0, 99.0, 96.5, 98.5),     # C3 green but closes 98.5, below C2 open 99: no C3 break
        (98.5, 100.5, 98.4, 100.0),   # C4 green closes 100 > C2 open 99: C4 confirmation
    ]
    closed = frame(htf_rows, "2026-11-02 09:15", "15min")
    s = htf_setup(closed, OclParams(allow_c4_confirmation=True))
    assert s["confirm_bar"] == "C4"
    assert s["aggressive"] == pytest.approx(97.0)      # C3 open
    assert s["conservative"] == pytest.approx(97.0)    # C3 open
    assert s["htf_sl"] == pytest.approx(96.0)


def test_ocl_signal_fires_on_touch_with_conservative_trigger():
    s = build("ocl", "RELIANCE", allow_c4_confirmation=False, hm_enabled=False,
              trigger_level_mode="CONSERVATIVE", atr_period=14)
    sig = s.signal_frames(ocl_frames())
    assert sig is not None
    assert sig["side"] == "BUY"
    assert sig["trigger"] == pytest.approx(97.0)
    assert sig["entry_ref"] == pytest.approx(97.9)
    assert sig["htf_sl"] == pytest.approx(96.0)
    # ref 97.9 is between conservative (97) and aggressive (99): middle band -> FIXED
    assert sig["exit_mode"] == "FIXED"
    assert sig["tp_dist"] == pytest.approx(3.0 * (97.9 - 96.0))
    assert sig["sl_price"] < sig["entry_ref"]


def test_ocl_no_signal_when_hm_gate_fails_on_oversold_cap():
    # Same frames, HM gate on: LTF RSI of the rising flat part is far above 30, so the cap blocks it.
    s = build("ocl", "RELIANCE", allow_c4_confirmation=False, hm_enabled=True)
    assert s.signal_frames(ocl_frames()) is None


def test_ocl_no_signal_when_price_never_touches_trigger():
    frames = ocl_frames()
    ltf = frames["5minute"].copy()
    ltf.iloc[-2, ltf.columns.get_loc("low")] = 97.5      # last closed low above trigger 97
    frames["5minute"] = ltf
    s = build("ocl", "RELIANCE", allow_c4_confirmation=False, hm_enabled=False)
    assert s.signal_frames(frames) is None


def test_ocl_stop_out_steps_trigger_down_then_retires():
    s = build("ocl", "RELIANCE", allow_c4_confirmation=False, hm_enabled=False, max_ltf_retries=1)
    s.signal_frames(ocl_frames())
    s.on_entry()
    s.on_exit("SL", 96.5)
    assert s.st.level == pytest.approx(96.5)           # stepped down to the stop-out close
    assert s.st.retries == 1 and s.st.armed is True
    s.signal_frames(ocl_frames())                      # re-arms at the same trigger, fine
    s.on_entry()
    s.on_exit("SL", 96.4)                              # retries exhausted -> pattern retired
    assert s.st.armed is False
    assert s.st.key in s.st.used


def test_ocl_state_round_trip():
    s = build("ocl", "RELIANCE", allow_c4_confirmation=False, hm_enabled=False)
    s.signal_frames(ocl_frames())
    saved = s.state()
    s2 = build("ocl", "RELIANCE", allow_c4_confirmation=False, hm_enabled=False)
    s2.restore(saved)
    assert s2.st.level == s.st.level and s2.st.key == s.st.key


# ---------- sell-fail TOP and BOTTOM ----------

def sell_fail_frames():
    """HTF failed-short core, then the break:
      C1 green  (open 100, close 101)
      C2 green  (open 100, close 103, low 99.8)  body 3 of range 3.4
      C3 red    (open 103, close 100.5, low 100.3) body 2.5 of range 2.8
      C4 break  (close 104 > C3 open 103)
    So: aggressive = C3 open 103, conservative = C2 open 100, floor = min lows = 99.8.
    LTF: last 3 closed red, red, green, last low 102.9 touches 103."""
    htf_rows = [
        (100.0, 101.2, 99.9, 101.0),
        (100.0, 103.2, 99.8, 103.0),
        (103.0, 103.1, 100.3, 100.5),
        (100.8, 104.2, 102.5, 104.0),
        (104.0, 104.2, 103.8, 104.1),  # forming
    ]
    ltf_rows = [(103.8, 103.9, 103.4, 103.5)] * 27 + [
        (103.9, 104.0, 103.5, 103.6),
        (103.6, 103.7, 103.2, 103.3),
        (103.3, 103.9, 102.9, 103.6),
        (103.6, 103.8, 103.5, 103.7),  # forming
    ]
    return {"15minute": frame(htf_rows, "2026-11-02 09:15", "15min"),
            "5minute": frame(ltf_rows, "2026-11-02 09:15", "5min")}


def sell_fail_bottom_frames():
    """Same HTF as sell_fail_frames. BOTTOM trigger is the lowest swing open = 100.
    LTF last 3 closed = red, red, green; last low 99.9 touches 100; close 100.2 is above floor 99.8."""
    frames = sell_fail_frames()
    ltf_rows = [(100.6, 100.7, 100.0, 100.2)] * 27 + [
        (100.6, 100.7, 100.0, 100.2),  # red
        (100.2, 100.3, 99.9, 99.95),   # red
        (99.95, 100.3, 99.9, 100.2),   # green, low 99.9 <= 100
        (100.2, 100.4, 100.1, 100.3),  # forming
    ]
    frames["5minute"] = frame(ltf_rows, "2026-11-02 09:15", "5min")
    return frames


def test_sell_fail_top_fires_at_tier_one_level():
    s = build("ocl_sell_fail_top", "TCS", hm_enabled=False, handoff=Handoff())
    sig = s.signal_frames(sell_fail_frames())
    assert sig is not None
    assert sig["blueprint"] == "TOP"
    assert sig["trigger"] == pytest.approx(103.0)      # tier 1 = broken C3 open
    assert sig["htf_sl"] == pytest.approx(99.8)


def test_sell_fail_bottom_waits_for_top_handoff():
    h = Handoff()
    bottom = build("ocl_sell_fail_bottom", "TCS", hm_enabled=False, handoff=h)
    assert bottom.signal_frames(sell_fail_frames()) is None   # TOP has not exhausted this core yet

    top = build("ocl_sell_fail_top", "TCS", hm_enabled=False, handoff=h)
    top.signal_frames(sell_fail_frames())
    top.on_exit("TP", 110.0)                                  # TOP finishes -> marks the core exhausted

    bottom = build("ocl_sell_fail_bottom", "TCS", hm_enabled=False, handoff=h)
    sig = bottom.signal_frames(sell_fail_bottom_frames())
    assert sig is not None
    assert sig["blueprint"] == "BOTTOM"
    # lowest swing candle in core..fail is C1 (low 99.9, open 100): trigger = its open
    assert sig["trigger"] == pytest.approx(100.0)


def test_sell_fail_top_requires_handoff_object():
    with pytest.raises(ValueError):
        build("ocl_sell_fail_top", "TCS", hm_enabled=False)


# ---------- engulfing OCL ----------

def engulfing_frames():
    """HTF: red bar then bullish engulfing (bodies 0.79 and 0.92), plus forming row.
    Fib level = 101.6 - 0.886 * 2.5 = 99.385; floor = low 99.1.
    LTF: last 3 closed red, red, green touching 99.385 (low 99.2), close 99.6 above floor."""
    htf_rows = [
        (101.0, 101.2, 99.3, 99.5),
        (99.2, 101.6, 99.1, 101.5),
        (101.5, 101.8, 101.2, 101.6),  # forming
    ]
    ltf_rows = [(99.9, 100.0, 99.4, 99.5)] * 27 + [
        (99.9, 100.0, 99.4, 99.5),     # red
        (99.5, 99.6, 99.25, 99.3),     # red
        (99.3, 99.7, 99.2, 99.6),      # green, low 99.2 <= 99.385
        (99.6, 99.8, 99.5, 99.7),      # forming
    ]
    return {"15minute": frame(htf_rows, "2026-11-02 09:15", "15min"),
            "5minute": frame(ltf_rows, "2026-11-02 09:15", "5min")}


def test_engulfing_ocl_fires_on_fib_touch():
    s = build("engulfing_ocl", "INFY", hm_enabled=False)
    sig = s.signal_frames(engulfing_frames())
    assert sig is not None
    assert sig["side"] == "BUY"
    assert sig["trigger"] == pytest.approx(99.385)
    assert sig["confirm_bar"] == "ENG"


def test_engulfing_ocl_no_signal_without_engulfing():
    frames = engulfing_frames()
    htf = frames["15minute"].copy()
    htf.iloc[1, htf.columns.get_loc("close")] = 100.0   # no longer engulfs
    frames["15minute"] = htf
    s = build("engulfing_ocl", "INFY", hm_enabled=False)
    assert s.signal_frames(frames) is None


# ---------- registry and safety ----------

def test_registry_has_only_the_four_buy_blueprints():
    assert set(BUY_STRATEGIES) == {"ocl", "ocl_sell_fail_top", "ocl_sell_fail_bottom", "engulfing_ocl"}


def test_unknown_strategy_name_fails_loudly():
    with pytest.raises(KeyError):
        build("ocl_inv", "RELIANCE")


def test_strategies_never_emit_sell_side():
    for name in ("ocl", "engulfing_ocl"):
        s = build(name, "RELIANCE", hm_enabled=False, allow_c4_confirmation=False,
                  handoff=Handoff())
        sig = s.signal_frames(ocl_frames())
        if sig is not None:
            assert sig["side"] == "BUY"


def test_bad_params_are_rejected():
    with pytest.raises(ValueError):
        OclParams(trigger_level_mode="MIDDLE")
    with pytest.raises(ValueError):
        OclParams(exit_mode="NONE")
    with pytest.raises(ValueError):
        OclParams(min_body_pct=1.0)
    with pytest.raises(ValueError):
        OclKeylevel("RELIANCE", bogus_key=1)


def test_signal_only_uses_closed_bars():
    """The forming row must not change the answer: change it and the signal stays the same."""
    base = build("ocl", "RELIANCE", allow_c4_confirmation=False, hm_enabled=False)
    sig1 = base.signal_frames(ocl_frames())
    frames = ocl_frames()
    frames["5minute"].iloc[-1, frames["5minute"].columns.get_loc("close")] = 50.0
    other = build("ocl", "RELIANCE", allow_c4_confirmation=False, hm_enabled=False)
    sig2 = other.signal_frames(frames)
    assert sig1 == sig2


def test_sell_fail_key_points_at_core_start_even_with_long_history():
    """Regression for an Algo-Lab bug: the pattern key used htf.index[core_start] on the FULL history,
    but core_start is relative to the last max_window bars. With long history it named the wrong bar,
    so the 'used' check could miss a finished core and re-arm it. The port uses the correct offset."""
    base = sell_fail_frames()
    closed_rows = [(100.0, 100.5, 99.5, 100.2)] * 33 + [
        (100.0, 101.2, 99.9, 101.0),
        (100.0, 103.2, 99.8, 103.0),
        (103.0, 103.1, 100.3, 100.5),
        (100.8, 104.2, 102.5, 104.0),
    ]
    htf = frame(closed_rows + [(104.0, 104.2, 103.8, 104.1)], "2026-11-02 09:15", "15min")
    frames = {"15minute": htf, "5minute": base["5minute"]}
    s = build("ocl_sell_fail_top", "TCS", hm_enabled=False, handoff=Handoff())
    assert s.signal_frames(frames) is not None
    assert s.st["key"] == str(htf.index[-5])            # C1 of the core, 4 closed bars back from forming
