"""Delivery settlement is T+1 on trading days (flow item 3). Holidays and weekends skipped."""
from datetime import date

import pytest

from quant.core.calendar import TradingCalendar


def test_friday_settles_monday_over_weekend():
    cal = TradingCalendar({date(2026, 11, 2)}, verified=True)  # some other holiday, not this week
    assert cal.settlement_date(date(2026, 11, 6)) == date(2026, 11, 9)  # Fri -> Mon


def test_holiday_on_next_day_is_skipped():
    cal = TradingCalendar({date(2026, 11, 10)}, verified=True)  # Tue holiday
    assert cal.settlement_date(date(2026, 11, 9)) == date(2026, 11, 11)  # Mon -> Wed


def test_normal_weekday_is_next_day():
    cal = TradingCalendar(set(), verified=True)
    assert cal.settlement_date(date(2026, 11, 10)) == date(2026, 11, 11)


def test_unverified_calendar_refuses_to_compute():
    cal = TradingCalendar(set(), verified=False)
    with pytest.raises(ValueError, match="not verified"):
        cal.settlement_date(date(2026, 11, 10))
