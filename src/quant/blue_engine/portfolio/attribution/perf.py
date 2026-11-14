"""
attribution/perf.py - performance statistics (pure).

The efficiency scorecard a PM reports every period. All from an equity curve or a
return series, dependency-free.

Included
--------
Sharpe, Sortino, Calmar, information ratio, tracking error, max drawdown,
annualised return/vol, hit rate, payoff ratio, up/down capture, rolling
Sharpe, XIRR (money-weighted / actual investor return) and TWRR (time-weighted /
manager return, the fair benchmark-relative number). ``summarise`` bundles them
for the dashboard.

XIRR vs TWRR - which to report
------------------------------
* **XIRR** answers *"what did MY money actually earn?"* It weights each period by
the capital that was at risk, so a well-timed deposit flatters it and a
poorly-timed one hurts it. This is the number a client / SEBI statement shows.
* **TWRR** answers *"how good was the manager?"* It neutralises the size and
timing of external cash flows and chains the sub-period returns, so it is the
only fair number to compare against NIFTY. Report TWRR against the benchmark;
report XIRR as the realised experience.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ...core.results import mean, safe_float, stdev

logger = logging.getLogger(__name__)

__all__ = [
    "sharpe", "sortino", "calmar", "information_ratio", "tracking_error",
    "annualised_return", "annualised_vol", "max_drawdown", "hit_rate",
    "payoff_ratio", "up_down_capture", "rolling_sharpe", "summarise",
    "xirr", "twrr",
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
    """Compound-return Sharpe: annualised (compounded) return minus rf, over annualised vol.

    NOTE: this is a DIFFERENT metric from ``ml.evaluation.metrics.sharpe``, which is
    the arithmetic mean excess return over its standard deviation, both annualised.
    The project standard is the arithmetic version (decision 0004). Do not compare
    numbers from the two without saying which one was used.
    """
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


# --------------------------------------------------------------------------- #
# Cash-flow-aware returns: XIRR (money-weighted) and TWRR (time-weighted)
# --------------------------------------------------------------------------- #

DateLike = Union[str, date, datetime]


def _to_date(d: DateLike) -> Optional[date]:
    """Coerce a date-ish value to ``datetime.date``; ``None`` if unparseable.

    Accepts ``datetime``/``date`` directly and ISO strings (``YYYY-MM-DD`` or a
    full ISO timestamp; a trailing ``Z`` is tolerated). Anything else -> None so
    a single bad row degrades one calculation rather than aborting the report.
    """
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        s = d.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s).date()
        except ValueError:
            try:
                return date.fromisoformat(s[:10])
            except ValueError:
                return None
    return None


def _as_flow(row: Any) -> Tuple[Optional[date], Optional[float]]:
    """Normalise one cash flow to ``(date, amount)``.

    Accepts a ``(date, amount)`` pair/tuple/list, or a mapping with keys
    ``date``/``amount``. Convention: an investment (money INTO the portfolio) is
    NEGATIVE, a withdrawal or terminal value is POSITIVE. Sign is the caller's
    contract - we only require it to be internally consistent.
    """
    d: Any = None
    a: Any = None
    if isinstance(row, (tuple, list)) and len(row) >= 2:
        d, a = row[0], row[1]
    elif isinstance(row, dict):
        d = row.get("date")
        a = row.get("amount", row.get("value"))
    dd = _to_date(d) if d is not None else None
    aa = safe_float(a)
    return dd, aa


def xirr(cash_flows: Sequence[Any], *, guess: float = 0.1,
         max_iter: int = 100, tol: float = 1e-9) -> Optional[float]:
    """Extended IRR (money-weighted / actual investor return).

    Solves ``sum(amount_i / (1 + r) ** ((date_i - date_0) / 365)) == 0`` for the
    annual rate ``r`` - the discount rate that sets the present value of all
