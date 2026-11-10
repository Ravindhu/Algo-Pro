"""Flow stage: Signal and conviction (indicators, shared by strategies).
Status: PORTED COPY of Blue_engine/alpha/confirm/roc.py. Tests: src/quant/tests/test_signals_roc_state.py.

z-scored Rate-of-Change momentum confirmation.

Raw ROC is:
    ROC(N) = (price_now - price_N_bars_ago) / price_N_bars_ago * 100

Dividing ROC by its rolling standard deviation makes it comparable across
volatility regimes: the result is how many "typical moves" the current move
represents (the z-score).

Usage:
    from quant.indicators.roc import zscored_roc, find_flip

    z = zscored_roc(closes, period=10, z_window=100)
    idx = find_flip(z, 0, entry_z=1.0, direction="BUY")   # first bullish flip

Both functions are pure (no I/O, no global state).
"""
from __future__ import annotations

from typing import List, Optional, Sequence

# Defaults are starting points, NOT tuned values. Re-derive per instrument and
# per walk-forward window before any live use.
DEFAULT_ROC_PERIOD = 10
DEFAULT_Z_WINDOW = 100
DEFAULT_ENTRY_Z = 1.0


def _mean(values: Sequence[float]) -> float:
    """Arithmetic mean of a sequence."""
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    """Sample standard deviation (n-1 denominator), 0.0 for < 2 points."""
    n = len(values)
    if n < 2:
        return 0.0
    m = _mean(values)
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    return var ** 0.5


def rate_of_change(closes: Sequence[float], period: int = DEFAULT_ROC_PERIOD) -> List[Optional[float]]:
    """Percentage Rate-of-Change for each bar.

    The first ``period`` bars have no lookback and return ``None`` (not 0.0 -
    a fabricated zero would look like "no movement" and corrupt the z-score).
    A zero or negative previous close yields ``None``.
    """
    if period <= 0:
        raise ValueError(f"period must be > 0, got {period}")
    out: List[Optional[float]] = []
    for i, price in enumerate(closes):
        if i < period:
            out.append(None)
            continue
        prev = closes[i - period]
        if prev is None or prev <= 0 or price is None:
            out.append(None)
            continue
        out.append((price - prev) / prev * 100.0)
    return out


def zscored_roc(
    closes: Sequence[float],
    period: int = DEFAULT_ROC_PERIOD,
    z_window: int = DEFAULT_Z_WINDOW,
) -> List[Optional[float]]:
    """Z-score each ROC value against its own trailing window.

    For bar ``i`` the z-score uses only ROC values from bars ``i - z_window .. i``
    (no future data). Returns ``None`` where there is not yet a full window, or
    where the window's standard deviation is zero.
    """
    if z_window <= 0:
        raise ValueError(f"z_window must be > 0, got {z_window}")

    rocs = rate_of_change(closes, period)
    out: List[Optional[float]] = []

    for i, roc in enumerate(rocs):
        if roc is None or i + 1 < z_window:
            out.append(None)
            continue
        window = [r for r in rocs[i + 1 - z_window: i + 1] if r is not None]
        if len(window) < z_window:       # holes in the window -> not ready
            out.append(None)
            continue
        sd = _stdev(window)
        if sd <= 0:
            out.append(None)
            continue
        out.append((roc - _mean(window)) / sd)
    return out


def find_flip(
    z_series: Sequence[Optional[float]],
    start_idx: int,
    *,
    entry_z: float = DEFAULT_ENTRY_Z,
    direction: str = "BUY",
) -> Optional[int]:
    """Return the first index >= start_idx where z flips in ``direction``.

    A flip is a crossing, not a level test:
      BUY  : z > +entry_z  and previous z <= +entry_z
      SELL : z < -entry_z  and previous z >= -entry_z
    """
    direction = (direction or "").upper()
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")
    if entry_z <= 0:
        raise ValueError(f"entry_z must be > 0, got {entry_z}")

    for i in range(max(1, start_idx), len(z_series)):
        cur, prev = z_series[i], z_series[i - 1]
        if cur is None or prev is None:
            continue
        if direction == "BUY":
            if cur > entry_z and prev <= entry_z:
                return i
        else:
            if cur < -entry_z and prev >= -entry_z:
                return i
    return None
