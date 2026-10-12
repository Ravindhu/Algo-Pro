"""
backtest/metrics.py - the metric layer for a completed backtest.

WHY THIS IS A THIN LAYER, NOT A SECOND METRIC LIBRARY
=====================================================
The system already measures performance in two places: ``ml.evaluation.metrics``
(for models) and ``portfolio.attribution.perf`` (for portfolios). Writing a third
independent Sharpe implementation inside the backtester would be the classic
mistake - three formulas that disagree by a rounding convention, and no way to
tell which one the dashboard's number came from.

So this module **composes**. It computes the things that are specific to a
*backtest* (the equity curve, the trade log, per-period returns derived from
fills) and delegates the actual statistics to the existing, already-tested
functions. Where a backtest-specific metric does not exist anywhere - Sortino,
Calmar, exposure, turnover, profit factor - it is added here, once, with the
reasoning written down.

WHY PURE PYTHON (no numpy/pandas)
==================================
Same house rule as ``core/results.py`` and ``ml/evaluation/metrics.py``: the
backtest layer must import on a bare ``openalgo==2.0.3`` machine, because the
backtester runs inside the optimization worker pool where optional extras are
not guaranteed. The sample sizes are a few thousand points, so vectorisation
buys nothing measurable while costing a hard dependency. Every formula below is
readable enough to check by hand, which matters more for a metric you are going
to risk capital on than the microseconds numpy would save.

WHY SORTINO AND CALMAR ARE BOTH HERE
=====================================
Sharpe punishes upside and downside volatility identically. A strategy that
makes most of its money in a few large up-days has a *low* Sharpe and an
excellent Sortino - because Sortino's denominator counts only the down-side
deviation. Conversely Calmar divides by max drawdown, so it rewards a strategy
that takes its losses smoothly over one that takes them all at once. They answer
different questions ("is the edge stable?" vs "how bad can the path get?") and a
parameter search that optimises only one of them will select for that question's
answer, not for robustness. Reporting all three, plus the objective used, is what
lets a human see whether the optimizer found a real edge or a metric artefact.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from . import logger
from ..ml.evaluation import metrics as _m
from ..portfolio.risk import tail as _tail

__all__ = ["compute_metrics", "returns_from_equity", "profit_factor",
           "sortino_ratio", "calmar_ratio", "exposure_pct", "turnover",
           "consecutive_losses"]


def returns_from_equity(equity: Sequence[Optional[float]]) -> List[float]:
    """Per-period simple returns from an equity curve.

    A return needs two points, so an n-point curve gives n-1 returns. The first
    bar therefore has no return - which is correct: on the first bar we have not
    yet held the position, so there is nothing to have earned. Padding the front
    with a 0.0 would silently add a fake observation to every statistic and
    dilute the volatility estimate by 1/n.
    """
    clean = [float(e) for e in equity if e is not None]
    out: List[float] = []
    for i in range(1, len(clean)):
        prev = clean[i - 1]
        if prev == 0:
            continue
        out.append((clean[i] - prev) / abs(prev))
    return out


def sortino_ratio(returns: Sequence[Optional[float]], *,
                  periods_per_year: float = 252.0,
                  risk_free: float = 0.0) -> Optional[float]:
    """Annualised Sortino ratio: excess return per unit of DOWNSIDE deviation.

    The denominator uses only returns below the risk-free target. Two design
    choices worth stating, because they change the number:

    * The target is the risk-free rate, not zero. A return of +2% in a period
      where cash paid +3% is a *loss in real terms*, and Sortino should count it
      as downside. Using zero would flatter a strategy in a high-rate regime -
      which is precisely the regime India has been in.
    * The mean of the downside is subtracted in the numerator AND the deviation
      is measured about the target (not about the downside mean). This is the
      standard definition; the alternative "semi-deviation about zero" variant
      produces a different and usually larger figure. We pick one and say which,
      so the number is reproducible.
    """
    clean = [float(r) for r in returns if r is not None]
    n = len(clean)
    if n < 2:
        return None
    excess = [r - risk_free for r in clean]
    mu = sum(excess) / n
    downside = [min(0.0, e) ** 2 for e in excess]
    dd = math.sqrt(sum(downside) / n)
    if dd == 0:
        # No losing period in the sample. The ratio is mathematically undefined
        # (division by zero), NOT infinite. Returning None and letting the
        # caller see "undefined" is more honest than returning a large number
        # that would win a parameter search.
        return None
    return (mu / dd) * math.sqrt(periods_per_year)


def calmar_ratio(total_return: float, max_dd: Optional[float], *,
                 periods_per_year: float = 252.0,
                 n_periods: int = 0) -> Optional[float]:
    """Annualised return divided by max drawdown. None when drawdown is 0/None."""
    if max_dd is None or max_dd <= 0:
        return None
    if n_periods <= 0:
        return None
    years = n_periods / periods_per_year
    if years <= 0:
        return None
    try:
        annual = (1.0 + total_return) ** (1.0 / years) - 1.0
    except (ValueError, OverflowError):
        return None
    return annual / max_dd


def profit_factor(gross_profit: float, gross_loss: float) -> Optional[float]:
    """Total winning rupees / total losing rupees. None when there are no losses."""
    if gross_loss == 0:
        return None
    return gross_profit / abs(gross_loss)


def consecutive_losses(pnls: Sequence[float]) -> int:
    """Longest run of consecutive losing trades. Feeds Loop B's tripwire."""
    worst = run = 0
    for p in pnls:
        if p < 0:
            run += 1
            worst = max(worst, run)
        else:
            run = 0
    return worst


