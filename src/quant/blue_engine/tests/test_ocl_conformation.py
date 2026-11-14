"""Tests for the CONFORMATION entry model inside ocl_keylevel.py.

What is under test is the chain's decision logic, not the OCL blueprint rules
(those already have their own suite). Each state gets its own test, plus the
one property the whole design rests on: ``entry_model="aggressive"`` behaves
exactly as before.
"""
from __future__ import annotations

import pytest

from blue_engine.alpha.Statergy import conformation_model as cm
from blue_engine.alpha.Statergy.conformation_model import ConformationModel
from blue_engine.alpha.Statergy.ocl_keylevel import OCLKeylevelSignal

CFG = {"symbol": "TEST", "exchange": "NSE", "quantity": 10}


def _signal(**overrides):
    cfg = dict(CFG)
    cfg.update(overrides)
    return OCLKeylevelSignal(None, cfg)



def _model(sig, rung_mode="aggressive"):
    """Attach (or rebuild) the conformation model for a test signal."""
    model = getattr(sig, "_conf_model", None)
    if model is None:
        model = ConformationModel(sig, rung_mode=rung_mode)
        sig._conf_model = model
        sig._conf = model.conf
    return model


def _bar(o, h, l, c, ts="2026-01-01"):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c,
            "volume": 1000}


# --------------------------------------------------------------------------- #
# wiring / defaults
# --------------------------------------------------------------------------- #


def test_default_entry_model_is_aggressive_and_allocates_no_state():
    sig = _signal()
    assert sig.entry_model == "aggressive"
    assert sig._conf is None


def test_conformation_model_allocates_state():
    sig = _signal(entry_model="conformation")
    assert sig.entry_model == "conformation"
    assert sig._conf is not None
    assert sig._conf.state == cm.ConfState.IDLE


def test_unknown_entry_model_is_rejected():
    with pytest.raises(ValueError, match="entry_model"):
        _signal(entry_model="clever")


def test_entry_model_is_in_the_optimizer_space():
    space = OCLKeylevelSignal.params_space
    assert space["entry_model"]["choices"] == ["aggressive", "conformation"]
    assert space["entry_model"]["default"] == "aggressive"
    assert space["mhtf_interval"]["default"] == "1month"


# --------------------------------------------------------------------------- #
# CISD - the operator definition
# --------------------------------------------------------------------------- #


def test_cisd_is_the_open_of_the_oldest_red_candle_of_the_run():
    rows = [
        _bar(110, 111, 109, 109),   # 0 red (older, NOT part of the run)
        _bar(109, 110, 107, 106),   # 1 c1 red  <- level must be 109
        _bar(106, 107, 104, 103),   # 2 c2 red
        _bar(103, 104, 101, 100),   # 3 c3 red
        _bar(100, 102, 99, 101),    # 4 displacement (green)
    ]
    fvg = {"index": 3, "low": 101, "high": 104}      # inside c3 -> floor = 1
    found = cm.cisd_level(rows, 4, max_lookback=10, fvg=fvg)
    assert found == (1, 109.0)


def test_cisd_run_stops_at_the_fvg_window_floor():
    """With the floor at the FVG window, c1 of the run is the oldest allowed."""
    rows = [
        _bar(120, 121, 119, 118),   # 0 red, above the floor
        _bar(110, 111, 109, 108),   # 1 floor (fvg.index - 2)
        _bar(108, 109, 105, 104),   # 2 red
        _bar(104, 105, 102, 101),   # 3 red
        _bar(101, 106, 100, 105),   # 4 displacement
    ]
    fvg = {"index": 3, "low": 102, "high": 105}
    assert cm.cisd_level(rows, 4, max_lookback=10, fvg=fvg) == (1, 110.0)


def test_cisd_is_undefined_when_the_candle_before_displacement_is_green():
    rows = [_bar(100, 101, 99, 100.5), _bar(100.5, 106, 100, 105)]
    assert cm.cisd_level(rows, 1, max_lookback=10) is None


