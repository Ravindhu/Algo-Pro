"""Volatility regime from the index. Uses only data known up to each date (no look-ahead).

Flow stage: Regime detector
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/economics/regime.py
NOTE: volatility percentile states only. HMM is a later upgrade.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

LOW, NORMAL, HIGH = "low_vol", "normal", "high_vol"


def realized_vol(close: pd.Series, window: int = 20) -> pd.Series:
    return np.log(close).diff().rolling(window).std() * np.sqrt(252)


def regime_states(close: pd.Series, window: int = 20, min_history: int = 60) -> pd.Series:
    """State at date t ranks today's vol against earlier vols only."""
    vol = realized_vol(close, window)
    out = []
    hist = []
    for v in vol:
        if np.isnan(v):
            out.append(None)
            continue
        hist.append(v)
        if len(hist) < min_history:
            out.append(NORMAL)
            continue
        pct = np.mean(np.array(hist[:-1]) < v)
        out.append(LOW if pct < 0.33 else HIGH if pct > 0.67 else NORMAL)
    return pd.Series(out, index=close.index, name="regime")
