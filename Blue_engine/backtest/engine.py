"""
backtest/engine.py - the deterministic event loop + BacktestResult.

THIS MODULE'S JOB, IN ONE SENTENCE
=================================
Implement the deterministic OHLCV simulator and result contract used by the
IS/OOS gate, live monitor, and slippage reconciler in ``batch.py`` and
``loops.py``.

WHY THIS FILE EXISTS *NOW* AND NOT LATER
========================================
The package's docstring (see ``__init__.py``) commits to three feedback loops:

    Loop A  IS_OOS_*          - gates a parameter set before promotion
    Loop B  LiveMonitor        - watches live trading vs the backtest envelope
    Loop C  SlippageReconciler - calibrates the cost model from real fills

All three of them consume a ``BacktestResult``. Without a defined shape, every
loop has to invent its own representation, and the event loop that gets
written later cannot match all three. Building the loops against a frozen
result dataclass is what makes the future engine "drop-in": it has to fill the
shape, not invent it.

SO WHAT IS HERE?
================
This module provides:

  * :class:`BacktestConfig`     - frozen input (cost model + how many trials)
  * :class:`FillRecord`         - one simulated fill (price + cost terms)
  * :class:`BacktestResult`     - frozen output: equity + trades + costs + envelope
  * :func:`build_envelope`      - the historical baselines the loops compare to

``run_backtest`` fills every field on ``BacktestResult`` and executes signals
at the next bar's open.

CONTRACT (so the loops have something to bind to)
================================================
    equity         : list[float], oldest first, length == n_bars + 1
                     (the +1 is the mark at bar 0's close, the starting equity).
                     ``equity[0] == config.starting_equity``.
    trades         : list[dict], each ``{"pnl", "entry_ts", "exit_ts", ...}``.
    fills          : list[FillRecord], every simulated fill in chronological
                     order. Loop C (slippage) reads from here. A backtest that
                     does not record its own fills cannot reconcile against live.
    metrics        : dict, the output of ``backtest.metrics.compute_metrics``.
    bars_in_market : int, number of bars the strategy held a position.
    total_bars     : int, length of the bar series.
    starting_equity: float, what ``equity[0]`` is.
    n_trials       : int, how many parameter sets were evaluated to produce
                     THIS result. Travelling with the result is what stops the
                     deflated-Sharpe correction being silently neutered.
    warnings       : list[str], data repairs and other soft failures that an
                     operator must see.
    version        : str, equal to ``backtest.__version__``. Bumped when the
                     cost model or fill convention changes; two results from
                     different versions are never compared directly.
"""
from __future__ import annotations

import inspect
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .bars import BarSeries, build_series
from .metrics import compute_metrics
from . import __version__

__all__ = [
    "BacktestConfig", "FillRecord", "BacktestResult",
    "build_envelope", "EMPTY_RESULT", "run_backtest",
]


