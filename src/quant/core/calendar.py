"""Trading calendar loader. Holidays come from a CSV you maintain from the exchange's published list.

Flow stage: Trading calendar
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/time_utils.py
NOTE: config/calendar/holidays.csv holds the 2026 NSE holidays. An empty file means no holidays
are known. Then is_trading_day() returns True for every weekday, so the caller must treat such
dates as unverified (TradingCalendar.verified is False). Do not trade on unverified dates.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import List, Set, Tuple

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

    def next_trading_day(self, day: date) -> date:
        d = day + timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def settlement_date(self, trade_day: date) -> date:
        """Delivery (CNC) settles T+1 on trading days. Raises if the calendar is not verified."""
        if not self.verified:
            raise ValueError("calendar not verified: cannot compute settlement date")
        return self.next_trading_day(trade_day)


def calendar_coverage(coverage_path, today: date, horizon_days: int = 30) -> Tuple[bool, List[str]]:
    """Is the holiday list known to cover the next `horizon_days` days? coverage.yaml holds
    `covered_through: YYYY-MM-DD`, set by the owner after checking the NSE circulars for those dates.
    Missing or unreadable = not covered (fail closed)."""
    import yaml

    p = Path(coverage_path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        through = date.fromisoformat(str(data["covered_through"]))
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
        return False, ["holiday coverage file missing or unreadable (config/calendar/coverage.yaml)"]
    if today + timedelta(days=horizon_days) > through:
        return False, [f"holiday list is only verified through {through.isoformat()}: "
                       f"extend it from the NSE circulars before live"]
    return True, []
