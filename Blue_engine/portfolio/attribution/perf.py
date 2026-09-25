"""
attribution/perf.py - performance statistics (pure).

The efficiency scorecard a PM reports every period. All from an equity curve or a
return series, dependency-free.

Included
--------
Sharpe, Sortino, Calmar, information ratio, tracking error, max drawdown,
annualised return/vol, hit rate, payoff ratio, up/down capture, and rolling
Sharpe. ``summarise`` bundles them for the dashboard.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence

from ...core.results import mean, safe_float, stdev

logger = logging.getLogger(__name__)

__all__ = [
    "sharpe", "sortino", "calmar", "information_ratio", "tracking_error",
    "annualised_return", "annualised_vol", "max_drawdown", "hit_rate",
    "payoff_ratio", "up_down_capture", "rolling_sharpe", "summarise",
]


def _clean(returns: Sequence[float]) -> List[float]:
    return [r for r in (safe_float(x) for x in (returns or [])) if r is not None]


def annualised_return(returns: Sequence[float], *, periods: float = 252.0
                      ) -> Optional[float]:
    rs = _clean(returns)
    if not rs:
        return None
    total = 1.0
    for r in rs:
        total *= (1.0 + r)
    if total <= 0:
        return None
    # Geometric annualisation to the number of periods observed.
    return total ** (periods / len(rs)) - 1.0


def annualised_vol(returns: Sequence[float], *, periods: float = 252.0
                   ) -> Optional[float]:
    sd = stdev(_clean(returns))
    return None if sd is None else sd * math.sqrt(periods)


def sharpe(returns: Sequence[float], *, rf: float = 0.0,
           periods: float = 252.0) -> Optional[float]:
    ar = annualised_return(returns, periods=periods)
    av = annualised_vol(returns, periods=periods)
    if ar is None or av is None or av == 0:
        return None
    return (ar - rf) / av


def sortino(returns: Sequence[float], *, rf: float = 0.0,
            periods: float = 252.0) -> Optional[float]:
    rs = _clean(returns)
    if not rs:
        return None
    ar = annualised_return(rs, periods=periods)
    if ar is None:
        return None
    rf_p = rf / periods
    downside = [(r - rf_p) ** 2 for r in rs if r < rf_p]
    if not downside:
        return None
    dd = math.sqrt(sum(downside) / len(downside)) * math.sqrt(periods)
    if dd == 0:
        return None
    return (ar - rf) / dd


def max_drawdown(returns: Sequence[float]) -> Optional[float]:
    """Max drawdown from a return series (positive fraction)."""
    rs = _clean(returns)
    if not rs:
        return None
    equity = 1.0
    peak = 1.0
    mdd = 0.0
    for r in rs:
        equity *= (1.0 + r)
        peak = max(peak, equity)
        mdd = max(mdd, (peak - equity) / peak if peak > 0 else 0.0)
    return mdd


def calmar(returns: Sequence[float], *, periods: float = 252.0
           ) -> Optional[float]:
    ar = annualised_return(returns, periods=periods)
    mdd = max_drawdown(returns)
    if ar is None or mdd is None or mdd == 0:
        return None
    return ar / mdd


def tracking_error(portfolio: Sequence[float], benchmark: Sequence[float],
                   *, periods: float = 252.0) -> Optional[float]:
    active = [p - b for p, b in zip(_clean(portfolio), _clean(benchmark))]
    av = annualised_vol(active, periods=periods)
    return av


def information_ratio(portfolio: Sequence[float], benchmark: Sequence[float],
                      *, periods: float = 252.0) -> Optional[float]:
    active = [p - b for p, b in zip(_clean(portfolio), _clean(benchmark))]
    ar = annualised_return(active, periods=periods)
    te = tracking_error(portfolio, benchmark, periods=periods)
    if ar is None or te is None or te == 0:
        return None
    return ar / te


def hit_rate(returns: Sequence[float]) -> Optional[float]:
    rs = _clean(returns)
    if not rs:
        return None
    wins = sum(1 for r in rs if r > 0)
    return wins / len(rs)


def payoff_ratio(returns: Sequence[float]) -> Optional[float]:
    rs = _clean(returns)
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r < 0]
    if not wins or not losses:
        return None
    aw = mean(wins)
    al = mean(losses)
    if aw is None or al is None or al == 0:
        return None
    return aw / al


def up_down_capture(portfolio: Sequence[float], benchmark: Sequence[float]
                    ) -> Dict[str, Optional[float]]:
    """Up/down capture ratios: portfolio participation in up/down benchmark legs."""
    pairs = [(p, b) for p, b in zip(_clean(portfolio), _clean(benchmark))]
    up_b = [p for p, b in pairs if b > 0]
    up_m = [b for p, b in pairs if b > 0]
    dn_b = [p for p, b in pairs if b < 0]
    dn_m = [b for p, b in pairs if b < 0]
    up = _capture(up_b, up_m)
    down = _capture(dn_b, dn_m)
    return {"up_capture": None if up is None else round(up, 4),
            "down_capture": None if down is None else round(down, 4)}


def _capture(portfolio_leg: List[float], bench_leg: List[float]
             ) -> Optional[float]:
    if not portfolio_leg or not bench_leg:
        return None
    pb = 1.0
    for r in portfolio_leg:
        pb *= (1 + r)
    bb = 1.0
    for r in bench_leg:
        bb *= (1 + r)
    if bb == 1.0:
        return None
    return (pb - 1.0) / (bb - 1.0) * 100.0


def rolling_sharpe(returns: Sequence[float], *, window: int = 60,
                   rf: float = 0.0, periods: float = 252.0
                   ) -> List[Optional[float]]:
    """Rolling Sharpe series (aligned to the input, ``None`` before ``window``)."""
    rs = _clean(returns)
    out: List[Optional[float]] = []
    for t in range(len(rs)):
        if t + 1 < window:
            out.append(None)
            continue
        seg = rs[t + 1 - window:t + 1]
        out.append(sharpe(seg, rf=rf, periods=periods))
    return out


def summarise(returns: Sequence[float], *, benchmark: Optional[Sequence[float]]
              = None, rf: float = 0.0, periods: float = 252.0
              ) -> Dict[str, Any]:
    """Full performance scorecard (the metrics the Efficiency tab shows)."""
    out: Dict[str, Any] = {
        "n": len(_clean(returns)),
        "annual_return": _r(annualised_return(returns, periods=periods)),
        "annual_vol": _r(annualised_vol(returns, periods=periods)),
        "sharpe": _r(sharpe(returns, rf=rf, periods=periods)),
        "sortino": _r(sortino(returns, rf=rf, periods=periods)),
        "calmar": _r(calmar(returns, periods=periods)),
        "max_drawdown": _r(max_drawdown(returns)),
        "hit_rate": _r(hit_rate(returns)),
        "payoff_ratio": _r(payoff_ratio(returns)),
    }
    if benchmark is not None:
        out["tracking_error"] = _r(tracking_error(returns, benchmark,
                                                  periods=periods))
        out["information_ratio"] = _r(information_ratio(returns, benchmark,
                                                        periods=periods))
        out.update(up_down_capture(returns, benchmark))
    return out


def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 6)