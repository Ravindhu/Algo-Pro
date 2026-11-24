"""
alpha.py - the Alpha engine.

Selects *what* to trade inside the portfolio sleeves. Three composable filters:

  1. Fundamental filter : quality / value / growth gates on per-name metrics
                          (PE, PB, ROE, debt/equity, earnings growth, ...).
                          Produces a normalised [0,1] fundamental score.
  2. Technical filter   : key-level / trend confirmation from OHLCV. Confirms
                          the entry *level* and direction (breakout above a
                          pivot, higher-highs, VWAP reclaim). Produces a
                          signed technical score in [-1, 1].
  3. Backtest           : a lightweight, deterministic event backtest of the
                          resulting signal (no pandas) so every candidate is
                          measured before it is allowed to trade.

Combined score = w_fund * fundamental + w_tech * technical (+ economic/market
tilt), mapped to a BUY / SELL / HOLD action. The Alpha engine is PURE: it only
reads the data it is given (provider / config), never the network.

Provider shape::

    {
      "symbols": ["INFY", "TCS"],
      "fundamentals": {"INFY": {"pe": 22, "roe": 0.31, "de": 0.1,
                                 "earnings_growth": 0.12}, ...},
      "prices":       {"INFY": {"close": 1480, "prev_high": 1470,
                                 "vwap": 1475, "sma_20": 1450, "sma_50": 1430}, ...},
      "history":      {"INFY": [{"close": 1450}, ...]},   # for the backtest
    }
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ..core.base import BaseEngine, EngineResult, EngineStage
from ..core.results import clamp, mean, safe_float, score_to_signal


# Direction: +1 means "higher is better". Used to normalise fundamentals.
DEFAULT_FUNDAMENTAL_RULES: Dict[str, Dict[str, Any]] = {
    "roe":              {"direction": +1, "weight": 1.5, "scale": 0.30},
    "earnings_growth":  {"direction": +1, "weight": 1.5, "scale": 0.30},
    "pe":               {"direction": -1, "weight": 1.0, "scale": 30.0},
    "pb":               {"direction": -1, "weight": 0.7, "scale": 4.0},
    "de":               {"direction": -1, "weight": 1.0, "scale": 1.0},
    "dividend_yield":   {"direction": +1, "weight": 0.5, "scale": 0.04},
}


class AlphaEngine(BaseEngine):
    """Fundamental + technical screening with an inline backtest gate."""

    name = "alpha"
    stage = EngineStage.ALPHA

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 **kwargs: Any):
        super().__init__(config, provider, kwargs.get("log"))
        self.rules = {k: dict(v) for k, v in DEFAULT_FUNDAMENTAL_RULES.items()}
        for key, override in (self.config.get("fundamental_rules") or {}).items():
            if key in self.rules and isinstance(override, dict):
                self.rules[key].update(override)
        self.w_fundamental = float(self.config.get("weight_fundamental", 1.0))
        self.w_technical = float(self.config.get("weight_technical", 1.5))
        self.min_score = float(self.config.get("min_score", 0.30))
        self.backtest_min_hit_rate = float(
            self.config.get("backtest_min_hit_rate", 0.45))
        self.backtest_horizon = int(self.config.get("backtest_horizon", 10))

    # ---- data ---------------------------------------------------------- #
    def _collect(self, context: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(context.get("alpha_inputs"), dict):
            return context["alpha_inputs"]
        if callable(self.provider):
            try:
                data = self.provider()
                if isinstance(data, dict):
                    return data
            except Exception as exc:  # noqa: BLE001
                self.log(f"provider failed ({exc!r})")
        if isinstance(self.config.get("alpha_inputs"), dict):
            return self.config["alpha_inputs"]
        return {}

    # ---- 1. fundamental filter ---------------------------------------- #
    def _fundamental_score(self, row: Dict[str, Any]) -> Optional[float]:
        weighted = 0.0
        wsum = 0.0
        for key, rule in self.rules.items():
            value = safe_float(row.get(key))
            if value is None:
                continue
            scale = safe_float(rule.get("scale")) or 1.0
            direction = float(rule.get("direction", 1))
            weight = float(rule.get("weight", 1.0))
            norm = clamp(direction * (value / scale), -1.0, 1.0)
            weighted += norm * weight
            wsum += weight
        if wsum == 0:
            return None
        return clamp(weighted / wsum, -1.0, 1.0)

    # ---- 2. technical level filter ------------------------------------ #
    def _technical_score(self, row: Dict[str, Any]) -> Optional[float]:
        close = safe_float(row.get("close"))
        if close is None:
            return None
        score = 0.0
        parts = 0

        prev_high = safe_float(row.get("prev_high"))
        if prev_high:
            score += 1.0 if close > prev_high else -1.0
            parts += 1

        vwap = safe_float(row.get("vwap"))
        if vwap:
            score += 1.0 if close >= vwap else -1.0
            parts += 1

        sma20 = safe_float(row.get("sma_20"))
        sma50 = safe_float(row.get("sma_50"))
        if sma20 and sma50:
            score += 1.0 if sma20 > sma50 else -1.0
            parts += 1

        if parts == 0:
            return None
        return clamp(score / parts, -1.0, 1.0)

    # ---- 3. inline backtest ------------------------------------------- #
    def _backtest(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Deterministic momentum backtest over a close series.

        Buys when close > mean of the prior window, exits after
        ``horizon`` bars, and reports the hit-rate and average return.
        Pure Python - no pandas/numpy.
        """
        closes = [safe_float(bar.get("close")) for bar in (history or [])]
        closes = [c for c in closes if c is not None]
        window = int(self.config.get("backtest_window", 5))
        horizon = max(1, self.backtest_horizon)
        if len(closes) < window + horizon + 1:
            return {"trades": 0, "hit_rate": None, "avg_return_pct": None}

        returns: List[float] = []
        for i in range(window, len(closes) - horizon):
            prior = closes[i - window:i]
            entry = closes[i]
            if entry > mean(prior):
                exit_price = closes[i + horizon]
                if entry:
                    returns.append((exit_price - entry) / entry * 100.0)

        if not returns:
            return {"trades": 0, "hit_rate": None, "avg_return_pct": None}
        hits = sum(1 for r in returns if r > 0)
        return {
            "trades": len(returns),
            "hit_rate": round(hits / len(returns), 4),
            "avg_return_pct": round(mean(returns), 4),
        }

    # ---- main ---------------------------------------------------------- #
    def _run(self, context: Dict[str, Any]) -> EngineResult:
        warnings: List[str] = []
        data = self._collect(context)
        symbols: List[str] = list(data.get("symbols") or [])
        if not symbols:
            return EngineResult(
                engine=self.name, ok=True,
                data={"candidates": [], "signals": {}},
                warnings=["no alpha inputs supplied; no candidates"],
            )

        fundamentals = data.get("fundamentals") or {}
        prices = data.get("prices") or {}
        history = data.get("history") or {}

        candidates: List[Dict[str, Any]] = []
        for symbol in symbols:
            fund = self._fundamental_score(fundamentals.get(symbol) or {})
            tech = self._technical_score(prices.get(symbol) or {})
            if fund is None and tech is None:
                warnings.append(f"no fundamental or technical data for {symbol}")
                continue

            combined = 0.0
            wsum = 0.0
            if fund is not None:
                combined += fund * self.w_fundamental
                wsum += self.w_fundamental
            if tech is not None:
                combined += tech * self.w_technical
                wsum += self.w_technical
            combined = clamp(combined / wsum, -1.0, 1.0) if wsum else 0.0

            bt = self._backtest(history.get(symbol) or [])
            passed_bt = (bt["hit_rate"] is None
                         or bt["hit_rate"] >= self.backtest_min_hit_rate)

            action = score_to_signal(combined)
            eligible = (abs(combined) >= self.min_score and passed_bt)
            if not passed_bt:
                warnings.append(
                    f"{symbol} failed backtest gate "
                    f"(hit_rate={bt['hit_rate']} < {self.backtest_min_hit_rate})")

            candidates.append({
                "symbol": symbol,
                "fundamental_score": None if fund is None else round(fund, 4),
                "technical_score": None if tech is None else round(tech, 4),
                "combined_score": round(combined, 4),
                "action": action,
                "eligible": eligible,
                "backtest": bt,
            })

        candidates.sort(key=lambda c: c["combined_score"], reverse=True)
        selected = [c["symbol"] for c in candidates if c["eligible"]]

        self.log(f"{len(candidates)} candidates, {len(selected)} eligible")

        return EngineResult(
            engine=self.name, ok=True,
            data={
                "candidates": candidates,
                "signals": {c["symbol"]: c["action"] for c in candidates},
                "selected": selected,
            },
            warnings=warnings,
        )


__all__ = ["AlphaEngine", "DEFAULT_FUNDAMENTAL_RULES"]