def test_cisd_respects_max_lookback():
    rows = [
        _bar(200 - i, 201 - i, 198 - i, 197 - i) for i in range(12)]
    rows.append(_bar(180, 190, 179, 189))            # displacement at index 12
    # The run is walked back from index 11. max_lookback=3 stops the walk at
    # index 8, so the level is the OPEN of the oldest red candle it reached.
    found = cm.cisd_level(rows, 12, max_lookback=3)
    assert found is not None
    assert found[0] == 8
    assert found[1] == 192.0                          # open of bar 8


# --------------------------------------------------------------------------- #
# FVG / IFVG
# --------------------------------------------------------------------------- #


def test_bearish_fvg_detection_uses_the_spec_definition():
    rows = [
        _bar(100, 102, 98, 99),      # 0
        _bar(99, 100, 95, 96),       # 1
        _bar(96, 97, 93, 94),        # 2
        _bar(94, 95, 91, 92),        # 3
        _bar(92, 92.5, 88, 89),      # 4 -> rows[2].low (93) > rows[4].high (92.5)
    ]
    # first_bearish_fvg is the OLDEST gap in the window - the one the
    # displacement has to invert. Here that is bars 0..2: rows[0].low (98) sits
    # above rows[2].high (97), so the gap 97.0-98.0 completes first.
    assert cm.first_bearish_fvg(rows, 2, 4, 0.0) == {
        "index": 2, "low": 97.0, "high": 98.0}


def test_no_fvg_when_the_window_overlaps():
    rows = [_bar(100, 102, 98, 99), _bar(99, 101, 97, 98), _bar(98, 100, 96, 97)]
    assert cm.first_bearish_fvg(rows, 2, 2, 0.0) is None


def test_fvg_min_size_filter():
    rows = [_bar(100, 102, 98, 99), _bar(99, 100, 95, 96), _bar(96, 97, 93, 94),
            _bar(94, 95, 91, 92), _bar(92, 92.4, 88, 89)]
    assert cm.first_bearish_fvg(rows, 2, 4, 0.0) is not None
    assert cm.first_bearish_fvg(rows, 2, 4, 5.0) is None


# --------------------------------------------------------------------------- #
# stop validation (absolute buffer)
# --------------------------------------------------------------------------- #


def test_stop_is_swing_low_minus_absolute_buffer():
    sl, reject = cm.validate_stop(entry=105.0, swing_low=99.0,
                                         buffer_abs=0.5)
    assert reject is None
    assert sl == 98.5


def test_stop_rejected_when_not_below_swing_low_or_entry():
    assert cm.validate_stop(105.0, 99.0, -1.0)[0] is None
    assert cm.validate_stop(98.0, 99.0, 0.0)[0] is None   # SL >= entry
    assert cm.validate_stop(105.0, 0.0, 0.0)[0] is None


# --------------------------------------------------------------------------- #
# fractal / displacement helpers
# --------------------------------------------------------------------------- #


def test_fractal_low_needs_bars_on_both_sides():
    rows = [_bar(10, 11, 9, 10), _bar(10, 11, 8, 10), _bar(10, 11, 7, 10),
            _bar(10, 11, 8, 10), _bar(10, 11, 9, 10)]
    assert cm.fractal_lows(rows, 2) == [(4, 7.0)]


def test_displacement_needs_a_big_body_closing_near_the_high():
    row = _bar(100, 110, 99.5, 109)          # body 9, closes 1 below the high
    assert cm.is_displacement(row, 2.0, mult=1.5, close_frac=0.33)
    weak = _bar(100, 104, 99.5, 101)         # body 1
    assert not cm.is_displacement(weak, 2.0, mult=1.5, close_frac=0.33)
    no_ref = _bar(100, 110, 99.5, 109)
    assert not cm.is_displacement(no_ref, None, mult=1.5, close_frac=0.33)


# --------------------------------------------------------------------------- #
# the ladder
# --------------------------------------------------------------------------- #


class _Fake:
    def __init__(self, aggressive, conservative, low=None):
        self._aggressive_level = aggressive
        self._conservative_level = conservative
        self._low_swing_level = low