dated cash flows to zero. This is the return an investor actually experienced:
    it accounts for the SIZE and TIMING of every deposit and withdrawal, unlike a
    simple return series.

    Parameters
    ----------
    cash_flows : sequence of ``(date, amount)`` pairs, or dicts with keys
        ``date`` / ``amount``. Date may be a ``date``/``datetime`` or an ISO
        string. Investments are negative, withdrawals/terminal value positive.
    guess : starting rate for the Newton iteration (0.1 = 10%).

    Returns
    -------
    The annualised rate as a fraction (``0.15`` == 15%), or ``None`` when the
    problem is under-determined (< 2 valid flows, all same sign) or the solver
    fails to converge.

    Notes
    -----
    Newton-Raphson with a bracketing bisection fallback. Newton alone can wander
    on real account histories (long gaps, a final flow far from the first), so we
    fall back to bisection over a wide bracket when it does. This is the same
    approach Excel's ``XIRR`` uses.
    """
    flows: List[Tuple[date, float]] = []
    for row in (cash_flows or []):
        d, a = _as_flow(row)
        if d is not None and a is not None and a != 0.0:
            flows.append((d, a))
    if len(flows) < 2:
        return None
    flows.sort(key=lambda f: f[0])
    # Need at least one positive and one negative flow, else no finite root.
    if not (any(a > 0 for _, a in flows) and any(a < 0 for _, a in flows)):
        return None

    t0 = flows[0][0]
    # (years_from_start, amount) - precomputed once, reused by every iteration.
    ts = [((d - t0).days / 365.0, a) for d, a in flows]

    def npv(r: float) -> float:
        return sum(a / (1.0 + r) ** t for t, a in ts)

    def dnpv(r: float) -> float:
        return sum(-t * a / (1.0 + r) ** (t + 1.0) for t, a in ts)

    # ---- Newton-Raphson ----
    r = guess
    for _ in range(max_iter):
        if r <= -1.0:  # rate below -100% is meaningless; bail to bisection
            break
        try:
            f = npv(r)
            df = dnpv(r)
        except (OverflowError, ZeroDivisionError):
            break
        if abs(f) < tol:
            return r
        if df == 0 or not math.isfinite(df):
            break
        step = f / df
        r_new = r - step
        if not math.isfinite(r_new):
            break
        if abs(r_new - r) < tol:
            return r_new
        r = r_new

    # ---- Bisection fallback over a wide bracket ----
    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        # No sign change in the bracket -> no bracketed root; report failure.
        logger.warning("xirr: no sign change in bracket [%.4f, %.4f]; "
                       "returning None", lo, hi)
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < tol or (hi - lo) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def twrr(flows: Sequence[Any], *, periods_per_year: float = 252.0,
         start_value: Optional[float] = None,
         end_value: Optional[float] = None) -> Optional[float]:
    """Time-weighted rate of return (the manager's return).

    Chains sub-period returns so that external cash flows do not distort the
    number. Each sub-period is bounded by a cash-flow date; the return over a
    sub-period is ``(end_value - flow_at_end) / start_value - 1`` where the flow
    at the boundary is excluded from the numerator (it arrived at the end, so it
    did not earn anything during the sub-period). The sub-period returns are then
    geometrically compounded.

    Inputs
    ------
    flows : sequence of valuation points, each a dict (or ``(date, value)`` pair)
        carrying the portfolio VALUE observed on that date and, optionally, the
        net external cash flow on that date:

            {"date": "2026-01-01", "value": 1_000_000, "flow": -100_000}

        ``flow`` is the net external cash flow that day using the CONTRIBUTION
        convention: POSITIVE = money paid INTO the portfolio (a deposit),
        NEGATIVE = money taken OUT (a withdrawal). It defaults to 0. ``value``
        is the mark-to-market NAV INCLUDING any flow that day. The first point is
        the start of the measurement window, the last is the end.
    start_value / end_value : optional overrides for the first/last valuation,
        for callers that pass flows without values.

    Returns
    -------
    Cumulative time-weighted return as a fraction over the whole window
    (``0.15`` == 15% for the window), or ``None`` if there are fewer than two
    usable valuations.

    Notes
    -----
    This returns the CUMULATIVE window return, not an annualised figure - annualise
    with the window length if you need a per-year number, or use :func:`xirr` for
    the money-weighted annualised figure. Keeping the raw chain-link is the
    standard GIPS presentation.
    """
    pts: List[Tuple[date, float, float]] = []
    for row in (flows or []):
        d: Any = None
        v: Any = None
        f: Any = 0.0
        if isinstance(row, (tuple, list)) and len(row) >= 2:
            d, v = row[0], row[1]
            f = row[2] if len(row) >= 3 else 0.0
        elif isinstance(row, dict):
            d = row.get("date")
            v = row.get("value", row.get("nav"))
            f = row.get("flow", row.get("cash_flow", 0.0))
        dd = _to_date(d) if d is not None else None
        vv = safe_float(v)
        ff = safe_float(f)
        if dd is not None and vv is not None:
            pts.append((dd, vv, ff if ff is not None else 0.0))
    if len(pts) < 2:
        return None
    pts.sort(key=lambda p: p[0])

    # Optional overrides: only replace if the caller supplied a real value.
    if start_value is not None:
        d0, _, f0 = pts[0]
        pts[0] = (d0, float(start_value), f0)
    if end_value is not None:
        dn, _, fn = pts[-1]
        pts[-1] = (dn, float(end_value), fn)

    chain = 1.0
    for i in range(1, len(pts)):
        v_prev = pts[i - 1][1]
        v_cur, flow_cur = pts[i][1], pts[i][2]
        if v_prev <= 0:
            # A zero/negative prior value cannot support a ratio; skip the leg.
            logger.warning("twrr: non-positive prior value at leg %d; skipping",
                           i)
            continue
        # Exclude the flow that arrived AT the end of this leg - it earned
        # nothing during the leg.
        sub_ret = (v_cur - flow_cur) / v_prev - 1.0
        if not math.isfinite(sub_ret):
            continue
        chain *= (1.0 + sub_ret)
    return chain - 1.0


def summarise(returns: Sequence[float], *, benchmark: Optional[Sequence[float]]
              = None, rf: float = 0.0, periods: float = 252.0,
              cash_flows: Optional[Sequence[Any]] = None,
              valuations: Optional[Sequence[Any]] = None,
              ) -> Dict[str, Any]:
    """Full performance scorecard (the metrics the Efficiency tab shows).

    When ``cash_flows`` is supplied, ``xirr`` (money-weighted / actual investor
    return) is added. When ``valuations`` is supplied, ``twrr`` (time-weighted /
    manager return) is added. Both are optional so existing callers are
    unaffected.
    """
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
    if cash_flows is not None:
        out["xirr"] = _r(xirr(cash_flows))
    if valuations is not None:
        out["twrr"] = _r(twrr(valuations, periods_per_year=periods))
    return out


def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 6)