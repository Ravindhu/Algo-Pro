"""Cross-sectional factors: 12-1 momentum and low volatility. Each date uses only its past.

Flow stage: Factor and alpha models
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/alpha/signals/keylevel.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MOM_LONG, MOM_SKIP, VOL_WINDOW = 252, 21, 60


def momentum_12_1(close: pd.DataFrame) -> pd.DataFrame:
    """Return from t-252 to t-21 (skips the last month, a common reversal guard)."""
    return close.shift(MOM_SKIP) / close.shift(MOM_LONG) - 1


def low_vol(close: pd.DataFrame) -> pd.DataFrame:
    """Negative 60-day volatility, so higher is calmer."""
    return -np.log(close).diff().rolling(VOL_WINDOW).std()


def zscore_cross_section(df: pd.DataFrame) -> pd.DataFrame:
    mu = df.mean(axis=1)
    sd = df.std(axis=1).replace(0, np.nan)
    return df.sub(mu, axis=0).div(sd, axis=0)


def composite(close: pd.DataFrame, w_mom: float = 0.7, w_vol: float = 0.3) -> pd.DataFrame:
    return w_mom * zscore_cross_section(momentum_12_1(close)) + w_vol * zscore_cross_section(low_vol(close))