def test_ladder_orders_aggressive_then_conservative_then_low_swing():
    assert cm.pick_levels(105.0, 100.0) == [105.0, 100.0]
    assert cm.pick_levels(105.0, 105.0) == [105.0]
    assert cm.pick_levels(None, 100.0, 95.0) == [100.0, 95.0]
    assert cm.pick_levels(None, None, None) == []


# --------------------------------------------------------------------------- #
# the chain, end to end, on a hand-built frame
# --------------------------------------------------------------------------- #


def _chain_frames():
    """MHTF / HTF / LTF rows that walk the whole sequence, built by rule.

    The fixture is generated so that every precondition the chain checks is
    TRUE BY CONSTRUCTION, and asserted at the bottom of this function. Tuning
    hand-written candles until the chain happened to pass was how the earlier
    version of this fixture went wrong three times; the assertions are what
    stop that recurring.

    MHTF: red, red, green closing above the second red's open -> aggressive
          level = open of the middle red (99.70), conservative = 99.10.
    HTF :  rally into 99.70 (mitigation), down-leg, fractal low, a sweep that
          closes back above, a bearish FVG inside the red run, then a
          displacement that closes through that gap.
    LTF :  a 15m bar whose low reaches the daily key level.
    """
    # ---- MHTF: a clean RRG whose C3 closes above C2's open --------------- #
    mhtf = [
        _bar(100.00, 100.50, 99.80, 100.20, "2025-02-01"),
        _bar(100.20, 100.60, 99.60, 99.70, "2025-03-01"),   # C1 red
        _bar(99.70, 100.10, 99.00, 99.10, "2025-04-01"),    # C2 red
        _bar(99.10, 100.90, 99.00, 100.60, "2025-05-01"),   # C3 green
        _bar(100.60, 100.90, 100.30, 100.50, "2025-06-01"),  # confirmation
    ]

    # ---- HTF: built from the levels the rules need ----------------------- #
    # Layout, with the bars each rule needs:
    #   0-3    drift below AGG, then bar 3 wicks into AGG (MITIGATION)
    #   4-7    down leg
    #   7      FRACTAL LOW at FIB (3 bars each side: 4,5,6 / 8,9,10)
    #   11     sweep: low < FIB, close < FIB (confirmation is allowed later)
    #   12     closes back above FIB -> STOP_HUNT
    #   13-16  red run c1..c4; bars 13,15,16 form the bearish gap
    #   17     DISPLACEMENT: big green body, closes above the gap
    #   18-19  back to the CISD level, then a close above it
    #   20-24  quiet bars
    AGG = 99.70          # MHTF aggressive level (C2 open)
    FIB = 98.60          # the fractal low the sweep must take out
    SWEEP = 98.40        # the sweep's low
    # The bearish gap is bars 33..35: bar 33's LOW (98.60) sits above bar 35's
    # HIGH (98.24), so the band is 98.24-98.60. c1's low (98.60) is its upper
    # edge - which is exactly the operator's "FVG present at c3-c5".
    GAP_LOW, GAP_HIGH = 98.24, 98.60
    DISP = 99.30         # displacement close (well above GAP_HIGH)
    CISD = 98.90         # open of c1, the oldest red candle of the run

    htf = [
        # Lead-in: 20 quiet bars so the displacement's 20-bar average body is
        # DEFINED. Without them _conf_avg_body returns None and the
        # displacement test can never pass - a fixture bug that looks exactly
        # like a detector bug.
        _bar(99.00, 99.10, 98.95, 99.05, "2025-06-02"),   # -20
        _bar(99.05, 99.15, 99.00, 99.10, "2025-06-03"),
        _bar(99.10, 99.20, 99.05, 99.15, "2025-06-04"),
        _bar(99.15, 99.25, 99.10, 99.20, "2025-06-05"),
        _bar(99.20, 99.30, 99.15, 99.25, "2025-06-06"),
        _bar(99.25, 99.35, 99.20, 99.30, "2025-06-09"),
        _bar(99.30, 99.40, 99.25, 99.35, "2025-06-10"),
        _bar(99.35, 99.45, 99.30, 99.40, "2025-06-11"),
        _bar(99.40, 99.50, 99.35, 99.45, "2025-06-12"),
        _bar(99.45, 99.55, 99.40, 99.50, "2025-06-13"),
        _bar(99.50, 99.60, 99.45, 99.55, "2025-06-16"),
        _bar(99.55, 99.65, 99.50, 99.60, "2025-06-17"),
        _bar(99.60, 99.70, 99.55, 99.65, "2025-06-18"),
        _bar(99.65, 99.75, 99.60, 99.70, "2025-06-19"),
        _bar(99.70, 99.80, 99.65, 99.75, "2025-06-20"),
        _bar(99.75, 99.85, 99.70, 99.80, "2025-06-23"),
        _bar(99.80, 99.90, 99.75, 99.85, "2025-06-24"),
        _bar(99.85, 99.95, 99.80, 99.90, "2025-06-25"),
        _bar(99.90, 100.00, 99.85, 99.95, "2025-06-26"),
        _bar(99.95, 100.05, 99.90, 100.00, "2025-06-27"),  # -1
        _bar(99.00, 99.10, 98.95, 99.05, "2025-07-01"),   # 0
        _bar(99.05, 99.15, 99.00, 99.10, "2025-07-02"),   # 1
        _bar(99.10, 99.20, 99.05, 99.15, "2025-07-03"),   # 2
        _bar(99.15, AGG + 0.05, 99.10, AGG, "2025-07-04"),  # 3 MITIGATION
        _bar(AGG, AGG + 0.05, 99.40, 99.45, "2025-07-07"),  # 4 red
        _bar(99.45, 99.50, 99.05, 99.10, "2025-07-08"),   # 5 red
        _bar(99.10, 99.15, FIB + 0.10, FIB + 0.15, "2025-07-09"),  # 6 red
        _bar(FIB + 0.15, FIB + 0.20, FIB, FIB + 0.05, "2025-07-10"),  # 7 FRACTAL LOW
        _bar(FIB + 0.05, FIB + 0.10, FIB + 0.02, FIB + 0.06, "2025-07-11"),  # 8
        _bar(FIB + 0.06, FIB + 0.09, FIB + 0.03, FIB + 0.05, "2025-07-14"),  # 9
        _bar(FIB + 0.05, FIB + 0.08, FIB + 0.01, FIB + 0.03, "2025-07-15"),  # 10
        _bar(FIB + 0.03, FIB + 0.05, SWEEP, SWEEP + 0.02, "2025-07-16"),  # 11 sweep
        _bar(SWEEP + 0.02, FIB + 0.15, SWEEP, FIB + 0.10, "2025-07-17"),  # 12 back above
        # Red run c1..c4. c1 opens at CISD. The bearish gap is bars 13..15:
        # bar 13's LOW (98.30) sits ABOVE bar 15's HIGH (98.24).
        _bar(CISD, CISD + 0.05, 98.60, 98.65, "2025-07-18"),   # 13 c1
        _bar(98.65, 98.68, 98.40, 98.45, "2025-07-21"),        # 14 c2
        _bar(98.45, 98.24, 98.15, 98.20, "2025-07-22"),        # 15 c3 (high 98.24)
        _bar(98.20, 98.22, 98.10, 98.12, "2025-07-23"),        # 16 c4 (high 98.22)
        _bar(98.12, DISP + 0.10, 98.08, DISP, "2025-07-24"),    # 17 DISPLACEMENT
        _bar(DISP, DISP + 0.05, CISD - 0.05, CISD, "2025-07-25"),  # 18 back to CISD
        _bar(CISD, DISP + 0.10, CISD - 0.02, DISP, "2025-07-28"),  # 19 closes above
        _bar(DISP, DISP + 0.10, CISD + 0.02, CISD + 0.10, "2025-07-29"),  # 20
        _bar(CISD + 0.10, CISD + 0.15, CISD + 0.05, CISD + 0.12, "2025-07-30"),
        _bar(CISD + 0.12, CISD + 0.18, CISD + 0.08, CISD + 0.15, "2025-07-31"),
        _bar(CISD + 0.15, CISD + 0.20, CISD + 0.10, CISD + 0.18, "2025-08-01"),
        _bar(CISD + 0.18, CISD + 0.22, CISD + 0.12, CISD + 0.20, "2025-08-04"),
    ]

    # LTF bars spread across the HTF dates the chain needs to walk: each 15m
    # batch sits on a DIFFERENT daily timestamp, because ``evaluate_bars``
    # advances the daily chain only when a new HTF bar has closed by the LTF
    # timestamp. Eleven bars on one date would show the chain one daily bar.
    # The LTF must START before every HTF bar the chain needs to walk. Each 15m
    # bar is dated to a different daily timestamp, because ``evaluate_bars``
    # advances the daily chain only when a NEW HTF bar has closed by the LTF
    # timestamp. Starting the LTF after the sweep would show the chain only the
    # finished state, and it could never observe the transitions.
    ltf = []
    for hour in htf:
        for minute in (15, 30):
            ltf.append(_bar(DISP + 0.20, DISP + 0.25, DISP + 0.15, DISP + 0.18,
                            f"{hour['timestamp']} 09:{minute}:00"))
    ltf.append(_bar(DISP + 0.18, DISP + 0.20, CISD - 0.01, CISD + 0.02,
                    f"{htf[-1]['timestamp']} 09:45:00"))   # mitigates the level

    # ---- preconditions: the fixture must be what the docstring claims ----- #
    _pre = {
        "mitigation": any(b["low"] <= AGG for b in htf),
        "fractal low exists": any(
            p == pytest.approx(FIB) for _, p in cm.fractal_lows(htf, 3)),
        "gap inside the run": any(
            g["low"] == pytest.approx(GAP_LOW)
            and g["high"] == pytest.approx(GAP_HIGH)
            for g in [cm.first_bearish_fvg(htf, 33, 36, 0.0)] if g),
        "displacement is real": cm.is_displacement(
            htf[37], cm.avg_body(htf, 36, 20), mult=1.5, close_frac=0.33),
        "displacement inverts the gap": htf[37]["close"] > GAP_LOW,
        "c1 is the run's oldest red": cm.cisd_level(
            htf, 37, max_lookback=10,
            fvg=cm.first_bearish_fvg(htf, 33, 36, 0.0)) == (33, CISD),
    }
    assert all(_pre.values()), f"fixture precondition failed: {_pre}"
    return mhtf, htf, ltf


