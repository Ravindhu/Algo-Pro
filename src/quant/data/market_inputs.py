"""Market inputs: flow data, bulk and block deals, relative strength, microstructure, global cues.
Inputs are local CSV files. No network calls here.

Flow stage: India flow data, Bulk and block deals, Relative strength, Intraday microstructure, Filings and circulars, Global cues
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/economics/providers/fii_provider.py
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


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
