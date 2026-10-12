"""Explicit control-loop primitives for validation, monitoring and TCA."""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .costs import CostModel
from .engine import BacktestConfig, BacktestResult, run_backtest

logger = logging.getLogger("blue_engine.backtest.loops")

__all__ = [
    "ReoptimizationSignal", "LiveMonitor", "ReconciliationResult",
    "SlippageReconciler",
]


@dataclass(frozen=True)
class ReoptimizationSignal:
    triggered: bool
    reasons: Tuple[str, ...]
    state: str
    pause_new_entries: bool
    risk_scale: float


class LiveMonitor:
    """Observer-style performance monitor; it never places or cancels orders.

    The live orchestrator should subscribe its entry-pause/risk-control handler
    to the emitted signal. A trip returns ``risk_scale=0`` and
    ``pause_new_entries=True``; resuming remains a separate health-check and
    operator-controlled decision.
    """

    def __init__(self, historical_envelope: Dict[str, Any], *,
                 drawdown_multiple: float = 1.5,
                 loss_streak_multiple: float = 1.4,
                 regime_std_multiple: float = 2.0,
                 regime_window: int = 10) -> None:
        if regime_window < 2:
            raise ValueError("regime_window must be >= 2")
        multiples = (drawdown_multiple, loss_streak_multiple,
                     regime_std_multiple)
        if any(not math.isfinite(value) or value <= 0 for value in multiples):
            raise ValueError("monitor thresholds must be finite and positive")
        self.envelope = dict(historical_envelope)
        self.drawdown_multiple = float(drawdown_multiple)
        self.loss_streak_multiple = float(loss_streak_multiple)
        self.regime_std_multiple = float(regime_std_multiple)
        self.regime_window = int(regime_window)
        self._observers: List[Callable[[ReoptimizationSignal], None]] = []

    def subscribe(self, observer: Callable[[ReoptimizationSignal], None]) -> None:
        if not callable(observer):
            raise TypeError("observer must be callable")
        if observer not in self._observers:
            self._observers.append(observer)

    def unsubscribe(self, observer: Callable[[ReoptimizationSignal], None]) -> None:
        self._observers.remove(observer)

    def evaluate(self, *, live_equity: Sequence[float],
                 live_trade_pnls: Sequence[float],
                 live_daily_returns: Sequence[float] = (),
                 historical_daily_returns: Sequence[float] = ()
                 ) -> ReoptimizationSignal:
        """Evaluate live drawdown, losing streak, and rolling-volatility shift."""
        reasons: List[str] = []
        equity = [float(value) for value in live_equity]
        if any(not math.isfinite(value) for value in equity):
            raise ValueError("live_equity must contain only finite values")
        if equity:
            if equity[-1] <= 0:
                reasons.append("live equity is non-positive")
            peak = max(equity)
            current_dd_pct = (
                (peak - equity[-1]) / peak * 100.0 if peak > 0 else 0.0)
            historical_dd_pct = self.envelope.get("max_dd")
            if historical_dd_pct is not None:
                historical_dd_pct = float(historical_dd_pct)
                if current_dd_pct > self.drawdown_multiple * historical_dd_pct:
                    reasons.append(
                        f"live drawdown {current_dd_pct:.4f}% exceeds "
                        f"{self.drawdown_multiple:g}x historical "
                        f"{historical_dd_pct:.4f}%")

        historical_streak = self.envelope.get("max_losing_streak")
        if historical_streak is not None:
            streak = 0
            pnls = [float(pnl) for pnl in live_trade_pnls]
            if any(not math.isfinite(value) for value in pnls):
                raise ValueError("live_trade_pnls must be finite")
            for value in reversed(pnls):
                if value >= 0:
                    break
                streak += 1
            threshold = self.loss_streak_multiple * int(historical_streak)
            if streak > threshold:
                reasons.append(
                    f"live losing streak {streak} exceeds "
                    f"{self.loss_streak_multiple:g}x historical "
                    f"{int(historical_streak)}")

        live_vols = self._rolling_volatility(
            live_daily_returns, self.regime_window)
        historical_vols = self._rolling_volatility(
            historical_daily_returns, self.regime_window)
        if live_vols and len(historical_vols) >= 2:
            baseline_mean = statistics.fmean(historical_vols)
            baseline_std = statistics.pstdev(historical_vols)
            latest = live_vols[-1]
            if abs(latest - baseline_mean) > \
                    self.regime_std_multiple * baseline_std:
                reasons.append(
                    f"rolling volatility {latest:.6g} is more than "
                    f"{self.regime_std_multiple:g} historical standard "
                    "deviations from baseline")

        triggered = bool(reasons)
        signal = ReoptimizationSignal(
            triggered=triggered, reasons=tuple(reasons),
            state="RE_OPTIMIZING" if triggered else "LIVE",
            pause_new_entries=triggered, risk_scale=0.0 if triggered else 1.0)
        for observer in tuple(self._observers):
            observer(signal)
        if triggered:
            logger.warning("live monitor requested re-optimization: %s",
                           "; ".join(reasons))
        return signal

    @staticmethod
    def _rolling_volatility(values: Sequence[float],
                            window: int) -> List[float]:
        clean = [float(value) for value in values]
        if any(not math.isfinite(value) for value in clean):
            raise ValueError("return series must contain only finite values")
        if len(clean) < window:
            return []
        return [
            statistics.pstdev(clean[index - window + 1:index + 1])
            for index in range(window - 1, len(clean))
        ]


