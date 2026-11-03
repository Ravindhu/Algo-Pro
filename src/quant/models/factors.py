"""Cross-sectional factors: 12-1 momentum and low volatility. Each date uses only its past.

Flow stage: Factor and alpha models
Status: PHASE 2 IMPLEMENTED (12-1 momentum delegated to Blue_engine ml.features.engineering.momentum)
Existing code to reuse or wrap (do not copy): Blue_engine/ml/features/engineering.py (momentum)
Low-vol, z-score and composite stay here: Blue realised_vol is annualised (different scale), and
no Blue equivalent exists for the cross-sectional z-score or composite.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.core.blue_bridge import load

MOM_LONG, MOM_SKIP, VOL_WINDOW = 252, 21, 60


def momentum_12_1(close: pd.DataFrame) -> pd.DataFrame:
    """Return from t-252 to t-21 (skips the last month, a common reversal guard).

    Equals Blue momentum(window=MOM_LONG - MOM_SKIP, shift=MOM_SKIP) per column; checked
    to the NaN pattern and to 0.0 difference on random panels (tests/unit/test_phase2_portfolio.py
    and the wrapper check in this session's commit message).
    """
    blue = load("ml.features.engineering").momentum
    window = MOM_LONG - MOM_SKIP
    cols = {}
    for c in close.columns:
        vals = blue(close[c].tolist(), window, shift=MOM_SKIP)
        cols[c] = [np.nan if v is None else v for v in vals]
    return pd.DataFrame(cols, index=close.index)


def low_vol(close: pd.DataFrame) -> pd.DataFrame:
    """Negative 60-day volatility, so higher is calmer."""
    return -np.log(close).diff().rolling(VOL_WINDOW).std()


def zscore_cross_section(df: pd.DataFrame) -> pd.DataFrame:
    mu = df.mean(axis=1)
    sd = df.std(axis=1).replace(0, np.nan)
    return df.sub(mu, axis=0).div(sd, axis=0)


def composite(close: pd.DataFrame, w_mom: float = 0.7, w_vol: float = 0.3) -> pd.DataFrame:
    return w_mom * zscore_cross_section(momentum_12_1(close)) + w_vol * zscore_cross_section(low_vol(close))