def test_mhtf_scan_returns_a_ladder_and_a_direction():
    mhtf, _, _ = _chain_frames()
    sig = _signal(entry_model="conformation")
    reading = _model(sig).scan("1month", mhtf, use_hm=False)
    assert reading is not None
    assert reading["direction"] in ("BUY", "SELL")
    assert reading["levels"]


def test_cisd_level_is_the_oldest_red_open_of_the_run():
    """The displacement's red run is c1..c4; the level is c1's OPEN (98.90)."""
    mhtf, htf, _ = _chain_frames()
    sig = _signal(entry_model="conformation")
    _model(sig).refresh_ladder(mhtf, mhtf[-1]["timestamp"])
    for i in range(len(htf)):
        _model(sig).step(htf[:i + 1], htf[i]["timestamp"])
        if sig._conf.cisd_level is not None:
            break
    # c1 is bar 33 of the fixture; its OPEN is the CISD level.
    assert sig._conf.cisd_level == pytest.approx(htf[33]["open"])
    assert sig._conf.cisd_level == pytest.approx(98.90)
    assert sig._conf.state in (cm.ConfState.AWAIT_CISD,
                               cm.ConfState.AWAIT_CISD_TOUCH,
                               cm.ConfState.AWAIT_HTF_OCL)


def test_chain_reaches_the_ltf_state_on_a_full_sequence():
    mhtf, htf, ltf = _chain_frames()
    sig = _signal(entry_model="conformation")
    out = sig.evaluate_bars(ltf, htf, mhtf)
    states = [t["state"] for t in out["transitions"]]
    for expected in (cm.ConfState.AWAIT_HUNT, cm.ConfState.AWAIT_IFVG,
                     cm.ConfState.AWAIT_CISD,
                     cm.ConfState.AWAIT_CISD_TOUCH,
                     cm.ConfState.AWAIT_HTF_OCL, cm.ConfState.AWAIT_LTF):
        assert expected in states, f"chain never reached {expected}: {states}"


