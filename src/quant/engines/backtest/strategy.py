"""Adapters connecting the deterministic backtester to optimizer strategies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .engine import BacktestConfig, run_backtest
from .metrics import returns_from_equity

__all__ = ["BacktestStrategyAdapter", "as_optimizer_strategy"]


def _closed_trade_records(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-trade fraction return with MAE/MFE, for closed trades only."""
    records: List[Dict[str, Any]] = []
    for t in trades:
        if t.get("exit_ts") is None:
            continue  # still open at end of data: excluded from excursion stats
        notional = float(t["entry_price"]) * float(t["quantity"])
        if notional <= 0:
            continue
        records.append({
            "ret": float(t["pnl"]) / notional,
            "pnl": float(t["pnl"]),
            "mae": float(t.get("mae", 0.0)),
            "mfe": float(t.get("mfe", 0.0)),
        })
    return records


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
            allow_short=self.config.allow_short,
            stop_loss_fraction=self.config.stop_loss_fraction,
            max_daily_loss_fraction=self.config.max_daily_loss_fraction,
            intra_bar_path=self.config.intra_bar_path)
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
            "risk_free_per_period": self.config.risk_free_per_period,
            "cost_bps": self.config.cost_model.round_trip_bps(),
            "costs_applied_once": True,
            "trades": _closed_trade_records(result.trades),
        }


def as_optimizer_strategy(signal: Any, *,
                          config: BacktestConfig) -> BacktestStrategyAdapter:
    """Wrap a target-position signal for :class:`OptimizerEngine`."""
    return BacktestStrategyAdapter(signal=signal, config=config)