@dataclass(frozen=True)
class ReconciliationResult:
    triggered: bool
    matched_fills: int
    mean_slippage_delta_bps: float
    previous_slippage_bps: float
    calibrated_slippage_bps: float
    cost_model: CostModel
    reason: Optional[str] = None
    backtest: Optional[BacktestResult] = None


class SlippageReconciler:
    """Calibrate simulated slippage from caller-matched live long-entry fills."""

    def __init__(self, *, threshold_bps: float = 1.0) -> None:
        if not math.isfinite(threshold_bps) or threshold_bps < 0:
            raise ValueError("threshold_bps must be finite and non-negative")
        self.threshold_bps = float(threshold_bps)

    def reconcile(self, *, cost_model: CostModel,
                  matched_fills: Iterable[Dict[str, Any]]) -> ReconciliationResult:
        """Update a copied cost model using unique, explicitly matched fills.

        Each record must include a unique ``fill_id``, ``side='BUY'``,
        ``reference_price``, ``backtest_price`` and ``live_price``. Matching is
        deliberately required from the caller; timestamps alone are not a safe
        way to pair executions from different systems.
        """
        deltas: List[float] = []
        seen_ids = set()
        for fill in matched_fills:
            if not isinstance(fill, dict):
                raise TypeError("each matched fill must be a dictionary")
            fill_id = fill.get("fill_id")
            if not fill_id or fill_id in seen_ids:
                raise ValueError("matched fills need unique non-empty fill_id values")
            seen_ids.add(fill_id)
            side = str(fill.get("side", "")).upper()
            if side not in ("BUY", "SELL"):
                raise ValueError(f"fill {fill_id!r} has invalid side {side!r}")
            if side != "BUY":
                continue
            reference = float(fill["reference_price"])
            simulated = float(fill["backtest_price"])
            live = float(fill["live_price"])
            if reference <= 0 or not all(
                    math.isfinite(value) for value in (reference, simulated, live)):
                raise ValueError(f"fill {fill_id!r} contains invalid prices")
            deltas.append((live - simulated) / reference * 10000.0)
        if not deltas:
            raise ValueError("no matched BUY fills available for slippage calibration")

        mean_delta = statistics.fmean(deltas)
        previous = float(cost_model.slippage_bps)
        triggered = mean_delta > self.threshold_bps
        calibrated = (max(0.0, previous + mean_delta) if triggered else previous)
        updated = (cost_model.calibrated(
            slippage_bps=calibrated,
            reason=f"matched live fills: delta {mean_delta:.4f} bps")
            if triggered else cost_model)
        return ReconciliationResult(
            triggered=triggered, matched_fills=len(deltas),
            mean_slippage_delta_bps=mean_delta,
            previous_slippage_bps=previous,
            calibrated_slippage_bps=calibrated,
            cost_model=updated,
            reason=("live slippage exceeded threshold" if triggered else
                    "live slippage delta is within threshold"))

    def reconcile_and_retest(self, *, cost_model: CostModel,
                             matched_fills: Iterable[Dict[str, Any]],
                             rows: Any, strategy: Any,
                             params: Dict[str, Any],
                             config: BacktestConfig, **metadata: Any
                             ) -> ReconciliationResult:
        """Calibrate, then rerun the identical data/strategy if costs changed."""
        result = self.reconcile(
            cost_model=cost_model, matched_fills=matched_fills)
        if not result.triggered:
            return result
        updated_config = replace(config, cost_model=result.cost_model)
        retest = run_backtest(
            rows, strategy, params, config=updated_config, **metadata)
        return replace(result, backtest=retest)
