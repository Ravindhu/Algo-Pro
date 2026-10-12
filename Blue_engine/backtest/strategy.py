"""Adapters connecting the deterministic backtester to optimizer strategies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .engine import BacktestConfig, run_backtest
from .metrics import returns_from_equity

__all__ = ["BacktestStrategyAdapter", "as_optimizer_strategy"]


@dataclass(frozen=True)
class BacktestStrategyAdapter:
    """Expose a target-position strategy through the optimizer's return API."""

    signal: Any
    config: BacktestConfig

    def __call__(self, params: Dict[str, Any], context: Dict[str, Any],
                 bars: Optional[list] = None) -> Dict[str, Any]:
        if bars is None or len(bars) < 2:
            return {"ok": False, "reason": "backtest requires at least two bars"}
        run_config = BacktestConfig(
            cost_model=self.config.cost_model,
            starting_equity=self.config.starting_equity,
            risk_free_per_period=self.config.risk_free_per_period,
            n_trials=1,
            periods_per_year=self.config.periods_per_year,
            max_position_fraction=self.config.max_position_fraction,
            allow_short=self.config.allow_short)
        result = run_backtest(
            bars, self.signal, params, config=run_config,
            symbol=str(context.get("symbol", "")),
            exchange=str(context.get("exchange", "")),
            interval=str(context.get("interval", "1d")))
        return {
            "ok": True,
            "returns": returns_from_equity(result.equity),
            "data_class": "historical:openalgo",
            "metrics": dict(result.metrics),
        }


def as_optimizer_strategy(signal: Any, *,
                          config: BacktestConfig) -> BacktestStrategyAdapter:
    """Wrap a target-position signal for :class:`OptimizerEngine`."""
    return BacktestStrategyAdapter(signal=signal, config=config)