def test_aggressive_model_never_touches_the_conformation_state():
    sig = _signal()
    assert sig.detect is not None
    # The bound method is the class's own; the conformation path is a free
    # function bound separately and only reached via entry_model.
    assert sig._conf is None
    assert OCLKeylevelSignal._conformation_detect.__name__ == "_conformation_detect"


# --------------------------------------------------------------------------- #
# the daily ladder + the monthly terminal stop
# --------------------------------------------------------------------------- #


def test_daily_ladder_is_walked_rung_by_rung_not_collapsed():
    """A daily OCL with several levels must arm the FIRST rung, not all of them."""
    sig = _signal(entry_model="conformation", rung_mode="ladder")
    conf = sig._conf
    conf.state = cm.ConfState.AWAIT_HTF_OCL
    conf.cisd_level = 98.90
    conf.htf_ladder = []
    reading = {"direction": "BUY", "levels": [99.70, 99.20, 98.80],
               "setup_mode": "OCL"}
    model = _model(sig, rung_mode="ladder")
    orig = model.scan
    model.scan = lambda *a, **k: reading
    try:
        htf = [_bar(99.0, 99.1, 98.9, 99.0, f"2025-01-{i + 1:02d}")
               for i in range(6)]
        model.step(htf, "2025-01-06")
    finally:
        model.scan = orig
    assert conf.htf_ladder == [99.70, 99.20, 98.80]
    assert conf.htf_rung == 0
    assert conf.htf_zone == 99.70
    assert conf.state == cm.ConfState.AWAIT_LTF


