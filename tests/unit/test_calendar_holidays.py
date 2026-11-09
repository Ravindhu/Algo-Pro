"""Checks the filled NSE 2026 holiday file (data integrity, plan item 2).

Source: NSE/CMTR/71775 (12 Dec 2025, Capital Market holidays 2026), amended by NSE circular 07/2026 (12 Jan 2026, adds 2026-01-15).
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


def test_row_count_matches_official_weekday_holidays(cal):
    # NSE/CMTR/71775 (15) + circular 07/2026 adds 2026-01-15 = 16
    assert len(cal.holidays) == 16


def test_loader_csv_equals_reference_equity_rows():
    """holidays.csv must equal the equity weekday rows of nse_holidays_2026.csv."""
    import csv
    ref = pathlib.Path(ROOT / "config" / "calendar" / "nse_holidays_2026.csv")
    eq = {r["date"] for r in csv.DictReader(ref.open(encoding="utf-8"))
          if r["equity_holiday"] == "yes" and date.fromisoformat(r["date"]).weekday() < 5}
    loaded = {r["date"] for r in csv.DictReader(CSV.open(encoding="utf-8"))}
    assert loaded == eq


@pytest.mark.parametrize("day", [date(2026, 1, 26), date(2026, 10, 2), date(2026, 12, 25)])
def test_known_holidays_are_closed(cal, day):
    assert cal.is_trading_day(day) is False


def test_ordinary_weekday_is_open(cal):
    assert cal.is_trading_day(date(2026, 1, 27)) is True


def test_muhurat_sunday_is_not_a_regular_trading_day(cal):
    # Limitation: the loader cannot model special sessions. Muhurat (2026-11-08) is
    # a Sunday, so it reads as closed. Handle it separately before trading that day.
    assert cal.is_trading_day(date(2026, 11, 8)) is False
