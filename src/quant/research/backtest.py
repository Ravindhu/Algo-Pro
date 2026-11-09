"""Event-driven daily backtest. Targets set at a close fill at the next bar's open.

Flow stage: Historical bars, Replay simulator
Status: PHASE 1 IMPLEMENTED (portfolio replay) + PHASE 5 IMPLEMENTED (single-symbol wrap)
Existing code to reuse or wrap (do not copy): Blue_engine/backtest/engine.py (single-symbol OHLCV
simulator, wrapped below), Blue_engine/backtest/costs.py (via research/costs.py, wrapped).
The PORTFOLIO replay (``run_backtest``) is kept local on purpose: Blue's engine is single-symbol.
See docs/decisions/0002-portfolio-backtest-kept-local.md.

TWO SIMULATORS, ONE RULE EACH
-----------------------------
- ``run_backtest`` (local): many symbols, target quantities, portfolio equity. Used for
  research on a basket and for the replay simulator.
- ``run_symbol_backtest`` (wrap): ONE symbol, a target-exposure signal, MAE/MFE, envelope and
  every fill with its cost terms. This is the engine the optimiser's objective scores, so using
  it here is what keeps research and optimisation on the same fill convention.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from quant.core.blue_bridge import BlueEngineUnavailable, load
from quant.research.costs import CostModel


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: pd.DataFrame
    total_cost: float
    missed_fills: int


def run_backtest(
    bars: pd.DataFrame,
    targets: pd.DataFrame,
    cost: Optional[CostModel] = None,
    capital: float = 1_000_000.0,
    product: str = "delivery",
) -> BacktestResult:
    """bars: date, symbol, open, close. targets: date, symbol, target_qty (position set at that close).

    No look-ahead: a target set at close(t) can only fill at open(t+1). A fill with no bar that day is
    counted in missed_fills and retried on the next bar, since the target still holds.
    """
    cost = cost or CostModel()
    open_px = bars.pivot(index="date", columns="symbol", values="open").sort_index()
    close_px = bars.pivot(index="date", columns="symbol", values="close").reindex(open_px.index)
    tgt = (
        targets.pivot(index="date", columns="symbol", values="target_qty")
        .reindex(index=open_px.index, columns=open_px.columns)
        .ffill()
        .fillna(0.0)
    )
    dates = list(open_px.index)
    cols = list(open_px.columns)
    pos = pd.Series(0.0, index=cols)
    cash = float(capital)
    equity, trades = [], []
    total_cost, missed = 0.0, 0
    for i, d in enumerate(dates):
        if i > 0:
            want = tgt.loc[dates[i - 1]]
            delta = want - pos
            for sym in [c for c in cols if delta[c] != 0]:
                px = open_px.at[d, sym]
                if pd.isna(px):
                    missed += 1
                    continue
                q = float(delta[sym])
                side = "BUY" if q > 0 else "SELL"
                c = cost.trade_cost(abs(q) * px, side, product)
                cash -= q * px + c
                pos[sym] += q
                total_cost += c
                trades.append({"date": d, "symbol": sym, "qty": q, "price": px, "cost": c})
        mtm = float((pos * close_px.loc[d].fillna(0.0)).sum())
        equity.append(cash + mtm)
    return BacktestResult(
        equity=pd.Series(equity, index=dates, name="equity"),
        trades=pd.DataFrame(trades, columns=["date", "symbol", "qty", "price", "cost"]),
        total_cost=total_cost,
        missed_fills=missed,
    )


# --------------------------------------------------------------------------- #
# Single-symbol wrap of Blue_engine/backtest/engine.py
# --------------------------------------------------------------------------- #
class BacktestUnavailable(RuntimeError):
    """Blue's single-symbol backtest cannot be loaded."""


def _blue_backtest():
    try:
        return load("backtest.engine")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise BacktestUnavailable(f"Blue backtest engine unavailable: {exc}") from exc


def run_symbol_backtest(rows: Sequence[Dict[str, Any]] | pd.DataFrame,
                        strategy: Any, params: Dict[str, Any], *,
                        symbol: str = "",
                        exchange: str = "NSE",
                        interval: str = "1d",
                        starting_equity: float = 100_000.0,
                        max_position_fraction: float = 1.0,
                        allow_short: Optional[bool] = None,
                        stop_loss_fraction: Optional[float] = None,
                        max_daily_loss_fraction: Optional[float] = None,
                        n_trials: int = 1) -> Dict[str, Any]:
    """One-symbol OHLCV simulation. The signal returns a target exposure, not a quantity.

    ``rows`` is a list of bar dicts (open/high/low/close/volume/ts) or a DataFrame
    with those columns. Bars are handed to Blue unchanged: it repairs or refuses
    them itself, and every repair it makes comes back in ``warnings``.

    LONG-ONLY (owner, 2026-11-09): this is an investment algo, BUY and ADD, never
    short. ``allow_short`` defaults to False and cannot be turned on: pass
    ``allow_short=True`` and this raises ``ValueError`` instead of scoring a short
    book that neither the POV planner nor the order path would send. A research
    run that silently studies a strategy production cannot execute is the worst
    kind of backtest.

    Raises ``ValueError`` for a bad input and ``BacktestUnavailable`` when Blue is
    absent. It does NOT swallow a bad signal: a strategy that returns an exposure
    outside ``[-max_position_fraction, max_position_fraction]`` is a bug in the
    strategy, and a backtest that hides it would be worse than no backtest.
    """
    if allow_short:
        raise ValueError("long-only: shorting is disabled for this investment algo")
    module = _blue_backtest()
    if isinstance(rows, pd.DataFrame):
        bars = rows.reset_index().to_dict("records")
    elif isinstance(rows, Sequence):
        bars = list(rows)
    else:
        raise ValueError("rows must be a DataFrame or a list of bar dicts")
    if len(bars) < 2:
        raise ValueError("a backtest needs at least two bars")
    if not callable(strategy):
        raise ValueError("strategy must be callable")
    if n_trials < 1:
        raise ValueError("n_trials must be at least 1 (zero would neuter the DSR)")

    from quant.research.costs import blue_cost_model  # local: avoids a module cycle
    config = module.BacktestConfig(
        cost_model=blue_cost_model(),
        starting_equity=float(starting_equity),
        n_trials=int(n_trials),
        max_position_fraction=float(max_position_fraction),
        allow_short=False,          # long-only: never enabled for this book
        stop_loss_fraction=stop_loss_fraction,
        max_daily_loss_fraction=max_daily_loss_fraction,
    )
    result = module.run_backtest(bars, strategy, dict(params), config=config,
                                 symbol=symbol, exchange=exchange, interval=interval)
    return {
        "equity": list(getattr(result, "equity", []) or []),
        "trades": list(getattr(result, "trades", []) or []),
        "fills": [getattr(f, "__dict__", f) for f in (getattr(result, "fills", []) or [])],
        "metrics": dict(getattr(result, "metrics", {}) or {}),
        "bars_in_market": int(getattr(result, "bars_in_market", 0) or 0),
        "total_bars": int(getattr(result, "total_bars", 0) or 0),
        "envelope": dict(getattr(result, "envelope", {}) or {}),
        "warnings": list(getattr(result, "warnings", []) or []),
        "version": str(getattr(result, "version", "")),
        "n_trials": int(getattr(result, "n_trials", n_trials) or n_trials),
    }