def consecutive_wins(pnls: Sequence[float]) -> int:
    """Longest run of consecutive winning trades."""
    best = run = 0
    for p in pnls:
        if p > 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def exposure_pct(bars_in_market: int, total_bars: int) -> Optional[float]:
    """Fraction of bars holding a position. A high-Sharpe/low-exposure combo is
    usually a few lucky trades, not a strategy."""
    if total_bars <= 0:
        return None
    return bars_in_market / total_bars


def turnover(*, n_round_trips: int, total_bars: int) -> Optional[float]:
    """Round trips per bar. The quantity that decides whether costs matter."""
    if total_bars <= 0:
        return None
    return n_round_trips / total_bars


def compute_metrics(*, equity: Sequence[Optional[float]],
                    trades: Sequence[Dict[str, Any]],
                    total_bars: int,
                    bars_in_market: int,
                    periods_per_year: float = 252.0,
                    risk_free_per_period: float = 0.0,
                    n_trials: int = 1,
                    costs_paid: float = 0.0,
                    starting_equity: float = 0.0) -> Dict[str, Any]:
    """The full metric block for one backtest. Delegates to the shared metrics.

    Args:
        equity: the equity curve, oldest first. Its length must equal the number
            of bars + 1 (the mark at bar 0's close is the starting equity) for the
            per-period returns to line up with the bar calendar; the function does
            not enforce this because some callers pass an already-trimmed curve,
            but a mismatch shows up as a deflated Sharpe rather than an error, so
            the engine constructs the curve carefully.
        trades: the trade log. Each entry needs at least ``pnl``.
        n_trials: how many parameter sets were evaluated to produce this result.
            **Passed straight into deflated_sharpe.** If this backtest is the
            single OOS confirmation run after a search, the honest value is the
            size of that search - not 1. This is the parameter most often got
            wrong, and it is why the optimizer's trial count travels with the
            parameters into the validation run.

    Returns a flat dict, JSON-serialisable, for the report and the store.
    """
    rets = returns_from_equity(equity)
    n_per = len(rets)
    total_return = 0.0
    if equity and len(equity) >= 2 and equity[0]:
        total_return = (float(equity[-1]) - float(equity[0])) / abs(float(equity[0]))

    pnls = [float(t.get("pnl", 0.0)) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_pnls = [float(t.get("gross_pnl", t.get("pnl", 0.0)))
                  for t in trades]
    gross_profit = sum(p for p in gross_pnls if p > 0)
    gross_loss = sum(p for p in gross_pnls if p < 0)

    sharpe_v = _m.sharpe(rets, periods_per_year=periods_per_year,
                         risk_free=risk_free_per_period)
    sortino_v = sortino_ratio(rets, periods_per_year=periods_per_year,
                              risk_free=risk_free_per_period)
    # Reuse the portfolio risk module's drawdown for the equity curve so the
    # number the backtest reports and the number the risk engine reports are
    # computed the same way.
    mdd = _tail.max_drawdown([float(e) for e in equity]) if equity else None
    calmar_v = calmar_ratio(total_return, mdd, periods_per_year=periods_per_year,
                            n_periods=n_per)

    # Deflated Sharpe needs the per-period Sharpe, NOT the annualised one.
    ds = _m.deflated_sharpe(
        _m.sharpe(rets, periods_per_year=1.0, risk_free=risk_free_per_period) or 0.0,
        n_trials=max(1, int(n_trials)), n_obs=max(2, n_per)) if n_per >= 2 else {}

    metrics: Dict[str, Any] = {
        "total_return_pct": round(total_return * 100.0, 4),
        "net_pnl": round(sum(pnls), 4),
        "costs_paid": round(float(costs_paid), 4),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "profit_factor": _round(profit_factor(gross_profit, gross_loss)),
        "n_trades": len(trades),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate": _round((len(wins) / len(trades)) if trades else None),
        "hit_rate": _round(_m.hit_rate(rets)),
        "sharpe": _round(sharpe_v),
        "sortino": _round(sortino_v),
        "calmar": _round(calmar_v),
        "max_drawdown_pct": _round(None if mdd is None else mdd * 100.0),
        "max_consecutive_losses": consecutive_losses(pnls),
        "max_consecutive_wins": consecutive_wins(pnls),
        "exposure_pct": _round(exposure_pct(bars_in_market, total_bars)),
        "turnover": _round(turnover(n_round_trips=len(trades), total_bars=total_bars)),
        "avg_trade_pnl": _round(sum(pnls) / len(pnls) if pnls else None),
        "best_trade": _round(max(pnls) if pnls else None),
        "worst_trade": _round(min(pnls) if pnls else None),
        "n_bars": int(total_bars),
        "periods_per_year": float(periods_per_year),
        "deflated_sharpe": _round(ds.get("deflated_sharpe")),
        "deflated_prob_real": _round(ds.get("prob_real")),
        "n_trials": int(n_trials),
        "starting_equity": round(float(starting_equity), 4),
        "ending_equity": _round(equity[-1] if equity else None),
    }
    return metrics


def _round(v: Optional[float], nd: int = 4) -> Optional[float]:
    """Round for display, preserving None. Never let a rounding step turn an
    "undefined" metric into a plausible-looking number."""
    if v is None:
        return None
    try:
        value = float(v)
    except (TypeError, ValueError):
        return None
    return round(value, nd) if math.isfinite(value) else None