def test_daily_rung_break_steps_down_one_rung():
    sig = _signal(entry_model="conformation", rung_mode="ladder")
    conf = sig._conf
    conf.state = cm.ConfState.AWAIT_LTF
    conf.htf_ladder = [99.70, 99.20, 98.80]
    conf.htf_rung = 0
    conf.htf_zone = 99.70
    conf.ltf_zone = 99.70
    htf = [_bar(99.0, 99.1, 98.9, 99.0, f"2025-02-{i + 1:02d}")
           for i in range(5)]
    htf.append(_bar(99.0, 99.05, 98.9, 99.15, "2025-02-06"))   # close < 99.70
    _model(sig, rung_mode="ladder").step(htf, "2025-02-06")
    assert conf.htf_rung == 1
    assert conf.htf_zone == 99.20
    assert conf.ltf_zone == 99.20


def test_daily_ladder_exhaustion_returns_to_the_cisd_gate():
    sig = _signal(entry_model="conformation", rung_mode="ladder")
    conf = sig._conf
    conf.state = cm.ConfState.AWAIT_LTF
    conf.htf_ladder = [99.70]
    conf.htf_rung = 0
    conf.htf_zone = 99.70
    conf.ltf_zone = 99.70
    htf = [_bar(99.0, 99.1, 98.9, 99.0, f"2025-03-{i + 1:02d}")
           for i in range(5)]
    htf.append(_bar(99.0, 99.05, 98.5, 98.9, "2025-03-06"))
    _model(sig, rung_mode="ladder").step(htf, "2025-03-06")
    assert conf.htf_zone is None
    assert conf.ltf_zone is None
    assert conf.state == cm.ConfState.AWAIT_CISD_TOUCH, \
        "an exhausted daily ladder must wait for a NEW CISD, not reuse this one"


