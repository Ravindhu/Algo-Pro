"""
features/stationarity.py - ADF / KPSS gates and VAR-lag Granger causality.

Macro levels are non-stationary; regressing a non-stationary series on another
produces spurious correlations. Before the engine (or a fitted model) trusts a
macro series as a regression feature, this module *gates* it:

  * **ADF** (Augmented Dickey-Fuller) - H0: unit root (non-stationary).
  * **KPSS** - H0: stationary.
  * **Granger causality** - does signal X help predict NIFTY returns?

Both tests need ``statsmodels``; when it is absent the functions return a
``method='unavailable'`` result rather than raising, honouring the
degrade-never-halt contract. Pure otherwise: no I/O.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ...core.results import safe_float

# ---------------------------------------------------------------------------
# LAZY OPTIONAL DEPENDENCY (perf fix 2026-10-07)
# ---------------------------------------------------------------------------
# ``statsmodels`` pulls in ``scipy.stats``, which costs ~100 s to import on a
# cold Windows interpreter. Importing it eagerly here meant that
# ``core/registry.discover_all()`` -- which walks every module under
# ``blue_engine.economics`` to auto-register engine classes -- paid that cost
# on EVERY dashboard/terminal start, even though none of these ADF/KPSS/Granger
# gates are used during boot.
#
# The dependency is now resolved on first *call* instead of on import. Public
# behaviour is unchanged: ``available()`` still reports whether statsmodels can
# be imported, and each test function still degrades to
# ``method='unavailable'`` when it cannot.
_STATSMODELS_CACHE: Dict[str, Any] = {}


def _load_statsmodels() -> bool:
    """Import statsmodels on demand; cache the result.

    Returns True when the optional dependency is importable. Never raises --
    the degrade-never-halt contract of this module is preserved.
    """
    if "ok" in _STATSMODELS_CACHE:
        return bool(_STATSMODELS_CACHE["ok"])
    try:  # optional dependency
        from statsmodels.tsa.stattools import (
            adfuller as _adfuller,
            grangercausalitytests as _grangercausalitytests,
            kpss as _kpss,
        )

        _STATSMODELS_CACHE.update(
            ok=True,
            adfuller=_adfuller,
            kpss=_kpss,
            grangercausalitytests=_grangercausalitytests,
        )
    except Exception:  # noqa: BLE001
        _STATSMODELS_CACHE.update(ok=False)
    return bool(_STATSMODELS_CACHE["ok"])


def _has_statsmodels() -> bool:
    """Backwards-compatible probe for the private flag callers used to read."""
    return _load_statsmodels()


def _clean(series: Sequence[float]) -> List[float]:
    return [float(v) for v in series if v is not None]


def adf(series: Sequence[float]) -> Dict[str, Any]:
    """Augmented Dickey-Fuller test. Returns p-value + stationarity verdict."""
    if not _load_statsmodels():
        return {"method": "unavailable", "stationary": None, "p_value": None}
    data = _clean(series)
    if len(data) < 8:
        return {"method": "insufficient_data", "stationary": None,
                "p_value": None, "n": len(data)}
    try:
        stat, pval, _lags, _nobs, _crit, _icbest = _STATSMODELS_CACHE["adfuller"](
            data, autolag="AIC")
    except Exception as exc:  # noqa: BLE001
        return {"method": "error", "error": repr(exc),
                "stationary": None, "p_value": None}
    return {"method": "adf", "statistic": round(float(stat), 6),
            "p_value": round(float(pval), 6),
            "stationary": bool(pval < 0.05), "n": len(data)}


def kpss_test(series: Sequence[float]) -> Dict[str, Any]:
    """KPSS test (H0 = stationary). Complements ADF when they disagree."""
    if not _load_statsmodels():
        return {"method": "unavailable", "stationary": None, "p_value": None}
    data = _clean(series)
    if len(data) < 8:
        return {"method": "insufficient_data", "stationary": None,
                "p_value": None, "n": len(data)}
    try:
        stat, pval, _lags, _crit = _STATSMODELS_CACHE["kpss"](
            data, regression="c", nlags="auto")
    except Exception as exc:  # noqa: BLE001
        return {"method": "error", "error": repr(exc),
                "stationary": None, "p_value": None}
    # KPSS: a HIGH p-value fails to reject stationarity.
    return {"method": "kpss", "statistic": round(float(stat), 6),
            "p_value": round(float(pval), 6),
            "stationary": bool(pval > 0.05), "n": len(data)}


def is_stationary(series: Sequence[float]) -> Dict[str, Any]:
    """Combine ADF + KPSS. Agreeing tests -> a confident verdict.

    * Both say stationary    -> ``stationary=True``, ``confident=True``
    * Both say non-stationary -> ``stationary=False``, ``confident=True``
    * Disagree / unavailable  -> ``stationary=None``, ``confident=False``
      (conservative: treat the feature as suspect)
    """
    a = adf(series)
    k = kpss_test(series)
    if a["stationary"] is None or k["stationary"] is None:
        return {"stationary": None, "confident": False, "adf": a, "kpss": k}
    agree = a["stationary"] == k["stationary"]
    return {"stationary": a["stationary"] if agree else None,
            "confident": agree, "adf": a, "kpss": k}


def granger(cause: Sequence[float], effect: Sequence[float],
            *, max_lag: int = 5) -> Dict[str, Any]:
    """Granger-causality test: does ``cause`` lead ``effect``?

    Both series must be equal length and stationary (run :func:`is_stationary`
    first). Returns the smallest p-value across lags and the chosen lag.
    """
    if not _load_statsmodels():
        return {"method": "unavailable", "p_value": None, "leads": None}
    c = _clean(cause)
    e = _clean(effect)
    n = min(len(c), len(e))
    if n < max_lag * 3 + 4:
        return {"method": "insufficient_data", "p_value": None, "leads": None}
    data = [[e[i], c[i]] for i in range(n)]
    try:
        res = _STATSMODELS_CACHE["grangercausalitytests"](
            data, maxlag=max_lag, verbose=False)
    except Exception as exc:  # noqa: BLE001
        return {"method": "error", "error": repr(exc), "p_value": None,
                "leads": None}
    best_lag, best_p = None, 1.0
    for lag, block in res.items():
        try:
            p = float(block[0]["ssr_ftest"][1])
        except Exception:  # noqa: BLE001
            continue
        if p < best_p:
            best_p, best_lag = p, int(lag)
    return {"method": "granger", "p_value": round(best_p, 6),
            "lag": best_lag, "leads": bool(best_p < 0.05)}


def available() -> bool:
    """True when statsmodels is importable (so tests can skip cleanly).

    NOTE: this triggers the (slow) statsmodels import on first call. Callers
    that only want the import-time cost avoided should not call it during boot.
    """
    return _load_statsmodels()


__all__ = ["adf", "kpss_test", "is_stationary", "granger", "available"]