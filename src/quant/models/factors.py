"""Cross-sectional factors: 12-1 momentum and low volatility. Each date uses only its past.

Flow stage: Factor and alpha models
Status: PHASE 2 IMPLEMENTED (stage 2 port 2026-11-06: 12-1 momentum is local)
Ported from: src/quant/blue_engine/ml/features/engineering.py (momentum, _shift). Parity:
tests/parity/test_momentum_parity.py.
Low-vol, z-score and composite stay here: Blue realised_vol is annualised (different scale), and
no Blue equivalent exists for the cross-sectional z-score or composite.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MOM_LONG, MOM_SKIP, VOL_WINDOW = 252, 21, 60


def momentum(close: pd.DataFrame, window: int, shift: int = 1) -> pd.DataFrame:
    """Total return over `window` bars, ending at `t - shift`, per column.

    Same rules as Blue: rows before the window are NaN; a missing or zero base is NaN;
    a negative shift is refused (it would read the future).
    """
    if shift < 0:
        raise ValueError(f"negative shift ({shift}) would read the future")
    shifted = close.shift(shift)            # look back only
    past = shifted.shift(window)
    return shifted / past.where(past != 0) - 1.0


def momentum_12_1(close: pd.DataFrame) -> pd.DataFrame:
    """Return from t-252 to t-21 (skips the last month, a common reversal guard)."""
    return momentum(close, MOM_LONG - MOM_SKIP, shift=MOM_SKIP)


def low_vol(close: pd.DataFrame) -> pd.DataFrame:
    """Negative 60-day volatility, so higher is calmer."""
    return -np.log(close).diff().rolling(VOL_WINDOW).std()


def zscore_cross_section(df: pd.DataFrame) -> pd.DataFrame:
    mu = df.mean(axis=1)
    sd = df.std(axis=1).replace(0, np.nan)
    return df.sub(mu, axis=0).div(sd, axis=0)


def composite(close: pd.DataFrame, w_mom: float = 0.7, w_vol: float = 0.3) -> pd.DataFrame:
    return w_mom * zscore_cross_section(momentum_12_1(close)) + w_vol * zscore_cross_section(low_vol(close))
