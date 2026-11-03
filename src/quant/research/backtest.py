"""Event-driven daily backtest. Targets set at a close fill at the next bar's open.

Flow stage: Historical bars, Replay simulator
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/backtest/costs.py (via research/costs.py, wrapped).
The portfolio replay is kept local on purpose. Blue_engine/backtest/engine.py is single-symbol.
See docs/decisions/0002-portfolio-backtest-kept-local.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

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
