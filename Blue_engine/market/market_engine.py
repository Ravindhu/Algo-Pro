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
        # Normalise with the configured scale (default ~ thousands of
        # currency units of net flow = a strong signal).
        return clamp((sum(parts) / len(parts)) / scale, -1.0, 1.0)

    # ---- main ---------------------------------------------------------- #
    def _run(self, context: Dict[str, Any]) -> EngineResult:
        warnings: List[str] = []
        data = self._collect(context)
        if not data:
            return EngineResult(
                engine=self.name, ok=True,
                data={"risk_appetite": 0.0, "breadth_score": 0.0,
                      "leaders": [], "laggards": [], "tradable_universe": []},
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

        # FII flow score.
        fii_scale = safe_float(self.config.get("fii_scale")) or 1000.0
        fii_score = self._fii_score(data.get("fii") or {}, fii_scale)
        if fii_score is None:
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

        self.log(f"breadth={breadth_score:+.2f} fii={fii_score} "
                 f"risk_appetite={risk_appetite:+.2f} "
                 f"tradable={len(tradable)}/{len(indices)}")

        return EngineResult(
            engine=self.name, ok=True,
            data={
                "risk_appetite": round(risk_appetite, 4),
                "breadth_score": round(breadth_score, 4),
                "fii_score": None if fii_score is None else round(fii_score, 4),
                "commodity_market_return": None if commodity_avg is None
                else round(commodity_avg, 4),
                "index_returns": indices,
                "commodities": commodities,
                "leaders": leaders,
                "laggards": laggards,
                "tradable_universe": tradable,
            },
            warnings=warnings,
        )


__all__ = ["MarketEngine"]