"""
roc.py - z-scored Rate-of-Change momentum confirmation.

================================================================================
WHY THIS EXISTS
================================================================================
The OCL keylevel signal fires the moment price touches a level. A touch alone
is a weak event: price touches levels constantly and most touches do nothing.
This module supplies the *confirmation* half of the confluence stack - a
momentum flip measured in units of its own recent volatility.

Raw ROC is:
    ROC(N) = (price_now - price_N_bars_ago) / price_N_bars_ago * 100

Raw ROC has two problems for confirmation use:
  1. A single gap bar can spike it (it uses only two endpoints).
  2. It is not comparable across volatility regimes: +1% is a big move in a
     quiet market and noise in a wild one.

Dividing by the rolling standard deviation of ROC fixes both: the result is
how many "typical moves" the current move represents. That is the z-score.

================================================================================
USAGE
================================================================================
    from blue_engine.alpha.confirm.roc import zscored_roc, find_flip

    z = zscored_roc(closes, roc_period=10, z_window=100)
    idx = find_flip(z, 0, entry_z=1.0, direction="BUY")   # first bullish flip

Both functions are pure (no SDK, no I/O, no global state) so they can be
unit-tested with plain lists and reused by the backtest harness.

================================================================================
"""
from __future__ import annotations

from typing import List, Optional, Sequence

# Defaults are starting points, NOT tuned values. They must be re-derived per
# instrument and per walk-forward window before any live use.
DEFAULT_ROC_PERIOD = 10
DEFAULT_Z_WINDOW = 100
DEFAULT_ENTRY_Z = 1.0


def _mean(values: Sequence[float]) -> float:
    """Population-free arithmetic mean of a sequence."""
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

    Returns a list the same length as ``closes``. The first ``period`` bars
    have no lookback available and are returned as ``None`` (not 0.0 - a
    fabricated zero would look like "no movement", which is a real signal
    and would corrupt the z-score statistics downstream).

    A zero or negative previous close yields ``None`` rather than a division
    error, so partially-loaded or corrupt series degrade gracefully.
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

    For bar ``i`` the z-score uses only ROC values from bars
    ``i - z_window .. i`` (inclusive of the current bar) - no future data.
    This is the same no-look-ahead discipline the backtest harness enforces
    with ``shift(1)``; a z-score computed from future bars would be a
    subtle, invisible leak that inflates every backtest.

    Returns ``None`` where there is not yet a full window, or where the
    window's standard deviation is zero (a flat series - undefined z).
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

    A flip is a *crossing*, not a level test:
      BUY  (bullish reversal)  : z > +entry_z  and previous z <= +entry_z
      SELL (bearish reversal)  : z < -entry_z  and previous z >= -entry_z

    Requiring the crossing (rather than merely "z is above the threshold")
    is what makes this a timing signal: it fires once at the event, not on
    every bar while the condition happens to hold.

    ``start_idx`` is normally the bar where the keylevel was touched, so the
    flip must occur at or after the touch - never before it.
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