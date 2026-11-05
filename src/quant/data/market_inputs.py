"""Market inputs: flow data, bulk and block deals, relative strength, microstructure, global cues,
and the NSE board-meeting event calendar.
Inputs are local files (CSV or saved JSON). No network calls here.

Flow stage: India flow data, Bulk and block deals, Relative strength, Intraday microstructure, Filings and circulars, Global cues
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/economics/providers/fii_provider.py
Source format: https://www.nseindia.com/api/event-calendar (fields: symbol, company, purpose, bm_desc, date as DD-Mon-YYYY)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Set

import numpy as np
import pandas as pd

NSE_EVENT_DATE_FORMAT = "%d-%b-%Y"


def load_inputs(path, required: Iterable[str]) -> pd.DataFrame:
    df = pd.read_csv(Path(path), parse_dates=["date"])
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{path} missing columns: {missing}")
    return df.sort_values("date").reset_index(drop=True)


def relative_strength(stock_close: pd.Series, index_close: pd.Series, window: int = 63) -> pd.Series:
    """Stock return minus index return over the window. Uses past bars only."""
    s = stock_close / stock_close.shift(window) - 1
    i = index_close / index_close.shift(window) - 1
    return s - i


def order_book_imbalance(bid_qty: float, ask_qty: float) -> float:
    total = bid_qty + ask_qty
    return 0.0 if total <= 0 else (bid_qty - ask_qty) / total


def volume_profile(prices: pd.Series, volumes: pd.Series, bins: int = 10) -> pd.Series:
    """Share of volume traded in each price bin."""
    hist, edges = np.histogram(prices, bins=bins, weights=volumes)
    total = hist.sum()
    return pd.Series(hist / total if total else hist, index=edges[:-1], name="volume_share")


def parse_nse_event_calendar(records: Iterable[dict]) -> List[Dict[str, object]]:
    """Turn NSE event-calendar JSON records into clean event dicts.

    Bad rows (missing symbol, or a date that is not DD-Mon-YYYY) are skipped, never guessed.
    Returns [{symbol, company, purpose, date}] with date as datetime.date.
    """
    events: List[Dict[str, object]] = []
    for rec in records:
        symbol = str(rec.get("symbol", "")).strip().upper()
        try:
            day = datetime.strptime(str(rec.get("date", "")).strip(), NSE_EVENT_DATE_FORMAT).date()
        except ValueError:
            continue
        if not symbol:
            continue
        events.append({
            "symbol": symbol,
            "company": str(rec.get("company", "")).strip(),
            "purpose": str(rec.get("purpose", "")).strip(),
            "date": day,
        })
    return events


def event_blackout_dates(events: Iterable[Dict[str, object]], symbol: str,
                         days_before: int = 1, days_after: int = 1) -> Set[date]:
    """Dates to block new entries for one symbol: each event day plus a window around it.

    Feed the result to risk.controls.in_event_blackout. Events are advice-level inputs:
    they can block entries but never create orders or set size.
    """
    if days_before < 0 or days_after < 0:
        raise ValueError("blackout window must be zero or more days")
    target = symbol.strip().upper()
    blocked: Set[date] = set()
    for ev in events:
        if ev["symbol"] != target:
            continue
        d = ev["date"]
        for k in range(-days_before, days_after + 1):
            blocked.add(d + timedelta(days=k))
    return blocked