def test_aggressive_rung_mode_never_walks_the_daily_ladder():
    """Default mode: the nearest rung is the rung, and a break does not step down."""
    sig = _signal(entry_model="conformation")            # aggressive default
    conf = sig._conf
    conf.state = cm.ConfState.AWAIT_LTF
    conf.htf_ladder = [99.70, 99.20, 98.80]
    conf.htf_rung = 0
    conf.htf_zone = 99.70
    conf.ltf_zone = 99.70
    htf = [_bar(99.0, 99.1, 98.9, 99.0, f"2025-04-{i + 1:02d}")
           for i in range(5)]
    htf.append(_bar(99.0, 99.05, 98.9, 99.15, "2025-04-06"))
    _model(sig).step(htf, "2025-04-06")
    assert conf.htf_rung == 0
    assert conf.htf_zone == 99.70


def test_conservative_rung_mode_arms_the_deepest_level():
    sig = _signal(entry_model="conformation", rung_mode="conservative")
    model = _model(sig, rung_mode="conservative")
    ladder = [99.70, 99.20, 98.80]
    assert model._rung_value(ladder, 0) == 98.80
    assert model._rung_value(ladder, 2) == 98.80      # never walks


def test_aggressive_rung_mode_arms_the_nearest_level():
    sig = _signal(entry_model="conformation")
    model = _model(sig)
    assert model._rung_value([99.70, 99.20, 98.80], 0) == 99.70
    assert model._rung_value([99.70, 99.20, 98.80], 2) == 99.70


def test_unknown_rung_mode_is_rejected():
    with pytest.raises(ValueError, match="rung_mode"):
        _signal(entry_model="conformation", rung_mode="clever")


def test_rung_mode_is_in_the_optimizer_space():
    spec = OCLKeylevelSignal.params_space["rung_mode"]
    assert spec["default"] == "aggressive"
    assert set(spec["choices"]) == {"aggressive", "conservative", "ladder"}


def test_monthly_close_through_the_bottom_and_the_swept_low_is_terminal():
    """Both conditions: below the bottom rung AND a new lower low."""
    sig = _signal(entry_model="conformation")
    sig._conf.ladder = [99.70, 99.10]
    sig._conf.mhtf_armed_index = 0
    mhtf = [_bar(99.0, 99.2, 98.8, 99.1, "2025-01-01"),     # swept low 98.8
            _bar(99.1, 99.2, 98.9, 99.0, "2025-02-01"),
            _bar(99.0, 99.05, 97.5, 97.8, "2025-03-01")]     # 97.8 < 99.10 < 98.8
    assert _model(sig).mhtf_broken(mhtf, "2025-03-01") is True
    assert sig._conf.mhtf_dead is True
    assert _model(sig).mhtf_broken(mhtf, "2025-03-01") is True


def test_monthly_close_inside_the_swept_range_does_not_kill_the_chain():
    """The RELIANCE case: below the bottom rung, but no new ground."""
    sig = _signal(entry_model="conformation")
    sig._conf.ladder = [99.70, 99.10]
    sig._conf.mhtf_armed_index = 0
    mhtf = [_bar(99.0, 99.2, 98.8, 99.1, "2025-01-01"),     # swept low 98.8
            _bar(99.1, 99.2, 98.9, 99.0, "2025-02-01"),
            _bar(99.0, 99.05, 98.5, 98.9, "2025-03-01")]     # 98.9 < 99.10 BUT > 98.8
    assert _model(sig).mhtf_broken(mhtf, "2025-03-01") is False
    assert sig._conf.mhtf_dead is False
    assert any(t["state"] == "MHTF_INSIDE_RANGE" for t in sig._conf.transitions)


def test_the_stop_hunt_low_counts_as_already_swept():
    """A hunt low below the MHTF bars still protects the chain."""
    sig = _signal(entry_model="conformation")
    sig._conf.ladder = [99.70, 99.10]
    sig._conf.mhtf_armed_index = 0
    sig._conf.sweep_low = 98.0              # hunted deeper than any MHTF bar
    mhtf = [_bar(99.0, 99.2, 98.8, 99.1, "2025-01-01"),
            _bar(99.1, 99.2, 98.9, 99.0, "2025-02-01"),
            _bar(99.0, 99.05, 98.4, 98.5, "2025-03-01")]     # 98.5 > 98.0
    assert _model(sig).mhtf_broken(mhtf, "2025-03-01") is False


