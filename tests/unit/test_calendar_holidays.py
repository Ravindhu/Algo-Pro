"""Checks the filled NSE 2026 holiday file (data integrity, plan item 2).

Source: NSE circular No. 172/2025, Download Ref. NSE/CMTR/71775 (issued 2025-12-12).
"""
import pathlib
from datetime import date

import pytest

from quant.core.calendar import TradingCalendar

ROOT = pathlib.Path(__file__).resolve().parents[2]
CSV = ROOT / "config" / "calendar" / "holidays.csv"


@pytest.fixture(scope="module")
def cal():
    return TradingCalendar.from_csv(CSV)


def test_calendar_is_verified_now(cal):
    assert cal.verified is True


def test_row_count_matches_circular_weekday_holidays(cal):
    assert len(cal.holidays) == 15


@pytest.mark.parametrize("day", [date(2026, 1, 26), date(2026, 10, 2), date(2026, 12, 25)])
def test_known_holidays_are_closed(cal, day):
    assert cal.is_trading_day(day) is False


def test_ordinary_weekday_is_open(cal):
    assert cal.is_trading_day(date(2026, 1, 27)) is True


def test_muhurat_sunday_is_not_a_regular_trading_day(cal):
    # Limitation: the loader cannot model special sessions. Muhurat (2026-11-08) is
    # a Sunday, so it reads as closed. Handle it separately before trading that day.
    assert cal.is_trading_day(date(2026, 11, 8)) is False
