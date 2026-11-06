"""Hand-checked cases for core/session_clock.py (stage 6.5, decision 0022).

Flow stage: Trading calendar, Session guards
Status: TEST
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from quant.core.calendar import TradingCalendar
from quant.core.session_clock import (
    DEFAULT_ENTRY_CUTOFF, ist_now, is_past_cutoff, market_session_state, parse_hhmm,
)

IST = ZoneInfo("Asia/Kolkata")
OPEN_CAL = TradingCalendar(set(), verified=True)   # verified, no holidays
FRI = date(2026, 11, 6)                             # a Friday
SAT = date(2026, 11, 7)


def at(day, hhmm):
    h, m = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=IST)


@pytest.mark.parametrize("hhmm, state, is_open", [
    ("09:14", "before_open", False),
    ("09:15", "open", True),          # session open is inclusive
    ("10:00", "open", True),
    ("14:44", "open", True),
    ("14:45", "no_new_entries", True),  # default entry cutoff is inclusive
    ("15:29", "no_new_entries", True),
    ("15:30", "after_close", False),    # session close is exclusive
    ("23:59", "after_close", False),
])
def test_weekday_session_states(hhmm, state, is_open):
    out = market_session_state(at(FRI, hhmm), calendar=OPEN_CAL)
    assert out["state"] == state and out["open"] is is_open


def test_weekend_is_closed_day():
    out = market_session_state(at(SAT, "10:00"), calendar=OPEN_CAL)
    assert out["state"] == "closed_day" and not out["open"]
    assert "Saturday" in out["reason"]


def test_holiday_is_closed_day():
    cal = TradingCalendar({FRI}, verified=True)
    out = market_session_state(at(FRI, "10:00"), calendar=cal)
    assert out["state"] == "closed_day" and "exchange holiday" in out["reason"]


def test_no_calendar_fails_closed():
    out = market_session_state(at(FRI, "10:00"), calendar=None)
    assert out["state"] == "closed_day" and not out["open"]
    assert "no trading calendar" in out["reason"]


def test_unverified_calendar_fails_closed():
    empty = TradingCalendar(set(), verified=False)
    out = market_session_state(at(FRI, "10:00"), calendar=empty)
    assert out["state"] == "closed_day" and "not verified" in out["reason"]


def test_bad_entry_cutoff_falls_back_to_default_not_to_close():
    """A bad config must not mean 'no cutoff' (that would allow entries until 15:30)."""
    out = market_session_state(at(FRI, "14:50"), entry_cutoff="soon", calendar=OPEN_CAL)
    assert out["state"] == "no_new_entries"
    assert DEFAULT_ENTRY_CUTOFF == "14:45"


def test_custom_entry_cutoff_is_used():
    out = market_session_state(at(FRI, "14:50"), entry_cutoff="15:00", calendar=OPEN_CAL)
    assert out["state"] == "open"


def test_now_stamp_has_offset():
    out = market_session_state(at(FRI, "10:00"), calendar=OPEN_CAL)
    assert out["now"].endswith("+0530")


@pytest.mark.parametrize("value, now_hhmm, want", [
    ("15:15", "15:15", True),   # at the cutoff
    ("15:15", "15:14", False),
    ("15:15", "16:00", True),
])
def test_is_past_cutoff(value, now_hhmm, want):
    assert is_past_cutoff(value, now=at(FRI, now_hhmm)) is want


def test_parse_hhmm_accepts_padding():
    from datetime import time
    assert parse_hhmm(" 09:05 ") == time(9, 5)


@pytest.mark.parametrize("bad", ["abc", "9:5:1", ""])
def test_parse_hhmm_rejects_bad_text(bad):
    with pytest.raises(ValueError):
        parse_hhmm(bad)


def test_ist_now_is_aware_in_ist():
    now = ist_now()
    assert now.utcoffset().total_seconds() == 19800
