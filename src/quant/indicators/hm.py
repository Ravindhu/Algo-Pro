"""Flow stage: Signal and conviction (Hilega-Milega, numpy form, shared by strategies).
Status: PHASE 2 IMPLEMENTED (ported or new, with tests)
Tests: src/quant/tests/test_engulfing_strategy.py.
Note: src/quant/indicators/indicators.py has a list-based Hilega-Milega RSI and EMA. Both are kept
for now; consolidating them into one module is a later clean-up.

hm.py - the Hilega-Milega indicator (RSI with an EMA plotted over it).
Bullish = RSI above EMA(RSI); bearish = RSI below. RSI uses the simple-average convention
(mean gain / mean loss over the last rsi_period changes).

All functions are causal: reading bar j uses closes[0..j] only.
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np


def hm_rsi_series(closes: Sequence[float], rsi_period: int = 9) -> np.ndarray:
    """Simple-average RSI per bar; NaN until rsi_period bars are available."""
    c = np.asarray(closes, dtype=float)
    n = len(c)
    p = int(rsi_period)
    out = np.full(n, np.nan)
    if p <= 0 or n <= p:
        return out
    d = np.diff(c)
    g = np.concatenate([[0.0], np.cumsum(np.clip(d, 0, None))])
    lo = np.concatenate([[0.0], np.cumsum(np.clip(-d, 0, None))])
    idx = np.arange(p, n)
    gains = g[idx] - g[idx - p]
    losses = lo[idx] - lo[idx - p]
    safe = np.where(losses > 0, losses, 1.0)
    rs = np.where(losses > 0, gains / safe, np.inf)
    out[idx] = np.where(gains + losses == 0, 50.0, 100.0 - 100.0 / (1.0 + rs))
    return out


def hm_ema_series(values: Sequence[Optional[float]], period: int = 3) -> np.ndarray:
    """EMA over a series that may start with NaNs; NaN until `period` defined points exist.

    The EMA is seeded with the first defined value."""
    v = np.asarray(values, dtype=float)
    n = len(v)
    k = 2.0 / (int(period) + 1.0)
    out = np.full(n, np.nan)
    prev: Optional[float] = None
    defined = 0
    for i in range(n):
        x = v[i]
        if np.isnan(x):
            continue
        defined += 1
        prev = float(x) if prev is None else k * float(x) + (1 - k) * prev
        if defined >= period:
            out[i] = prev
    return out


def hm_direction(closes: Sequence[float], rsi_period: int = 9,
                 ema_period: int = 3) -> np.ndarray:
    """Per-bar Hilega-Milega direction: +1 RSI > EMA(RSI), -1 RSI < EMA(RSI), 0 undefined/equal."""
    rsi = hm_rsi_series(closes, rsi_period)
    line = hm_ema_series(rsi, ema_period)
    out = np.zeros(len(rsi), dtype=int)
    ok = ~np.isnan(rsi) & ~np.isnan(line)
    out[ok & (rsi > line)] = 1
    out[ok & (rsi < line)] = -1
    return out