# --------------------------------------------------------------------------- #
# config + the fill record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BacktestConfig:
    """The frozen inputs to one backtest run.

    Attributes:
        cost_model: the ``CostModel`` that priced every fill. Storing it on the
            result means a reviewer can reproduce the cost number without
            guessing what the engine assumed.
        starting_equity: the cash the simulation begins with. ``equity[0]``
            equals this exactly; it is not a free parameter on the result.
        risk_free_per_period: per-period risk-free rate as a fraction. India is
            not zero-rate; leaving it at 0 silently flatters every Sharpe. The
            metric layer takes it from here, not from a module-level default.
        n_trials: how many parameter sets were evaluated to produce this result.
            1 means "single OOS confirmation run with a known search size"; the
            optimiser passes the real number. Honesty here is what makes the
            deflated-Sharpe correction meaningful.
        periods_per_year: 252 daily, 52 weekly. Stamped on the result so a
            report never has to second-guess the calendar.
    """

    cost_model: Any                                   # ``backtest.costs.CostModel``
    starting_equity: float = 100000.0
    risk_free_per_period: float = 0.0
    n_trials: int = 1
    periods_per_year: float = 252.0
    max_position_fraction: float = 1.0
    allow_short: bool = False
    stop_loss_fraction: Optional[float] = None
    max_daily_loss_fraction: Optional[float] = None
    intra_bar_path: str = "adverse_first"

    def __post_init__(self) -> None:
        # Validate here so a misconfigured call fails BEFORE the run starts,
        # not after an hour of simulation. Cost model is left duck-typed because
        # the actual class is in costs.py and importing it would create a
        # circular ref; the engine calls ``cost_model.round_trip_bps()`` and
        # ``cost_model.fill_price()`` on it, and a typo in the constructor is
        # caught the first time those are invoked.
        if not math.isfinite(self.starting_equity) or self.starting_equity <= 0:
            raise ValueError("starting_equity must be positive")
        if isinstance(self.n_trials, bool) or not isinstance(self.n_trials, int) \
                or self.n_trials < 1:
            raise ValueError("n_trials must be >= 1 (a search of size 0 is not a backtest)")
        if not math.isfinite(self.periods_per_year) or self.periods_per_year <= 0:
            raise ValueError("periods_per_year must be positive")
        if not math.isfinite(self.risk_free_per_period):
            raise ValueError("risk_free_per_period must be finite")
        if not math.isfinite(self.max_position_fraction) or \
                not 0 < self.max_position_fraction <= 1:
            raise ValueError("max_position_fraction must be in (0, 1]")
        for name, value in (("stop_loss_fraction", self.stop_loss_fraction),
                            ("max_daily_loss_fraction", self.max_daily_loss_fraction)):
            if value is not None and (
                    not math.isfinite(value) or not 0 < value < 1):
                raise ValueError(f"{name} must be in (0, 1) or None")
        if self.intra_bar_path != "adverse_first":
            raise ValueError("intra_bar_path must be 'adverse_first'")


@dataclass(frozen=True)
class FillRecord:
    """One simulated fill, with the cost terms itemised.

    Why this is frozen: a fill that can be retro-edited is a fill that the
    reconciler cannot trust. Loop C reads ``price`` and ``reference_price`` to
    compute ``Price_live - Price_backtest``; mutating either is how a past
    backtest silently changes its cost number.

    Attributes:
        ts: ISO timestamp of the bar the fill was PRICED off (the bar AFTER
            the decision bar). Slippage is always adverse to the trader.
        side: ``"BUY"`` or ``"SELL"``.
        reference_price: the price the engine expected to transact at
            (``next_bar.open``).
        price: the price the cost model said we actually got.
        notional: order notional in rupees.
        charges_bps, slippage_bps, latency_bps, impact_bps: the four cost terms,
            per side. Summed they equal ``total_bps``.
    """

    ts: str
    side: str
    reference_price: float
    price: float
    notional: float
    charges_bps: float
    slippage_bps: float
    latency_bps: float
    impact_bps: float

    @property
    def total_bps(self) -> float:
        return (self.charges_bps + self.slippage_bps
                + self.latency_bps + self.impact_bps)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "side": self.side,
            "reference_price": round(self.reference_price, 4),
            "price": round(self.price, 4),
            "notional": round(self.notional, 2),
            "charges_bps": round(self.charges_bps, 4),
            "slippage_bps": round(self.slippage_bps, 4),
            "latency_bps": round(self.latency_bps, 4),
            "impact_bps": round(self.impact_bps, 4),
            "total_bps": round(self.total_bps, 4),
        }


