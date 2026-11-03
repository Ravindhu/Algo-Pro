"""Load bars from local CSV cache and report feed health.

Flow stage: Market data live, Broker feeds, Feed health monitor
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/quote_health.py
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from quant.data.qa import REQUIRED


def load_bars_csv(path) -> pd.DataFrame:
    df = pd.read_csv(Path(path), parse_dates=["date"])
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"bars file missing columns: {missing}")
    return df.sort_values(["date", "symbol"]).reset_index(drop=True)


class FeedHealth:
    """Tracks the last heartbeat. A stale feed means no new signals until it recovers."""

    def __init__(self, max_gap_seconds: float = 5.0) -> None:
        self.max_gap = timedelta(seconds=max_gap_seconds)
        self.last: Optional[datetime] = None
        self.gaps = 0

    def heartbeat(self, ts: datetime) -> None:
        if self.last is not None and ts - self.last > self.max_gap:
            self.gaps += 1
        self.last = ts

    def is_stale(self, now: datetime) -> bool:
        return self.last is None or now - self.last > self.max_gap