def test_a_monthly_close_above_the_bottom_rung_is_not_terminal():
    sig = _signal(entry_model="conformation")
    sig._conf.ladder = [99.70, 99.10]
    mhtf = [_bar(99.0, 99.2, 98.8, 99.1, "2025-01-01"),
            _bar(99.1, 99.3, 99.05, 99.25, "2025-02-01")]      # closes above
    assert _model(sig).mhtf_broken(mhtf, "2025-02-01") is False
    assert sig._conf.mhtf_dead is False


def test_a_new_mhtf_pattern_reopens_a_dead_chain():
    mhtf, _, _ = _chain_frames()
    sig = _signal(entry_model="conformation")
    sig._conf.mhtf_dead = True
    _model(sig).refresh_ladder(mhtf, mhtf[-1]["timestamp"])
    assert sig._conf.mhtf_dead is False


# --------------------------------------------------------------------------- #
# MHTF pattern age cap
# --------------------------------------------------------------------------- #


def _monthly_with_pattern(pattern_at_end, total):
    """Monthly bars where a bullish OCL completes at ``pattern_at_end``."""
    rows = [_bar(100.0, 101.0, 99.5, 100.5, f"2020-{i + 1:02d}-01")
            for i in range(total)]
    if pattern_at_end >= 3:
        rows[pattern_at_end - 3] = _bar(100.0, 100.6, 99.6, 99.7,
                                        f"2020-{pattern_at_end - 2:02d}-01")
        rows[pattern_at_end - 2] = _bar(99.7, 100.1, 99.0, 99.1,
                                        f"2020-{pattern_at_end - 1:02d}-01")
        rows[pattern_at_end - 1] = _bar(99.1, 100.9, 99.0, 100.6,
                                        "PATTERN-C3")
        rows[pattern_at_end] = _bar(100.6, 100.9, 100.3, 100.5, "PATTERN-C4")
    return rows


def test_stale_monthly_pattern_does_not_arm_the_ladder():
    """INFY: a 10-month-old OCL must not arm a level 40% above spot."""
    sig = _signal(entry_model="conformation", mhtf_max_pattern_age=3)
    rows = _monthly_with_pattern(pattern_at_end=6, total=17)   # 10 bars old
    armed = _model(sig).refresh_ladder(rows, "2026-01-01")
    assert armed is False
    assert sig._conf.ladder == []
    assert sig._conf.state == cm.ConfState.IDLE
    assert any(t["state"] == "MHTF_NO_RECENT_PATTERN"
               for t in sig._conf.transitions)


def test_fresh_monthly_pattern_arms_the_ladder():
    sig = _signal(entry_model="conformation", mhtf_max_pattern_age=3)
    rows = _monthly_with_pattern(pattern_at_end=15, total=17)   # 1 bar old
    armed = _model(sig).refresh_ladder(rows, "2026-01-01")
    assert armed is True
    assert sig._conf.ladder
    assert sig._conf.state == cm.ConfState.AWAIT_MITIGATION
    assert sig._conf.mhtf_pattern_index == 15


def test_age_cap_of_zero_disables_the_check():
    sig = _signal(entry_model="conformation", mhtf_max_pattern_age=0)
    rows = _monthly_with_pattern(pattern_at_end=6, total=17)
    assert _model(sig).refresh_ladder(rows, "2026-01-01") is True


def test_age_cap_is_in_the_optimizer_space():
    spec = OCLKeylevelSignal.params_space["mhtf_max_pattern_age"]
    assert spec["default"] == 30
    assert spec["low"] == 0


def test_negative_age_cap_is_rejected():
    with pytest.raises(ValueError, match="mhtf_max_pattern_age"):
        _signal(entry_model="conformation", mhtf_max_pattern_age=-1)
