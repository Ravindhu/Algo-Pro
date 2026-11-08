"""PCAS session clock and self-cross guard (decision 0025). Boundary cases are hand-checked.

Flow stage: Session guards, Risk gate
Status: TEST
"""
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from quant.core.pcas_schedule import (
    STATE_ENTRY_OPEN, STATE_MATCHING, STATE_OUTSIDE, STATE_RANDOM_CLOSE,
    orders_allowed, pcas_state, session_bounds, session_sequence,
)
from quant.risk.self_cross import would_self_cross

IST = ZoneInfo("Asia/Kolkata")
DAY = (2026, 11, 6)  # a Friday


def at(hhmm, sec=0):
    h, m = hhmm.split(":")
    return datetime(*DAY, int(h), int(m), sec, tzinfo=IST)


@pytest.mark.parametrize("when, state", [
    ("09:29", STATE_OUTSIDE),
    ("09:30", STATE_ENTRY_OPEN),          # first session starts
    ("10:13", STATE_ENTRY_OPEN),          # minute 43
    ("10:14", STATE_RANDOM_CLOSE),        # minute 44: unsafe
    ("10:14", STATE_RANDOM_CLOSE),
    ("10:15", STATE_MATCHING),            # minute 45
    ("10:29", STATE_MATCHING),            # minute 59
    ("10:30", STATE_ENTRY_OPEN),          # next session starts
    ("15:29", STATE_MATCHING),            # last session, minute 59
    ("15:30", STATE_OUTSIDE),             # after the last session
])
def test_state_at_boundaries(when, state):
    assert pcas_state(at(when))["state"] == state


def test_seconds_matter_at_minute_44():
    assert pcas_state(at("10:13", 59))["state"] == STATE_ENTRY_OPEN
    assert pcas_state(at("10:14", 0))["state"] == STATE_RANDOM_CLOSE


def test_minute_index_is_reported():
    out = pcas_state(at("11:45", 30))
    assert out["minute"] == 15 and out["session_start"].startswith("2026-11-06T11:30")


def test_orders_allowed_only_in_safe_entry():
    assert orders_allowed(at("09:30")) is True
    assert orders_allowed(at("10:13", 59)) is True
    assert orders_allowed(at("10:14")) is False
    assert orders_allowed(at("10:20")) is False
    assert orders_allowed(at("09:29", 59)) is False
    assert orders_allowed(at("15:30")) is False


def test_naive_time_is_read_as_ist():
    assert pcas_state(datetime(*DAY, 9, 30))["state"] == STATE_ENTRY_OPEN


def test_utc_time_converted_to_ist():
    # 04:00 UTC is 09:30 IST
    assert pcas_state(datetime(*DAY, 4, 0, tzinfo=timezone.utc))["state"] == STATE_ENTRY_OPEN


def test_session_bounds_for_noon():
    start, end = session_bounds(at("12:00"))
    assert start == at("11:30") and end == at("12:30")


def test_session_bounds_none_outside():
    assert session_bounds(at("09:00")) is None


def test_session_sequence_is_six_contiguous_hours():
    seq = session_sequence(datetime(*DAY).date())
    assert len(seq) == 6
    assert seq[0][0] == at("09:30") and seq[-1][1] == at("15:30")
    for (_, end), (start, _) in zip(seq, seq[1:]):
        assert end == start


# --- self-cross (penalty condition) ---

@pytest.mark.parametrize("side, price, resting, refused", [
    ("BUY", 100, [("SELL", 100)], True),     # equal counts as cross
    ("BUY", 99, [("SELL", 100)], False),
    ("SELL", 100, [("BUY", 99)], False),
    ("SELL", 100, [("BUY", 100)], True),
    ("SELL", 100, [("BUY", 101)], True),
    ("BUY", 100, [], False),
    ("BUY", 101, [("SELL", 100), ("SELL", 105)], True),
    ("BUY", 99, [("SELL", 100), ("SELL", 105)], False),
])
def test_limit_cross_cases(side, price, resting, refused):
    flag, reason = would_self_cross(side, price, resting)
    assert flag is refused
    assert (reason != "") is refused


def test_market_buy_with_no_sell_is_allowed():
    assert would_self_cross("BUY", None, []) == (False, "")


def test_market_buy_against_open_sell_refused():
    assert would_self_cross("BUY", None, [("SELL", 100)])[0] is True


def test_market_sell_against_open_buy_refused():
    assert would_self_cross("SELL", None, [("BUY", 100)])[0] is True


def test_market_sell_with_no_buy_is_allowed():
    assert would_self_cross("SELL", None, []) == (False, "")


def test_market_against_market_refused():
    assert would_self_cross("BUY", None, [("SELL", None)])[0] is True


def test_existing_crossed_book_is_refused_even_for_a_safe_order():
    """If the open book already crosses, a new order must not add to it."""
    assert would_self_cross("BUY", 90, [("BUY", 101), ("SELL", 100)])[0] is True


@pytest.mark.parametrize("bad", [0, -1, math.nan, math.inf, "10"])
def test_bad_limit_price_raises(bad):
    with pytest.raises(ValueError):
        would_self_cross("BUY", bad, [])


def test_bad_resting_price_raises():
    with pytest.raises(ValueError):
        would_self_cross("BUY", 100, [("SELL", 0)])


def test_bad_side_raises():
    with pytest.raises(ValueError):
        would_self_cross("HOLD", 100, [])
