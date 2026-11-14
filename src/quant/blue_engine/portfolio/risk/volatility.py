"""
risk/volatility.py - volatility estimators and forecasters (pure).

The allocator needs *forward-looking* volatility, not the trailing realised
number. This module provides the ladder a quant climbs:

    realised vol  ->  EWMA vol  ->  GARCH(1,1)  ->  HAR-RV multi-horizon

All are dependency-free except GARCH (uses ``arch`` if installed; otherwise a
hand-rolled MLE-free method-of-moments GARCH(1,1) is used as the fallback).

Estimator reference
-------------------
* ``realised_vol``        close-to-close, annualised std of log returns.
* ``parkinson_vol``       Parkinson high-low estimator (5x more efficient than
                          close-to-close; uses the intraday range).
* ``garman_klass_vol``    Garman-Klass (uses OHLC; the most efficient of the
                          three when open/high/low/close are all available).
* ``ewma_vol``            RiskMetrics EWMA (lambda 0.94 daily).
* ``garch_vol``           GARCH(1,1) one-step-ahead forecast.
* ``har_vol``             HAR-RV style blend of daily/weekly/monthly RV.
* ``forecast``            one-call dispatcher returning the best available.

Vol is returned as an ANNUALISED number (e.g. 0.14 for 14%) unless
``annualise=False``.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence

from ...core.results import mean, safe_float, stdev

logger = logging.getLogger(__name__)

__all__ = [
    "realised_vol", "parkinson_vol", "garman_klass_vol", "ewma_vol",
    "garch_vol", "har_vol", "forecast",
]


def _log_returns(prices: Sequence[float]) -> List[float]:
    clean = [safe_float(p) for p in prices]
    out: List[float] = []
    for a, b in zip(clean, clean[1:]):
        if a is None or b is None or a <= 0 or b <= 0:
            continue
        out.append(math.log(b / a))
    return out


def realised_vol(prices: Sequence[float], *, periods: float = 252.0,
                 annualise: bool = True) -> Optional[float]:
    """Annualised close-to-close volatility of a price series."""
    rs = _log_returns(prices)
    sd = stdev(rs)
    if sd is None:
        return None
    return sd * math.sqrt(periods) if annualise else sd


def parkinson_vol(highs: Sequence[float], lows: Sequence[float], *,
                  periods: float = 252.0, annualise: bool = True
                  ) -> Optional[float]:
    """Parkinson (high-low) volatility estimator.

    Uses only the daily high/low range: ``sigma^2 = mean(ln(H/L)^2) / (4 ln2)``.
    ~5x more efficient than close-to-close, and robust to overnight jumps.
    """
    n = min(len(highs or []), len(lows or []))
    terms: List[float] = []
    for i in range(n):
        h = safe_float(highs[i])
        l = safe_float(lows[i])
        if h is None or l is None or h <= 0 or l <= 0 or h < l:
            continue
        terms.append(math.log(h / l) ** 2)
    if len(terms) < 2:
        return None
    var = mean(terms) / (4.0 * math.log(2.0))
    if var is None or var < 0:
        return None
    vol = math.sqrt(var)
    return vol * math.sqrt(periods) if annualise else vol


def garman_klass_vol(opens: Sequence[float], highs: Sequence[float],
                     lows: Sequence[float], closes: Sequence[float], *,
                     periods: float = 252.0, annualise: bool = True
                     ) -> Optional[float]:
    """Garman-Klass (OHLC) volatility estimator.

    ``sigma^2 = mean( 0.5*ln(H/L)^2 - (2ln2-1)*ln(C/O)^2 )``. The most efficient
    of the range-based estimators when the full OHLC bar is available.
    """
    n = min(len(opens or []), len(highs or []), len(lows or []), len(closes or []))
    terms: List[float] = []
    for i in range(n):
        o = safe_float(opens[i]); h = safe_float(highs[i])
        l = safe_float(lows[i]); c = safe_float(closes[i])
        if None in (o, h, l, c) or min(o, h, l, c) <= 0 or h < l:
            continue
        hl = math.log(h / l) ** 2
        co = math.log(c / o) ** 2
        terms.append(0.5 * hl - (2.0 * math.log(2.0) - 1.0) * co)
    if len(terms) < 2:
        return None
    var = mean(terms)
    if var is None or var < 0:
        var = max(var or 0.0, 0.0)
    vol = math.sqrt(var)
    return vol * math.sqrt(periods) if annualise else vol


def ewma_vol(returns: Sequence[float], *, lam: float = 0.94,
             periods: float = 252.0, annualise: bool = True) -> Optional[float]:
    """RiskMetrics EWMA volatility of a returns series."""
    rs = [safe_float(r) for r in (returns or [])]
    rs = [r for r in rs if r is not None]
    if len(rs) < 2:
        return None
    lam = min(max(float(lam), 0.0), 0.9999)
    mu = mean(rs) or 0.0
    n = len(rs)
    weights = [lam ** (n - 1 - t) for t in range(n)]
    wsum = sum(weights) or 1.0
    var = sum(w * (rs[t] - mu) ** 2 for t, w in enumerate(weights)) / wsum
    vol = math.sqrt(max(var, 0.0))
    return vol * math.sqrt(periods) if annualise else vol


def garch_vol(returns: Sequence[float], *, periods: float = 252.0,
              annualise: bool = True) -> Optional[float]:
    """GARCH(1,1) one-step-ahead volatility forecast.

    Prefers the ``arch`` package's MLE fit when installed. Falls back to a
    method-of-moments GARCH(1,1) (estimate omega/alpha/beta from the sample
    variance and autocorrelation of squared returns), which is accurate enough
    for a sanity-check overlay and needs no dependency.
    """
    rs = [safe_float(r) for r in (returns or [])]
    rs = [r for r in rs if r is not None]
    if len(rs) < 20:
        return ewma_vol(rs, periods=periods, annualise=annualise)

    # Preferred: arch's MLE.
    try:
        from arch import arch_model  # type: ignore

        am = arch_model([r * 100.0 for r in rs], vol="Garch", p=1, q=1,
                        dist="normal", rescale=False)
        res = am.fit(disp="off", show_warning=False)
        fcast = res.forecast(horizon=1, reindex=False)
        var_pct = float(fcast.variance.values[-1, 0])   # in (%)^2
        vol = math.sqrt(var_pct) / 100.0
        return vol * math.sqrt(periods) if annualise else vol
    except Exception:  # noqa: BLE001 - fall through to MoM
        pass

    # Fallback: method-of-moments GARCH(1,1) on centred returns.
    mu = mean(rs) or 0.0
    eps = [r - mu for r in rs]
    var = (stdev(eps) or 0.0) ** 2
    if var <= 0:
        return None
    # alpha+beta from the lag-1 autocorrelation of squared residuals (clipped).
    sq = [e * e for e in eps]
    m_sq = mean(sq) or 0.0
    if m_sq <= 0:
        return None
    ac1 = _acf1(sq, m_sq)
    alpha = min(max(ac1 * 0.5, 0.01), 0.30)
    beta = min(max(0.90 - alpha, 0.50), 0.98)
    omega = var * (1.0 - alpha - beta)
    # One-step forecast from the last residual.
    sigma2_last = var
    sigma2_next = omega + alpha * (eps[-1] ** 2) + beta * sigma2_last
    vol = math.sqrt(max(sigma2_next, 0.0))
    return vol * math.sqrt(periods) if annualise else vol


def _acf1(x: Sequence[float], mu: float) -> float:
    n = len(x)
    if n < 3 or mu <= 0:
        return 0.0
    num = sum((x[t] - mu) * (x[t - 1] - mu) for t in range(1, n)) / (n - 1)
    return num / mu


def har_vol(returns: Sequence[float], *, periods: float = 252.0,
            annualise: bool = True) -> Optional[float]:
    """HAR-RV style volatility: a weighted blend of daily/weekly/monthly RV.

    The Heterogeneous AutoRegressive (HAR) model says realised vol at multiple
    horizons (1d, 1w, 1m) forecasts the next period. We compute a simple
    weights-summed blend of the three realised vols (a linear HAR with fixed,
    positive weights), which is more stable than the 1-day number alone.
    """
    rs = [safe_float(r) for r in (returns or [])]
    rs = [r for r in rs if r is not None]
    if len(rs) < 10:
        return realised_vol([math.exp(x) for x in rs], periods=periods,
                            annualise=annualise)
    d = stdev(rs[-22:]) or 0.0          # ~1 month
    w = stdev(rs[-5:]) or 0.0           # ~1 week
    m = stdev(rs) or 0.0                # full window (proxy for the long leg)
    blended = 0.5 * w + 0.3 * d + 0.2 * m
    return blended * math.sqrt(periods) if annualise else blended


def forecast(ohlc: Dict[str, Any], *, method: str = "auto",
             periods: float = 252.0, annualise: bool = True) -> Dict[str, Any]:
    """One-call volatility forecast from a bar dict.

    ``ohlc`` may contain any of: ``close``/``prices`` (a series), ``returns``
    (a series), ``high``, ``low``, ``open``. The dispatcher uses the best
    estimator the available data supports, unless ``method`` names one
    explicitly (``realised`` | ``parkinson`` | ``garman_klass`` | ``ewma`` |
    ``garch`` | ``har``).

    Returns ``{"ok": bool, "vol": float|None, "method": str, "warnings": [...]}``.
    """
    closes = ohlc.get("close") or ohlc.get("prices") or []
    rets = ohlc.get("returns") or []
    highs = ohlc.get("high") or []
    lows = ohlc.get("low") or []
    opens = ohlc.get("open") or []

    def _call(fn, *args):
        try:
            return fn(*args, periods=periods, annualise=annualise)
        except Exception:  # noqa: BLE001
            return None

    candidates: List[tuple] = []
    if rets:
        candidates += [("har", lambda: _call(har_vol, rets)),
                       ("garch", lambda: _call(garch_vol, rets)),
                       ("ewma", lambda: _call(ewma_vol, rets))]
    if closes and len(closes) >= 3:
        candidates += [("realised", lambda: _call(realised_vol, closes))]
    if highs and lows:
        candidates += [("parkinson", lambda: _call(parkinson_vol, highs, lows))]
    if opens and highs and lows and closes:
        candidates += [("garman_klass",
                        lambda: _call(garman_klass_vol, opens, highs, lows, closes))]

    if method != "auto":
        named = [c for c in candidates if c[0] == method]
        if named:
            v = named[0][1]()
            if v is not None:
                return {"ok": True, "vol": round(v, 6), "method": method,
                        "warnings": []}
        warnings = [f"volatility method '{method}' unavailable; trying others"]
    else:
        warnings = []

    for name, fn in candidates:
        v = fn()
        if v is not None:
            return {"ok": True, "vol": round(v, 6), "method": name,
                    "warnings": warnings}
    return {"ok": False, "vol": None, "method": method,
            "warnings": warnings + ["no volatility estimator produced a value"]}