# --------------------------------------------------------------------------- #
# the result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BacktestResult:
    """The full, frozen output of one backtest run.

    Frozen so a result stored on disk cannot be mutated to look better than the
    run that produced it. ``to_dict()`` is the only legal way out, and it
    copies - it does not hand back the underlying lists.

    The fields are deliberately the superset of what every loop needs, so the
    loops do not have to invent fields of their own:

      * IS_OOS_*        reads ``metrics``, ``envelope``.
      * LiveMonitor     reads ``equity``, ``metrics``, ``envelope``.
      * SlippageReconciler reads ``fills``.

    ``run_backtest`` populates the complete result contract, so a loop never
    silently falls back to a missing field.
    """

    equity: List[float]
    trades: List[Dict[str, Any]]
    fills: List[FillRecord]
    metrics: Dict[str, Any]
    bars_in_market: int
    total_bars: int
    starting_equity: float
    n_trials: int
    envelope: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)
    version: str = __version__

    # -- the legal escape hatches ------------------------------------------ #

    def to_dict(self) -> Dict[str, Any]:
        """A JSON-safe copy. Lists are copied so the result's mutability after
        ``to_dict`` does not affect the frozen original."""
        return {
            "version": self.version,
            "starting_equity": round(self.starting_equity, 4),
            "n_trials": int(self.n_trials),
            "n_bars": int(self.total_bars),
            "bars_in_market": int(self.bars_in_market),
            "equity": [round(float(e), 4) for e in self.equity],
            "trades": list(self.trades),
            "fills": [f.to_dict() for f in self.fills],
            "metrics": dict(self.metrics),
            "envelope": dict(self.envelope),
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- #
# the historical envelope
# --------------------------------------------------------------------------- #


def build_envelope(*, equity: Sequence[float],
                   metrics: Dict[str, Any],
                   losing_streak: int) -> Dict[str, Any]:
    """The historical baselines the live monitor and the slippage reconciler
    compare against.

    Why these exact four numbers:

      * ``max_dd``         - the worst drawdown the backtest recorded. Loop B
                             trips if live exceeds ``LIVE_DRAWDOWN_MULT * this``.
      * ``sharpe``         - the run's headline. Used by the OOS gate (Loop A)
                             and by the monitor (the deflated Sharpe is on the
                             metrics block, but the raw is the floor).
      * ``max_losing_streak`` - the longest consecutive-loss run. Tripped by
                             ``LIVE_LOSS_STREAK_MULT * this`` in Loop B.
      * ``bar_count``      - the bar count, so the monitor can express trip
                             thresholds in the same unit.

    Args:
        equity: the equity curve from the result.
        metrics: the metrics dict from ``compute_metrics``. We pull sharpe and
            max_drawdown from here rather than recomputing - one definition of
            each, everywhere.
        losing_streak: precomputed longest losing-streak length (the
            ``metrics.max_consecutive_losses`` field).

    Returns:
        A plain dict the loops can read directly and the store can serialise.
        None values are preserved so a downstream consumer sees "missing",
        never "0.0" pretending to be a number.
    """
    return {
        "max_dd": metrics.get("max_drawdown_pct"),
        "sharpe": metrics.get("sharpe"),
        "max_losing_streak": int(losing_streak),
        "bar_count": len(equity) - 1,                       # equity has n_bars+1 points
    }


# --------------------------------------------------------------------------- #
# an empty result, for tests + for "nothing to score" branches
# --------------------------------------------------------------------------- #


#: A zero-length result used by tests, and by any loop branch that needs a
#: well-typed "nothing here" value rather than a hand-rolled dict.
EMPTY_RESULT = BacktestResult(
    equity=[],
    trades=[],
    fills=[],
    metrics={},
    bars_in_market=0,
    total_bars=0,
    starting_equity=0.0,
    n_trials=1,
    envelope={"max_dd": None, "sharpe": None,
              "max_losing_streak": 0, "bar_count": 0},
    warnings=("empty result (no bars scored)",),
)


def _target_fraction(signal: Any, *, max_fraction: float,
                     allow_short: bool) -> float:
    """Validate and normalize a strategy's target exposure."""
    if isinstance(signal, dict):
        if "target_position" in signal:
            signal = signal["target_position"]
        elif "position" in signal:
            signal = signal["position"]
        else:
            raise ValueError(
                "strategy result dict must contain 'target_position' or 'position'")
    if isinstance(signal, bool):
        raise ValueError("strategy target position must be a finite number")
    try:
        target = float(signal)
    except (TypeError, ValueError) as exc:
        raise ValueError("strategy target position must be a finite number") from exc
    if not math.isfinite(target):
        raise ValueError("strategy target position must be finite")
    if not allow_short and target < 0:
        raise ValueError("negative target position requires allow_short=True")
    if abs(target) > max_fraction:
        raise ValueError(
            f"target position {target} exceeds configured limit {max_fraction}")
    return target


def _call_signal(strategy: Any, params: Dict[str, Any],
                 context: Dict[str, Any], view: Any) -> Any:
    """Call a signal using the documented (params, context, bars) contract."""
    try:
        signature = inspect.signature(strategy)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        try:
            signature.bind(params, context, view)
        except TypeError:
            try:
                signature.bind(params, context, bars=view)
            except TypeError:
                pass
            else:
                return strategy(params, context, bars=view)
            try:
                signature.bind(params, context)
            except TypeError as exc:
                raise TypeError(
                    "strategy must accept (params, context[, bars])") from exc
            return strategy(params, context)
    return strategy(params, context, view)


def run_backtest(rows: Any, strategy: Any, params: Dict[str, Any], *,
                 config: BacktestConfig, symbol: str = "",
                 exchange: str = "", interval: str = "1d",
                 strict: bool = True) -> BacktestResult:
    """Run a deterministic, long-only-by-default single-pass OHLCV simulation.

    A signal is evaluated after each bar closes and is executed at the next
    bar's open. Strategies receive only the prefix ending at their decision
    bar. The signal must return a target exposure fraction in
    ``[-max_position_fraction, max_position_fraction]`` (negative exposures
    require ``config.allow_short``). Quantity is rounded down to whole shares.

    ``rows`` may be a :class:`BarSeries` or cached OHLCV dictionaries. No
    network, broker, or live-configuration access occurs here. Invalid data,
    signals, and cost-model failures are raised to the caller rather than
    converted into success-shaped results.
    """
    if not callable(strategy):
        raise TypeError("strategy must be callable")
    if not isinstance(params, dict):
        raise TypeError("params must be a dictionary")
    if isinstance(rows, BarSeries):
        series = rows
    else:
        series = build_series(rows, symbol=symbol, exchange=exchange,
                              interval=interval, strict=strict)
    if len(series) < 2:
        raise ValueError("backtest requires at least two valid bars")

    equity: List[float] = [float(config.starting_equity)]
    trades: List[Dict[str, Any]] = []
    fills: List[FillRecord] = []
    warnings = list(series.warnings)
    cash = float(config.starting_equity)
    quantity = 0
    average_entry_reference = 0.0
    average_entry_ts: Optional[str] = None
    entry_cost_remaining = 0.0
    costs_paid = 0.0
    bars_in_market = 0
    previous_equity = float(config.starting_equity)
    pending_target: Optional[float] = None
    entry_price_for_stop: Optional[float] = None
    current_day = None
    day_start_equity = float(config.starting_equity)
    daily_halt = False
    stop_exit_pending = False
    stop_exit_reference: Optional[float] = None
    for index, bar in enumerate(series.bars):
        # Timestamps are sortable strings from the validated bar adapter. Daily
        # loss is measured from the equity mark at each new calendar date.
        bar_day = bar.ts[:10]
        if bar_day != current_day:
            current_day = bar_day
            day_start_equity = previous_equity
            # A daily risk halt is session-scoped; it resets on the next date.
            daily_halt = False
        # Check stop before strategy target execution. If both OHLC extremes
        # could hit a stop/target inside a bar, adverse_first makes no favorable
        # target assumption: a stop is considered hit whenever low/high crosses.
        stop_triggered = False
        if (quantity and entry_price_for_stop is not None
                and config.stop_loss_fraction is not None):
            if quantity > 0:
                stop_price = entry_price_for_stop * (1.0 - config.stop_loss_fraction)
                stop_triggered = bar.low <= stop_price
                stop_fill_reference = min(bar.open, stop_price) if stop_triggered else bar.open
            else:
                stop_price = entry_price_for_stop * (1.0 + config.stop_loss_fraction)
                stop_triggered = bar.high >= stop_price
                stop_fill_reference = max(bar.open, stop_price) if stop_triggered else bar.open
            if stop_triggered:
                stop_exit_pending = True
                stop_exit_reference = stop_fill_reference
                pending_target = 0.0
                warnings.append(f"stop loss triggered at {bar.ts}; adverse-first OHLC convention")
        if daily_halt and pending_target is not None and pending_target != 0:
            pending_target = 0.0
        if stop_exit_pending:
            pending_target = 0.0
        if pending_target is not None:
            target_quantity = (
                int(math.floor(previous_equity * abs(pending_target) / bar.open))
                if previous_equity > 0 else 0)
            if pending_target < 0:
                target_quantity = -target_quantity
            delta = target_quantity - quantity
            if delta > 0:
                low, high = 0, delta
                while low < high:
                    candidate = (low + high + 1) // 2
                    candidate_notional = candidate * bar.open
                    candidate_fill = config.cost_model.fill_price(
                        side="BUY", reference_price=bar.open,
                        order_notional=candidate_notional,
                        bar_notional=bar.volume * bar.close)
                    candidate_price = float(candidate_fill["price"])
                    candidate_fee = (
                        candidate * candidate_price
                        * float(candidate_fill["charges_bps"]) / 10000.0)
                    if candidate * candidate_price + candidate_fee <= cash:
                        low = candidate
                    else:
                        high = candidate - 1
                delta = low
                target_quantity = quantity + delta
            if delta:
                side = "BUY" if delta > 0 else "SELL"
                order_quantity = abs(delta)
                reference_price = (stop_exit_reference
                                   if stop_exit_pending and delta * quantity < 0
                                   else bar.open)
                order_notional = order_quantity * reference_price
                bar_notional = bar.volume * bar.close
                impact_bps = config.cost_model.impact_bps(
                    order_notional=order_notional,
                    bar_notional=bar_notional)
                fill = config.cost_model.fill_price(
                    side=side, reference_price=reference_price,
                    order_notional=order_notional,
                    bar_notional=bar_notional)
                fill_price = float(fill["price"])
                charges_bps = float(fill["charges_bps"])
                fee = order_quantity * fill_price * charges_bps / 10000.0
                execution_cost = order_quantity * abs(fill_price - reference_price)
                total_fill_cost = fee + execution_cost
                costs_paid += total_fill_cost
                cash -= delta * fill_price + fee
                fills.append(FillRecord(
                    ts=bar.ts, side=side, reference_price=reference_price,
                    price=fill_price, notional=order_quantity * fill_price,
                    charges_bps=charges_bps,
                    slippage_bps=float(config.cost_model.slippage_bps),
                    latency_bps=float(config.cost_model.latency_bps),
                    impact_bps=float(impact_bps)))

                if quantity == 0 or quantity * delta > 0:
                    old_abs = abs(quantity)
                    new_abs = abs(delta)
                    if old_abs == 0:
                        average_entry_ts = bar.ts
                        entry_price_for_stop = fill_price
                        average_entry_reference = reference_price
                    else:
                        average_entry_reference = (
                            (average_entry_reference * old_abs + reference_price * new_abs)
                            / (old_abs + new_abs))
                        if config.stop_loss_fraction is not None:
                            entry_price_for_stop = average_entry_reference

                    entry_cost_remaining += total_fill_cost
                else:
                    closed_quantity = min(abs(quantity), abs(delta))
                    entry_cost_allocated = (
                        entry_cost_remaining * closed_quantity / abs(quantity))
                    exit_cost_allocated = (
                        total_fill_cost * closed_quantity / abs(delta))
                    direction = 1 if quantity > 0 else -1
                    gross_pnl = (
                        (reference_price - average_entry_reference)
                        * closed_quantity * direction)
                    trades.append({
                        "entry_ts": average_entry_ts,
                        "exit_ts": bar.ts,
                        "side": "LONG" if direction > 0 else "SHORT",
                        "quantity": closed_quantity,
                        "entry_price": average_entry_reference,
                        "exit_price": reference_price,
                        "gross_pnl": gross_pnl,
                        "costs": entry_cost_allocated + exit_cost_allocated,
                        "pnl": gross_pnl - entry_cost_allocated - exit_cost_allocated,
                    })
                    entry_cost_remaining -= entry_cost_allocated
                    if target_quantity == 0 or target_quantity * quantity < 0:
                        entry_cost_remaining = 0.0
                        if target_quantity == 0:
                            entry_price_for_stop = None
                        if target_quantity == 0:
                            average_entry_ts = None
                    if target_quantity * quantity < 0:
                        average_entry_reference = reference_price
                        entry_price_for_stop = fill_price
                        average_entry_ts = bar.ts
                        entry_cost_remaining = total_fill_cost - exit_cost_allocated
                quantity = target_quantity
                if stop_exit_pending and quantity == 0:
                    stop_exit_pending = False
                    stop_exit_reference = None
        marked_equity = cash + quantity * bar.close
        if not math.isfinite(marked_equity):
            raise ArithmeticError(f"non-finite equity at {bar.ts}")
        equity.append(marked_equity)
        previous_equity = marked_equity
        if quantity:
            bars_in_market += 1
        if (config.max_daily_loss_fraction is not None
                and day_start_equity > 0
                and marked_equity <= day_start_equity
                    * (1.0 - config.max_daily_loss_fraction)):
            if not daily_halt:
                warnings.append(
                    f"daily loss limit triggered at {bar.ts}; new entries blocked for {bar_day}")
            daily_halt = True
            pending_target = 0.0
            if quantity:
                pending_target = 0.0
        if index < len(series) - 1:
            context = {
                "symbol": series.symbol or symbol,
                "exchange": series.exchange or exchange,
                "interval": series.interval or interval,
                "timestamp": bar.ts,
                "bar_index": index,
                "equity": marked_equity,
                "quantity": quantity,
                "risk_free_per_period": config.risk_free_per_period,
                "periods_per_year": config.periods_per_year,
                "cost_bps": config.cost_model.round_trip_bps(),
            }
            if daily_halt:
                pending_target = 0.0
            elif stop_exit_pending:
                pending_target = 0.0
            else:
                signal = _call_signal(strategy, dict(params), context,
                                      series.view_upto(index))
                pending_target = _target_fraction(
                    signal, max_fraction=config.max_position_fraction,
                    allow_short=config.allow_short)

    if quantity:
        if stop_exit_pending or daily_halt:
            warnings.append(
                "risk exit could not execute: no later bar is available; "
                "position remains marked open at end of data")
        direction = 1 if quantity > 0 else -1
        last_bar = series.bars[-1]
        gross_mark_pnl = (
            (last_bar.close - average_entry_reference) * abs(quantity) * direction)
        trades.append({
            "entry_ts": average_entry_ts,
            "exit_ts": None,
            "side": "LONG" if direction > 0 else "SHORT",
            "quantity": abs(quantity),
            "entry_price": average_entry_reference,
            "exit_price": last_bar.close,
            "gross_pnl": gross_mark_pnl,
            "costs": entry_cost_remaining,
            "pnl": gross_mark_pnl - entry_cost_remaining,
            "status": "OPEN",
        })

    metrics = compute_metrics(
        equity=equity, trades=trades, total_bars=len(series),
        bars_in_market=bars_in_market,
        periods_per_year=config.periods_per_year,
        risk_free_per_period=config.risk_free_per_period,
        n_trials=config.n_trials, costs_paid=costs_paid,
        starting_equity=config.starting_equity)
    envelope = build_envelope(
        equity=equity, metrics=metrics,
        losing_streak=int(metrics.get("max_consecutive_losses", 0)))
    return BacktestResult(
        equity=equity, trades=trades, fills=fills, metrics=metrics,
        bars_in_market=bars_in_market, total_bars=len(series),
        starting_equity=config.starting_equity, n_trials=config.n_trials,
        envelope=envelope, warnings=warnings)
