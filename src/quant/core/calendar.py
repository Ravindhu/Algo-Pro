"""Trading calendar loader. Holidays come from a CSV you maintain from the exchange's published list.

Flow stage: Trading calendar
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/time_utils.py
NOTE: config/calendar/holidays.csv is empty until you fill it from the NSE/BSE holiday list.
Empty means no holidays are known, so is_trading_day() returns False for weekends only and the
caller must treat any date as unverified. Do not trade on unverified dates.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Set

import pandas as pd


class TradingCalendar:
    def __init__(self, holidays: Set[date], verified: bool) -> None:
        self.holidays = holidays
        self.verified = verified

    @classmethod
    def from_csv(cls, path) -> "TradingCalendar":
        df = pd.read_csv(Path(path), parse_dates=["date"])
        if df.empty:
            return cls(set(), verified=False)
        return cls({d.date() for d in df["date"]}, verified=True)

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays
