"""
risk/tail.py - tail-risk measures (pure): VaR, CVaR, drawdown, stress.

Volatility alone does not describe the risk that ends careers — the *tail*
does. This module computes, for a returned P&L series or for a portfolio
defined by (weights, covariance):

* **Historical VaR**   — the empirical quantile of the loss distribution.
* **Parametric VaR**   — Gaussian, plus a Cornish-Fisher correction for the
                         skew/kurtosis that Indian equity returns always show.
* **CVaR (ES)**        — the mean loss *given* we are in the tail; the coherent,
                         optimiser-friendly risk measure.
* **Drawdown-at-risk** — peak-to-trough distribution (max drawdown, duration).
* **Stress scenarios** — replay named historical shocks on the current book.

All functions are dependency-free. Losses are returned as POSITIVE numbers
(e.g. ``var = 0.03`` means "we expect to lose 3% or less on 95% of days").
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence

from ...core.results import mean, safe_float, stdev

logger = logging.getLogger(__name__)

__all__ = [
    "historical_var", "parametric_var", "cornish_fisher_var", "cvar",
    "drawdown_series", "max_drawdown", "drawdown_at_risk",
    "portfolio_var", "stress_test", "DEFAULT_STRESS",
]

# Named historical stress scenarios as (market shock, description). The shock is
# applied to the portfolio's beta so it scales the whole book (India-relevant):
#   GFC 2008, taper 2013, demonetisation 2016, COVID Mar-2020, a 2025-style spike.
DEFAULT_STRESS: Dict[str, Dict[str, Any]] = {
    "GFC_2008":        {"market_shock": -0.55, "vol_mult": 3.0,
                        "label": "Global Financial Crisis (NIFTY ~-55%)"},
    "TAPER_2013":      {"market_shock": -0.20, "vol_mult": 1.8,
                        "label": "2013 taper tantrum / INR crisis"},
    "DEMONET_2016":    {"market_shock": -0.10, "vol_mult": 1.5,
                        "label": "2016 demonetisation"},
    "COVID_2020":      {"market_shock": -0.38, "vol_mult": 4.0,
                        "label": "COVID crash Mar-2020"},
    "VOL_SPIKE_2025":  {"market_shock": -0.15, "vol_mult": 2.5,
                        "label": "2025-style intraday VIX spike"},
}


# --------------------------------------------------------------------------- #
# Quantile helpers
# --------------------------------------------------------------------------- #

def _quantile(sorted_xs: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolated empirical quantile of a sorted sequence."""
    n = len(sorted_xs)
    if n == 0:
        return None
    if n == 1:
        return sorted_xs[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_xs[lo]
    frac = pos - lo
    return sorted_xs[lo] * (1 - frac) + sorted_xs[hi] * frac


def _skew_kurt(xs: Sequence[float]) -> tuple:
    n = len(xs)
    if n < 2:
        return 0.0, 3.0
    mu = mean(xs) or 0.0
    sd = stdev(xs) or 0.0
    if sd <= 0:
        return 0.0, 3.0
    m3 = sum((x - mu) ** 3 for x in xs) / n
    m4 = sum((x - mu) ** 4 for x in xs) / n
    skew = m3 / (sd ** 3)
    kurt = m4 / (sd ** 4)
    return skew, kurt


# --------------------------------------------------------------------------- #
# VaR / CVaR
# --------------------------------------------------------------------------- #

def historical_var(returns: Sequence[float], *, level: float = 0.95
                   ) -> Optional[float]:
    """Historical VaR: the loss at the ``level`` quantile (positive number).

    ``returns`` is a P&L (or return) series; the loss is ``-quantile`` of the
    left tail. A ``level`` of 0.95 means "the 5th-percentile loss".
    """
    rs = sorted(r for r in (safe_float(x) for x in (returns or []))
                if r is not None)
    if len(rs) < 2:
        return None
    q = _quantile(rs, 1.0 - level)
    return None if q is None else max(-q, 0.0)


def cvar(returns: Sequence[float], *, level: float = 0.95) -> Optional[float]:
    """Conditional VaR / Expected Shortfall: mean loss beyond the VaR point.

    The coherent risk measure — the tail *average*, not just the tail *point* —
    which is what a mean-CVaR optimiser minimises.
    """
    rs = sorted(r for r in (safe_float(x) for x in (returns or []))
                if r is not None)
    if len(rs) < 2:
        return None
    cut = _quantile(rs, 1.0 - level)
    if cut is None:
        return None
    tail = [r for r in rs if r <= cut]
    if not tail:
        return max(-cut, 0.0)
    m = mean(tail)
    return None if m is None else max(-m, 0.0)


def parametric_var(returns: Sequence[float], *, level: float = 0.95
                   ) -> Optional[float]:
    """Gaussian parametric VaR from the sample mean/std (positive = a loss).

    Sign convention (this is where it is easy to go wrong): ``_z_score`` returns
    the LEFT-tail quantile, so it is NEGATIVE (-1.645 at 95%). The loss quantile
    is therefore ``mu + z*sd`` (a number below the mean), and VaR is its
    negation: ``-(mu + z*sd)``. Writing ``-(mu - z*sd)`` flips the sign and the
    ``max(..., 0)`` clip then silently reports 0.0000 for every input — a risk
    report that says "no risk" is worse than no report.
    """
    rs = [safe_float(x) for x in (returns or [])]
    rs = [r for r in rs if r is not None]
    sd = stdev(rs)
    mu = mean(rs)
    if sd is None or mu is None or sd <= 0:
        return None
    z = _z_score(level)          # negative, e.g. -1.645 at 95%
    return max(-(mu + z * sd), 0.0)


def cornish_fisher_var(returns: Sequence[float], *, level: float = 0.95
                       ) -> Optional[float]:
    """Cornish-Fisher VaR: parametric VaR corrected for skew & fat tails.

    ``z_cf = z + (z^2-1)/6 * skew + (z^3-3z)/24 * exkurt -
    (2z^3-5z)/36 * skew^2``. This is why a plain Gaussian under-states the risk
    of Indian small-cap sleeves, which are sharply negatively skewed.

    Same sign convention as :func:`parametric_var`: ``z`` is the negative
    left-tail quantile, so the loss is ``-(mu + z_cf * sd)``.
    """
    rs = [safe_float(x) for x in (returns or [])]
    rs = [r for r in rs if r is not None]
    sd = stdev(rs)
    mu = mean(rs)
    if sd is None or mu is None or sd <= 0:
        return None
    skew, kurt = _skew_kurt(rs)
    z = _z_score(level)          # negative, e.g. -1.645 at 95%
    exkurt = kurt - 3.0
    z_cf = (z + (z * z - 1) / 6.0 * skew
            + (z ** 3 - 3 * z) / 24.0 * exkurt
            - (2 * z ** 3 - 5 * z) / 36.0 * skew * skew)
    return max(-(mu + z_cf * sd), 0.0)


def _z_score(level: float) -> float:
    """Inverse standard-normal CDF via the Acklam rational approximation.

    Dependency-free (no scipy): accurate to ~1e-9 over [1e-6, 1-1e-6], which is
    far beyond what VaR reporting needs.
    """
    p = min(max(1.0 - level, 1e-6), 1 - 1e-6)
    # Acklam coefficients.
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) \
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) \
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q \
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


# --------------------------------------------------------------------------- #
# Drawdown
# --------------------------------------------------------------------------- #

def drawdown_series(equity: Sequence[float]) -> List[float]:
    """Running drawdown (negative fractions) from an equity curve."""
    out: List[float] = []
    peak = None
    for e in equity or []:
        v = safe_float(e)
        if v is None:
            continue
        peak = v if peak is None else max(peak, v)
        out.append((v / peak - 1.0) if peak and peak != 0 else 0.0)
    return out


def max_drawdown(equity: Sequence[float]) -> Optional[float]:
    """Maximum peak-to-trough drawdown as a POSITIVE fraction (e.g. 0.22)."""
    dd = drawdown_series(equity)
    if not dd:
        return None
    return abs(min(dd))


def drawdown_at_risk(equity: Sequence[float], *, level: float = 0.95
                     ) -> Optional[float]:
    """Drawdown-at-risk: the ``level`` quantile of the drawdown distribution.

    Answers "on a bad stretch, how deep can the equity curve get?" rather than
    "on a bad day". Useful for the drawdown governor (sizing/drawdown_governor).
    """
    dd = sorted(drawdown_series(equity))
    if len(dd) < 2:
        return None
    q = _quantile(dd, 1.0 - level)
    return None if q is None else abs(q)


# --------------------------------------------------------------------------- #
# Portfolio-level VaR / CVaR from weights + covariance (no return paths)
# --------------------------------------------------------------------------- #

def portfolio_var(weights: Sequence[float], vols: Sequence[float],
                  corr: Sequence[Sequence[float]], *, level: float = 0.95,
                  horizon_days: float = 1.0) -> Dict[str, Any]:
    """Parametric portfolio VaR/CVaR from weights, vols and correlation.

    ``vol`` here is assumed ANNUALISED; the horizon scaling is
    ``sqrt(horizon_days/252)``. Returns a dict with ``var``, ``cvar``,
    ``vol`` (horizon-scaled) and ``z``. Gaussian CVaR via the closed form
    ``sigma * phi(z)/(1-level)``.
    """
    w = [safe_float(x) or 0.0 for x in (weights or [])]
    n = len(w)
    if n == 0:
        return {"var": None, "cvar": None, "vol": None, "z": None}
    v = [safe_float(vols[i]) if i < len(vols) else None for i in range(n)]
    v = [(x if x is not None and x > 0 else 0.0) for x in v]
    var_sum = 0.0
    for i in range(n):
        for j in range(n):
            rho = 1.0 if i == j else (
                corr[i][j] if i < len(corr) and j < len(corr[i]) else 0.0)
            var_sum += w[i] * w[j] * v[i] * v[j] * rho
    ann_vol = math.sqrt(max(var_sum, 0.0))
    scale = math.sqrt(max(horizon_days, 0.0) / 252.0)
    h_vol = ann_vol * scale
    z = _z_score(level)
    var = max(z * h_vol, 0.0)
    cvar = h_vol * _norm_pdf(z) / max(1.0 - level, 1e-9)
    return {"var": round(var, 6), "cvar": round(cvar, 6),
            "vol": round(h_vol, 6), "z": round(z, 4),
            "ann_vol": round(ann_vol, 6)}


def _norm_pdf(z: float) -> float:
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


# --------------------------------------------------------------------------- #
# Stress testing
# --------------------------------------------------------------------------- #

def stress_test(beta: Optional[float], *, net_exposure: float = 1.0,
                scenarios: Optional[Dict[str, Dict[str, Any]]] = None
                ) -> List[Dict[str, Any]]:
    """Replay named historical shocks on the current book.

    A simple, transparent model: ``pnl = net_exposure * beta * market_shock``,
    and the stressed vol is ``book_vol * vol_mult``. Real desks layer in factor
    betas; this is the first-order (market-factor) version every PM starts with.
    """
    sc = scenarios or DEFAULT_STRESS
    b = beta if beta is not None else 1.0
    out: List[Dict[str, Any]] = []
    for name, s in sc.items():
        shock = safe_float(s.get("market_shock")) or 0.0
        pnl = net_exposure * b * shock
        out.append({
            "scenario": name,
            "label": s.get("label", name),
            "market_shock": shock,
            "vol_mult": safe_float(s.get("vol_mult")) or 1.0,
            "pnl_pct": round(pnl, 6),
            "pnl_pct_human": f"{pnl * 100:+.1f}%",
        })
    out.sort(key=lambda r: r["pnl_pct"])
    return out


def summarise(returns: Sequence[float], *, levels: Sequence[float] = (0.95, 0.99)
              ) -> Dict[str, Any]:
    """Bundle the standard tail diagnostics for one P&L series."""
    out: Dict[str, Any] = {}
    for lv in levels:
        tag = f"{int(lv * 100)}"
        out[f"var_hist_{tag}"] = _r(historical_var(returns, level=lv))
        out[f"var_param_{tag}"] = _r(parametric_var(returns, level=lv))
        out[f"var_cf_{tag}"] = _r(cornish_fisher_var(returns, level=lv))
        out[f"cvar_{tag}"] = _r(cvar(returns, level=lv))
    skew, kurt = _skew_kurt([safe_float(x) for x in (returns or [])
                             if safe_float(x) is not None])
    out["skew"] = round(skew, 4)
    out["excess_kurtosis"] = round(kurt - 3.0, 4)
    out["n"] = len([x for x in (returns or []) if safe_float(x) is not None])
    return out


def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 6)