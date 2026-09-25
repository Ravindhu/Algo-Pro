"""
results.py - small, dependency-free numeric/statistical helpers shared by the
macro engines and the dashboard.

Everything here is pure: no pandas, no numpy, no network. That keeps the core
engines importable in a bare ``openalgo==2.0.3`` environment where the optional
dashboard extras are not installed.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Parse ``value`` to a finite float, else ``default``."""
    if value is None or value == "":
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def pct_change(new: Any, old: Any) -> Optional[float]:
    """Percent change from ``old`` to ``new`` (None-safe, zero-safe)."""
    n = safe_float(new)
    o = safe_float(old)
    if n is None or o is None or o == 0:
        return None
    return (n - o) / abs(o) * 100.0


def annualise_return(total_return_pct: float, days: float,
                     periods_per_year: float = 252.0) -> Optional[float]:
    """Annualise a simple return given the horizon in days.

    Uses (1 + r) ** (periods_per_year / days) - 1. Returns None for a
    non-positive horizon. ``periods_per_year`` is days-based by default.
    """
    if days is None or days <= 0:
        return None
    base = 1.0 + (total_return_pct / 100.0)
    if base <= 0:
        return None
    return (base ** (periods_per_year / days) - 1.0) * 100.0


def mean(values: Sequence[float]) -> Optional[float]:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def stdev(values: Sequence[float], *, sample: bool = True) -> Optional[float]:
    clean = [float(v) for v in values if v is not None]
    n = len(clean)
    if n < 2:
        return None
    mu = sum(clean) / n
    var = sum((v - mu) ** 2 for v in clean) / (n - 1 if sample else n)
    return math.sqrt(var)


def zscore(value: Optional[float], mean_value: Optional[float],
           sd_value: Optional[float]) -> Optional[float]:
    if value is None or mean_value is None or sd_value is None or sd_value == 0:
        return None
    return (value - mean_value) / sd_value


def correlation(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Pearson correlation of two equal-length series (None on degenerate in)."""
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys)
             if x is not None and y is not None]
    n = len(pairs)
    if n < 2:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    cov = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    vx = sum((p[0] - mx) ** 2 for p in pairs)
    vy = sum((p[1] - my) ** 2 for p in pairs)
    denom = math.sqrt(vx * vy)
    if denom == 0:
        return None
    return cov / denom


def portfolio_variance(weights: Sequence[float],
                       vols: Sequence[float],
                       corr: Sequence[Sequence[float]]) -> Optional[float]:
    """Portfolio variance from weights, per-asset vols and a corr matrix.

    var = sum_i sum_j w_i w_j sigma_i sigma_j rho_ij.
    Pure double loop - the universe here is small (indices/sectors), so an
    O(n^2) evaluation is faster than importing numpy.
    """
    n = len(weights)
    if n == 0 or len(vols) != n or len(corr) != n:
        return None
    var = 0.0
    for i in range(n):
        for j in range(n):
            rho = corr[i][j] if i < len(corr[i]) else 0.0
            var += weights[i] * weights[j] * vols[i] * vols[j] * rho
    return var


def portfolio_vol(weights: Sequence[float],
                  vols: Sequence[float],
                  corr: Sequence[Sequence[float]]) -> Optional[float]:
    var = portfolio_variance(weights, vols, corr)
    if var is None or var < 0:
        return None
    return math.sqrt(var)


def risk_parity_weights(vols: Sequence[float]) -> Optional[List[float]]:
    """Inverse-volatility (naive risk-parity) weights, normalised to 1.

    Each asset gets weight proportional to 1/sigma_i. This is the simplest
    defensible risk-balance rule and a good default when a full covariance
    estimate is unavailable.
    """
    clean = [safe_float(v) for v in vols]
    if not clean or any(v is None or v <= 0 for v in clean):
        return None
    inv = [1.0 / v for v in clean]
    total = sum(inv)
    if total <= 0:
        return None
    return [w / total for w in inv]


def clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


def score_to_signal(score: Optional[float], *, buy: float = 0.5,
                    sell: float = -0.5) -> str:
    """Map a signed score to BUY / SELL / HOLD."""
    if score is None:
        return "HOLD"
    if score >= buy:
        return "BUY"
    if score <= sell:
        return "SELL"
    return "HOLD"


__all__ = [
    "safe_float", "pct_change", "annualise_return", "mean", "stdev",
    "zscore", "correlation", "portfolio_variance", "portfolio_vol",
    "risk_parity_weights", "clamp", "score_to_signal",
]