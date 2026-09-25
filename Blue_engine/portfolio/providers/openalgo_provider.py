"""
providers/openalgo_provider.py - build the portfolio inputs from the SDK.

Pulls daily closes for each sleeve via ``client.history(...)``, computes returns,
and estimates the covariance/vol/correlation/beta block with the risk models.
This is the M1 deliverable from the plan: replace the hand-fed ``vols``/``corr``
with *live-estimated risk*, using only the public SDK (no extra credentials).

Flow
----
    client.history(symbol=..., exchange=..., interval="D", ...)  per sleeve
        -> close series -> daily returns
        -> risk.covariance.estimate  -> cov -> vols + corr
        -> risk.beta.ols_beta (vs benchmark)  -> betas
        -> provider dict (the exact shape portfolio_engine consumes)

Degrades gracefully: any sleeve whose history fails is dropped with a warning; if
no sleeve yields data, ``meta.ok`` is False and the engine falls back to whatever
the config supplied.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from .base import PortfolioProvider
from . import universe as universe_mod
from . import price_cache
from ..risk import beta as beta_mod
from ..risk import covariance as cov_mod

logger = logging.getLogger(__name__)

__all__ = ["OpenAlgoPortfolioProvider"]


def _close_series(client: Any, symbol: str, exchange: str, *,
                  lookback_days: int = 400) -> List[float]:
    """Daily closes for a symbol via the SDK (oldest first). [] on any failure.

    Thin wrapper over :func:`_history_rows` that keeps only the close column.
    Retained for backward compatibility; new code should use ``_history_rows``
    so the full OHLCV bar is available for the price cache and the
    range-based volatility estimators.
    """
    return [r["close"] for r in _history_rows(client, symbol, exchange,
                                              lookback_days=lookback_days)
            if r.get("close") is not None]


def _history_rows(client: Any, symbol: str, exchange: str, *,
                  lookback_days: int = 400) -> List[Dict[str, Any]]:
    """Raw daily OHLCV rows for a symbol via the SDK (oldest first).

    Returns rows normalised by ``price_cache.normalise_rows`` so both the close
    series and the price cache consume the same shape. [] on any failure.
    """
    end = datetime.now()
    # OpenAlgo's daily history endpoint returns OHLCV rows; ~400 calendar days
    # gives roughly 250+ trading rows for a stable covariance estimate.
    start = end - timedelta(days=lookback_days)
    try:
        resp = client.history(
            symbol=symbol, exchange=exchange, interval="D",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        )
    except Exception as exc:  # noqa: BLE001 - SDK call boundary
        logger.debug("history failed for %s/%s: %s", symbol, exchange, exc)
        return []
    if not isinstance(resp, dict) or resp.get("status") != "success":
        return []
    rows = resp.get("data") or []
    return price_cache.normalise_rows(rows)


def _returns_from_closes(closes: List[float]) -> List[float]:
    out: List[float] = []
    for a, b in zip(closes, closes[1:]):
        if a and b and a > 0:
            out.append(b / a - 1.0)
    return out


class OpenAlgoPortfolioProvider(PortfolioProvider):
    """Live-estimate the portfolio inputs from the OpenAlgo SDK."""

    name = "openalgo_portfolio"

    def __init__(self, client: Any, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None):
        super().__init__(config, log)
        self.client = client
        #: covariance method: sample | ewma | ledoit_wolf (default last).
        self.cov_method: str = str(self.config.get("cov_method", "ledoit_wolf"))
        self.lam: float = float(self.config.get("ewma_lambda", 0.94))
        self.lookback_days: int = int(self.config.get("lookback_days", 400))
        self.benchmark: str = str(self.config.get("benchmark", "NIFTY"))
        self.periods: float = float(self.config.get("periods_per_year", 252.0))
        #: Persist every fetched bar so a walk-forward dataset accumulates.
        self.cache_enabled: bool = bool(self.config.get("cache_prices", True))
        self._cache: Optional[price_cache.PriceCache] = None

    # ---- price cache ---------------------------------------------------- #
    def _cache_obj(self) -> Optional[price_cache.PriceCache]:
        """Lazily build the price cache (None when disabled/unavailable)."""
        if not self.cache_enabled:
            return None
        if self._cache is None:
            try:
                db = self.config.get("cache_db")
                self._cache = price_cache.PriceCache(db, log=self.log)
            except Exception as exc:  # noqa: BLE001
                self.log(f"price cache unavailable: {exc!r}")
                return None
        return self._cache

    def _fetch(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        warnings: List[str] = []
        if self.client is None:
            return {"meta": {"ok": False, "warnings": [
                "no SDK client supplied to OpenAlgoPortfolioProvider"]}}

        uni = universe_mod.resolve_universe(self.config)
        universe = self.config.get("universe_names") or list(uni.keys())

        closes: Dict[str, List[float]] = {}
        returns: Dict[str, List[float]] = {}
        cache = self._cache_obj()
        cached_total = 0
        for sleeve in universe:
            meta = uni.get(sleeve) or {}
            symbol = meta.get("symbol", sleeve)
            exchange = meta.get("exchange", "NSE_INDEX")
            rows = _history_rows(self.client, symbol, exchange,
                                 lookback_days=self.lookback_days)
            # Persist every bar so the dataset accumulates day over day. The
            # cache key is the *sleeve* name (not the broker symbol) so it lines
            # up with the universe the optimiser and backtest use.
            if cache is not None and rows:
                try:
                    cached_total += cache.upsert(sleeve, exchange, "D", rows)
                except Exception as exc:  # noqa: BLE001 - cache is best-effort
                    self.log(f"cache write failed for {sleeve}: {exc!r}")
            cs = [r["close"] for r in rows if r.get("close") is not None]
            # Fall back to the cache when the live fetch came up short, so a
            # brief broker outage still yields a usable risk estimate.
            if len(cs) < 20 and cache is not None:
                cached = cache.closes(sleeve, exchange, "D")
                if len(cached) >= 20:
                    cs = cached
                    warnings.append(
                        f"{sleeve}: live fetch short; used {len(cached)} "
                        f"cached rows")
            if len(cs) < 20:
                warnings.append(f"{sleeve}: insufficient history ({len(cs)} rows)")
                continue
            closes[sleeve] = cs
            returns[sleeve] = _returns_from_closes(cs)

        present = list(returns.keys())
        if len(present) < 2:
            warnings.append("fewer than 2 sleeves had usable history")
            return {"universe": present, "vols": {}, "corr": {}, "betas": {},
                    "current_weights": {}, "meta": {"ok": False,
                                                    "warnings": warnings}}

        # Align the returns panel to a common length (most-recent aligned).
        tmin = min(len(returns[s]) for s in present)
        panel = [returns[s][-tmin:] for s in present]

        est = cov_mod.estimate(panel, method=self.cov_method, lam=self.lam,
                               periods=self.periods)
        warnings.extend(est.get("warnings", []))
        cov = est.get("cov")
        if cov is None:
            warnings.append("covariance estimation failed; using inverse-vol only")
            vols_map, corr_map = _fallback_vol_corr(panel, present)
        else:
            vols_map, corr_map = cov_mod.cov_to_dicts(cov, present)

        # Betas vs the benchmark sleeve (aligned panel).
        betas: Dict[str, float] = {}
        if self.benchmark in present:
            b_idx = present.index(self.benchmark)
            bench = panel[b_idx]
            for i, s in enumerate(present):
                b = beta_mod.ols_beta(panel[i], bench)
                betas[s] = round(b, 4) if b is not None else 1.0
        else:
            warnings.append(f"benchmark '{self.benchmark}' not in universe; "
                            f"betas default to 1.0")
            betas = {s: 1.0 for s in present}

        data: Dict[str, Any] = {
            "universe": present,
            "vols": vols_map,
            "corr": corr_map,
            "betas": betas,
            "cov": cov,
            "returns": {s: panel[i] for i, s in enumerate(present)},
            "n_obs": tmin,
            "cov_method": est.get("method"),
            "shrinkage_delta": est.get("delta"),
            "meta": {"ok": bool(cov is not None), "warnings": warnings,
                     "stale": False, "source": self.name},
        }
        if cached_total:
            data["meta"]["cached_rows"] = cached_total
        self.log(f"universe={len(present)} n_obs={tmin} method={est.get('method')} "
                 f"delta={est.get('delta')} cached={cached_total}")
        return data


def _fallback_vol_corr(panel: List[List[float]], names: List[str]
                       ) -> tuple:
    """Inverse-vol-friendly vols + constant-correlation fallback."""
    from ...core.results import stdev

    vols = {}
    for i, s in enumerate(names):
        sd = stdev(panel[i])
        vols[s] = round((sd or 0.01) * (252.0 ** 0.5), 6)
    corr = {a: {b: (1.0 if a == b else 0.3) for b in names} for a in names}
    return vols, corr