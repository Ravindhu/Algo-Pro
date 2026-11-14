"""
market.py - the Market engine.

Consumes the Economic regime and produces the *tradable* market picture:

  * commodity_market_return : commodity complex performance (proxy for
                              inflation / cyclicals; feeds sector tilt).
  * index_return            : performance of every tracked index (NIFTY,
                              BANKNIFTY, sector indices, ...). Used to rank
                              breadth and to pick the strongest market to
                              express risk in.
  * fii_inflow              : Foreign Institutional Investor net cash /
                              derivatives inflow (in currency units). The
                              single most-watched institutional flow signal
                              in Indian equities.

Outputs
-------
  breadth_score      : fraction of indices up, mapped to [-1, 1]
  leaders / laggards : ranked index list
  risk_appetite      : composite of breadth + FII flow, in [-1, 1]
  tradable_universe  : indices whose trend is confirmable (up on both
                       short and medium lookbacks)

Data is supplied via the ``provider`` callable returning::

    {
      "commodities": {"gold": {"close": 72000, "prev_close": 71500}, ...},
      "indices":     {"NIFTY": {"close": ..., "prev_close": ..., "return_1m_pct": ...}, ...},
      "fii":         {"net_cash": -1234.5, "net_futures": 678.9}
    }

Pure: no network, no SDK, no DB.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional

from ..core.base import BaseEngine, EngineResult, EngineStage
from ..core.results import clamp, mean, pct_change, safe_float


class MarketEngine(BaseEngine):
    """Build the market-context picture: breadth, sector leadership, flows."""

    name = "market"
    stage = EngineStage.MARKET

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 **kwargs: Any):
        super().__init__(config, provider, kwargs.get("log"))
        #: Indices we rank. If empty, every supplied index is ranked.
        self.index_whitelist: List[str] = list(
            self.config.get("indices") or [])
        #: Weight of FII flow vs index breadth in the risk-appetite composite.
        self.fii_weight: float = float(self.config.get("fii_weight", 0.5))
        self.low_volatility_pct = float(
            self.config.get("low_volatility_pct", 15.0))
        self.high_volatility_pct = float(
            self.config.get("high_volatility_pct", 30.0))
        if (not math.isfinite(self.low_volatility_pct)
                or not math.isfinite(self.high_volatility_pct)
                or self.low_volatility_pct <= 0
                or self.high_volatility_pct <= self.low_volatility_pct):
            raise ValueError(
                "volatility thresholds must be finite, positive, and increasing")

    # ---- data ---------------------------------------------------------- #
    def _collect(self, context: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(context.get("market"), dict):
            return context["market"]
        if callable(self.provider):
            try:
                data = self.provider()
                if isinstance(data, dict):
                    return data
            except Exception as exc:  # noqa: BLE001
                self.log(f"provider failed ({exc!r})")
        if isinstance(self.config.get("market"), dict):
            return self.config["market"]
        return {}

    # ---- sub-computations --------------------------------------------- #
    def _index_returns(self, indices: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for name, row in (indices or {}).items():
            if self.index_whitelist and name not in self.index_whitelist:
                continue
            if not isinstance(row, dict):
                row = {"close": row}
            close = safe_float(row.get("close", row.get("ltp", row.get("last"))))
            prev = safe_float(row.get("prev_close", row.get("previous_close")))
            r1d = safe_float(row.get("return_1d_pct"))
            if r1d is None:
                r1d = pct_change(close, prev)
            r1m = safe_float(row.get("return_1m_pct"))
            r3m = safe_float(row.get("return_3m_pct"))
            trend_up = None
            if r1m is not None and r3m is not None:
                trend_up = (r1m > 0 and r3m > 0)
            out[name] = {
                "close": close, "return_1d_pct": r1d,
                "return_1m_pct": r1m, "return_3m_pct": r3m,
                "trend_confirmed": trend_up,
            }
        return out

    def _commodity_returns(self, commodities: Dict[str, Any]):
        out: Dict[str, Any] = {}
        for name, row in (commodities or {}).items():
            if not isinstance(row, dict):
                row = {"close": row}
            close = safe_float(row.get("close", row.get("ltp")))
            prev = safe_float(row.get("prev_close"))
            r = safe_float(row.get("return_1d_pct")) or pct_change(close, prev)
            out[name] = {"close": close, "return_1d_pct": r}
        return out

    def _fii_score(self, fii: Dict[str, Any],
                   scale: float) -> Optional[float]:
        if not isinstance(fii, dict):
            return None
        net_cash = safe_float(fii.get("net_cash"))
        net_fut = safe_float(fii.get("net_futures"))
        parts = [v for v in (net_cash, net_fut) if v is not None]
        if not parts:
            return None
        # Normalise with the configured scale: |flow| == scale is a full-strength
        # signal (+/-1).
        #
        # AUDIT (2026-10-22): the default used to be 1000 Cr. Measured on 170 real
        # days of vendor FII cash flow, |net| >= 1000 Cr on 82% of days, so the
        # score saturated almost every day and a -1,305 Cr day was numerically
        # identical to a -5,000 Cr day. The score was effectively a binary
        # "FII bought / FII sold" flag, not a magnitude signal.
        #
        # At 6000 Cr (Q95 of the absolute daily distribution) saturation drops to
        # 5% of days and the score stays continuous across normal flow.
        #
        # AUDIT (2026-10-22): ``scale`` arrives from JSON config, so it can be a
        # string ("6000") or garbage ("abc"). The previous ``scale <= 0`` guard
        # raised TypeError on a non-numeric string and silently mis-divided on a
        # numeric string. Coerce first, then validate, so a bad scale degrades to
        # ``None`` (the engine then falls back to breadth) instead of crashing.
        scale_f = safe_float(scale)
        if scale_f is None or scale_f <= 0:
            return None
        return clamp((sum(parts) / len(parts)) / scale_f, -1.0, 1.0)

    def _fii_zscore(self, fii: Dict[str, Any],
                    window: int) -> Optional[float]:
        """Standardised FII flow vs its own recent distribution.

        A single day of FII flow is noisy: +2,000 Cr is unremarkable in a week
        that has been running +/-4,000 Cr, but extreme in a quiet month. The
        z-score measures *unusualness*, which is what a desk actually trades.

        Input is ``fii["history"]`` - a time-ordered list of daily net-cash
        values (oldest first), optionally ending with today's print. Returns
        ``None`` when there is not enough history, so the engine degrades to the
        point score alone.

        ``z`` is the standard score of the LATEST value against the window
        *including* it; a flat window yields ``None`` (no information).
        """
        if not isinstance(fii, dict):
            return None
        hist = fii.get("history")
        if not isinstance(hist, (list, tuple)) or len(hist) < 3:
            return None
        values = [safe_float(v) for v in hist]
        values = [v for v in values if v is not None]
        if len(values) < 3:
            return None
        window = max(3, int(window or 20))
        sample = values[-window:]
        latest = sample[-1]
        mean = sum(sample) / len(sample)
        if len(sample) < 2:
            return None
        var = sum((v - mean) ** 2 for v in sample) / (len(sample) - 1)
        sd = math.sqrt(max(var, 0.0))
        if sd <= 0:
            return None
        # tanh keeps a 4-sigma day from dominating the composite the same way
        # the Economic engine saturates its surprise view.
        return math.tanh((latest - mean) / sd)

    def _volatility_state(self, data: Dict[str, Any],
                          warnings: List[str]):
        """Classify annualized realized volatility from explicit inputs only."""
        raw_volatility = data.get(
            "annualized_volatility_pct", data.get("realized_volatility_pct"))
        volatility = safe_float(raw_volatility)
        if raw_volatility is not None and (
                volatility is None or volatility < 0):
            warnings.append(
                "invalid annualized volatility; using conservative elevated state")
            volatility = None
        returns = data.get("daily_returns_pct")
        if volatility is None and isinstance(returns, list) and len(returns) >= 2:
            values = [None if isinstance(value, bool) else safe_float(value)
                      for value in returns]
            if all(value is not None for value in values):
                mean_return = sum(values) / len(values)
                variance = sum((value - mean_return) ** 2
                               for value in values) / (len(values) - 1)
                volatility = math.sqrt(max(variance, 0.0) * 252.0)
            else:
                warnings.append(
                    "invalid daily return series; realized volatility unavailable")

        low_limit = self.low_volatility_pct
        high_limit = self.high_volatility_pct
        panic = any(bool(data.get(key)) for key in (
            "extreme_market_panic", "latency_anomaly", "corrupted_data"))
        if panic or (volatility is not None and volatility >= high_limit):
            state, lookback = "High_Panic", 10
        elif volatility is not None and volatility < low_limit:
            state, lookback = "Low", 50
        elif volatility is not None:
            state, lookback = "Elevated", 20
        else:
            state, lookback = "Elevated", 14
        rationale = (
            "Explicit panic/data-integrity alert is active." if panic else
            f"Annualized realized volatility is {volatility:.2f}%." if volatility is not None else
            "Volatility data unavailable; using conservative elevated state.")
        return state, lookback, volatility, rationale

    @staticmethod
    def _short_term_bias(indices: Dict[str, Dict[str, Any]]) -> str:
        if not indices:
            return "Sideways"
        reference = indices.get("NIFTY") or next(iter(indices.values()))
        values = [reference.get(key) for key in
                  ("return_1d_pct", "return_1m_pct")]
        values = [value for value in values if value is not None]
        if not values:
            return "Sideways"
        direction = sum(values) / len(values)
        return "Bullish" if direction > 0 else "Bearish" if direction < 0 else "Sideways"

    # ---- main ---------------------------------------------------------- #
    def _run(self, context: Dict[str, Any]) -> EngineResult:
        warnings: List[str] = []
        data = self._collect(context)
        if not data:
            return EngineResult(
                engine=self.name, ok=True,
                data={"risk_appetite": 0.0, "breadth_score": 0.0,
                      "leaders": [], "laggards": [], "tradable_universe": [],
                      "market_volatility_state": "Elevated",
                      "technical_lookback_days": 14,
                      "short_term_bias": "Sideways",
                      "realized_volatility_pct": None,
                      "rationale": "No market data supplied; using conservative elevated state."},
                warnings=["no market data supplied; neutral by default"],
            )

        indices = self._index_returns(data.get("indices") or {})
        commodities = self._commodity_returns(data.get("commodities") or {})

        # Breadth: fraction advancing, mapped from [0,1] to [-1,1].
        daily = [v["return_1d_pct"] for v in indices.values()
                 if v["return_1d_pct"] is not None]
        breadth_score = 0.0
        if daily:
            up = sum(1 for r in daily if r > 0)
            breadth_score = (up / len(daily)) * 2.0 - 1.0

        # Leaders / laggards by 1-month return (fallback: 1-day).
        def _sort_key(item):
            v = item[1]
            return v["return_1m_pct"] if v["return_1m_pct"] is not None else (
                v["return_1d_pct"] if v["return_1d_pct"] is not None else -1e9)

        ranked = sorted(indices.items(), key=_sort_key, reverse=True)
        leaders = [k for k, _ in ranked[:5]]
        laggards = [k for k, _ in ranked[-5:]][::-1] if len(ranked) > 5 else []

        # FII flow score. 6000 Cr = the ~95th percentile of
        # |daily FII net cash| over 170 trading days of vendor data, so a normal
        # day lands mid-band instead of pinning the score at the clamp.
        fii_scale = safe_float(self.config.get("fii_scale"))
        if fii_scale is None or fii_scale <= 0:
            fii_scale = 6000.0
        fii_block = data.get("fii") or {}
        fii_point = self._fii_score(fii_block, fii_scale)
        fii_z = self._fii_zscore(
            fii_block, int(self.config.get("fii_zscore_window", 20)))

        # Blend the point score with the z-score when history is available.
        # ``fii_zscore_weight`` 0.0 reproduces the point-only behaviour exactly,
        # so a deployment that has no flow history is unaffected.
        z_weight = clamp(safe_float(self.config.get("fii_zscore_weight")) or 0.0,
                         0.0, 1.0)
        fii_score = fii_point
        if fii_point is not None and fii_z is not None and z_weight > 0:
            fii_score = clamp((1.0 - z_weight) * fii_point + z_weight * fii_z,
                              -1.0, 1.0)
        if fii_point is None:
            warnings.append("no FII flow data (risk appetite uses breadth only)")

        # Composite risk appetite.
        w_fii = clamp(self.fii_weight, 0.0, 1.0)
        if fii_score is None:
            risk_appetite = breadth_score
        else:
            risk_appetite = (1 - w_fii) * breadth_score + w_fii * fii_score
        risk_appetite = clamp(risk_appetite, -1.0, 1.0)

        tradable = [name for name, v in indices.items()
                    if v.get("trend_confirmed")]

        # Commodity complex return (equal-weighted).
        commodity_avg = mean([v["return_1d_pct"] for v in commodities.values()
                              if v["return_1d_pct"] is not None])
        volatility_state, lookback, volatility, rationale = \
            self._volatility_state(data, warnings)
        bias = self._short_term_bias(indices)
        agent_advice = context.get("agent_advice")
        market_advice = (
            agent_advice.get("market") if isinstance(agent_advice, dict) else None)
        if isinstance(market_advice, dict):
            severity = {"Low": 0, "Elevated": 1, "High_Panic": 2}
            suggested_state = market_advice.get("market_volatility_state")
            if severity.get(suggested_state, 0) > severity[volatility_state]:
                volatility_state = suggested_state
                rationale = "Agent flagged higher volatility risk; deterministic risk stage can veto."
            suggested_lookback = market_advice.get("technical_lookback_days")
            if (suggested_lookback in (10, 14, 20, 50)
                    and suggested_lookback <= lookback):
                lookback = suggested_lookback
            suggested_bias = market_advice.get("short_term_bias")
            if suggested_bias in ("Bullish", "Bearish", "Sideways"):
                bias = suggested_bias

        self.log(f"breadth={breadth_score:+.2f} fii={fii_score} "
                 f"risk_appetite={risk_appetite:+.2f} "
                 f"tradable={len(tradable)}/{len(indices)}")

        return EngineResult(
            engine=self.name, ok=True,
            data={
                "risk_appetite": round(risk_appetite, 4),
                "breadth_score": round(breadth_score, 4),
                "fii_score": None if fii_score is None else round(fii_score, 4),
                # Both views are reported so an operator can see WHICH one moved
                # risk appetite: the level of today's flow, or how unusual it is
                # versus the last N days.
                "fii_point_score": (None if fii_point is None
                                    else round(fii_point, 4)),
                "fii_zscore": None if fii_z is None else round(fii_z, 4),
                "fii_zscore_weight": z_weight,
                "fii_scale_cr": fii_scale,
                "commodity_market_return": None if commodity_avg is None
                else round(commodity_avg, 4),
                "index_returns": indices,
                "commodities": commodities,
                "leaders": leaders,
                "laggards": laggards,
                "tradable_universe": tradable,
                "market_volatility_state": volatility_state,
                "technical_lookback_days": lookback,
                "short_term_bias": bias,
                "realized_volatility_pct": (
                    None if volatility is None else round(volatility, 4)),
                "rationale": " ".join(rationale.split()[:15]),
            },
            warnings=warnings,
        )


__all__ = ["MarketEngine"]