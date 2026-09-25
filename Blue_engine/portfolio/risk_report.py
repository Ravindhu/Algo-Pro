"""
portfolio/risk_report.py — Phase 2: the risk & performance report.

What this is
------------
Phase 1 produced an equity curve. This module turns that curve into the one
report a PM actually reads every morning: how much did we make, how much risk
did we take to make it, and what is the worst thing that can happen tomorrow.

It is deliberately an **assembler, not a reimplementation**: the maths already
lives in ``attribution/perf.py`` (Sharpe/Sortino/Calmar/IR/capture) and
``risk/tail.py`` (VaR/CVaR/stress). Duplicating those formulas would give the
desk two answers to the same question, which is exactly how P&L disputes start.

What is genuinely new here
--------------------------
* **Underwater series** — the drawdown path, for the dashboard's underwater plot.
  ``tail.drawdown_series`` exists but returns a bare list; we pair it with dates
  and add recovery statistics.
* **Drawdown duration & recovery** — *how long* we were under water, and whether
  we are still under water. Sharpe says nothing about this; a strategy that
  takes 3 years to recover a 20% hole is not the same as one that recovers in
  3 months, even at identical Sharpe.
* **Rolling series** — rolling vol and Sharpe at a chosen window, so the
  dashboard can show whether the risk profile is drifting.
* **One report dict** — everything the Risk panel needs, in a single call.

India-specific notes
--------------------
* Returns are **daily** and annualised at 252 (NSE trades ~250 sessions/yr).
* VaR/CVaR use the Cornish-Fisher correction by default because Indian equity
  returns are sharply negatively skewed and fat-tailed; the Gaussian estimate
  under-states tail risk by a wide margin on NSE small/mid-caps.
* Stress scenarios in ``risk/tail.DEFAULT_STRESS`` are India-relevant
  (GFC, taper tantrum, demonetisation, COVID, a VIX spike) rather than generic.

Everything is pure: feed it returns; get dicts. No I/O, no network.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from ..core.results import safe_float
from . import attribution as _attr_pkg
from . import risk as _risk_pkg

logger = logging.getLogger(__name__)

__all__ = ["underwater", "drawdown_stats", "rolling_series",
           "performance_report", "risk_report", "report"]


# --------------------------------------------------------------------------- #
# Drawdown analytics (the genuinely new part)
# --------------------------------------------------------------------------- #

def underwater(returns: Optional[Sequence[float]],
               dates: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """The underwater curve: running drawdown from the high-water mark.

    Returns ``{"dates", "drawdown", "peak", "equity"}`` where ``drawdown`` is
    **negative** (0 at a new high, -0.10 at 10% below the peak) so it plots
    naturally as a falling area chart.

    ``dates`` is passed through for the x-axis; when omitted we emit indices so
    the function still works for a synthetic/simulated series in a test.
    """
    rs = [safe_float(r) for r in (returns or [])]
    rs = [r for r in rs if r is not None]
    if not rs:
        return {"dates": [], "drawdown": [], "peak": [], "equity": []}

    d = list(dates) if dates is not None else list(range(len(rs)))
    if len(d) > len(rs):
        # An extra leading date (the t0 NAV) is common; align on the tail.
        d = d[-len(rs):]

    equity = 1.0
    peak = 1.0
    eq_path: List[float] = []
    pk_path: List[float] = []
    dd_path: List[float] = []
    for r in rs:
        equity *= (1.0 + r)
        peak = max(peak, equity)
        eq_path.append(round(equity, 8))
        pk_path.append(round(peak, 8))
        dd_path.append(round((equity / peak - 1.0) if peak > 0 else 0.0, 8))
    return {"dates": d, "drawdown": dd_path, "peak": pk_path,
            "equity": eq_path}


def drawdown_stats(returns: Optional[Sequence[float]]) -> Dict[str, Any]:
    """Max drawdown plus its duration and recovery state.

    Definitions used here (documented because desks disagree):

    * **max_drawdown** — the deepest peak-to-trough fall, as a positive fraction.
    * **peak_index / trough_index** — positions in the return series.
    * **duration** — number of observations from peak to trough (how long the
      fall took).
    * **underwater_length** — observations from peak until the equity curve
      regains that peak. ``None`` when still under water at the end.
    * **recovered** — whether the last observation is back at/above the peak.
    * **current_drawdown** — the drawdown as of the last observation (<= 0).

    A strategy with a modest max drawdown but a *long* underwater length is a
    far harder hold in practice than the single number suggests.
    """
    rs = [safe_float(r) for r in (returns or [])]
    rs = [r for r in rs if r is not None]
    if not rs:
        return {"max_drawdown": None, "peak_index": None, "trough_index": None,
                "duration": None, "underwater_length": None,
                "recovered": None, "current_drawdown": None}

    equity = 1.0
    peak = 1.0
    peak_idx = 0
    worst = 0.0
    worst_peak = 0
    worst_trough = 0
    worst_peak_value = 1.0
    peak_values: List[float] = []      # high-water mark AT each step
    for i, r in enumerate(rs):
        equity *= (1.0 + r)
        if equity > peak:
            peak = equity
            peak_idx = i
        peak_values.append(peak)
        dd = (equity / peak - 1.0) if peak > 0 else 0.0
        if dd < worst:
            worst = dd
            worst_peak = peak_idx
            worst_trough = i
            worst_peak_value = peak

    # Recovery: does the curve ever regain ``worst_peak_value``?
    #
    # We must compare against the ACTUAL peak VALUE, not a value rebuilt by
    # replaying returns from 1.0. Rebuilding breaks whenever the peak itself is
    # not at index 0 — e.g. +50% then -10%: the true peak is 1.50, but a replay
    # from rs[:0] gives 1.00, so the curve looks "recovered" the moment it climbs
    # back above 1.00 while still 12.8% under water. That contradiction is how a
    # PM gets told a hole is filled when it is not.
    #
    # Starting the scan at worst_trough is also correct: while the curve is
    # making new highs it cannot be recovering from a past trough, and at the
    # trough equity is by definition still below the peak.
    equity = 1.0
    for r in rs[:worst_trough + 1]:
        equity *= (1.0 + r)
    recovered_at: Optional[int] = None
    for i in range(worst_trough + 1, len(rs)):
        equity *= (1.0 + rs[i])
        if equity >= worst_peak_value - 1e-12:
            recovered_at = i
            break

    # Current drawdown from the all-time high water mark.
    eq = 1.0
    pk = 1.0
    for r in rs:
        eq *= (1.0 + r)
        pk = max(pk, eq)
    current = (eq / pk - 1.0) if pk > 0 else 0.0

    return {
        "max_drawdown": round(abs(worst), 8),
        "peak_index": worst_peak,
        "trough_index": worst_trough,
        "duration": worst_trough - worst_peak,
        "underwater_length": (None if recovered_at is None
                              else recovered_at - worst_peak),
        "recovered": recovered_at is not None,
        "current_drawdown": round(current, 8),
    }


def rolling_series(returns: Optional[Sequence[float]], *,
                   window: int = 60, periods: float = 252.0
                   ) -> Dict[str, Any]:
    """Rolling annualised vol and Sharpe (aligned to the input).

    The first ``window - 1`` entries are ``None`` so the series lines up 1:1 with
    the returns series and therefore with any date axis built from it. This is
    the "is our risk profile drifting?" chart — a strategy whose rolling vol has
    doubled is a different strategy from the one that was backtested.
    """
    rs = [safe_float(r) for r in (returns or [])]
    rs = [r for r in rs if r is not None]
    if not rs or window < 2:
        return {"window": window, "vol": [], "sharpe": []}

    vols: List[Optional[float]] = []
    sharpes: List[Optional[float]] = []
    for t in range(len(rs)):
        if t + 1 < window:
            vols.append(None)
            sharpes.append(None)
            continue
        seg = rs[t + 1 - window:t + 1]
        vols.append(_attr_pkg.perf.annualised_vol(seg, periods=periods))
        sharpes.append(_attr_pkg.perf.sharpe(seg, periods=periods))
    return {"window": window,
            "vol": [None if v is None else round(v, 8) for v in vols],
            "sharpe": [None if s is None else round(s, 6) for s in sharpes]}


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

def performance_report(returns: Optional[Sequence[float]], *,
                       benchmark: Optional[Sequence[float]] = None,
                       rf: float = 0.0, periods: float = 252.0
                       ) -> Dict[str, Any]:
    """Performance scorecard + drawdown analytics + rolling series.

    Wraps ``attribution.perf.summarise`` (the existing, tested maths) and adds
    the Phase-2 extras. Returns ``{"ok", "summary", "drawdown", "underwater",
    "rolling", "n", "warnings"}``.
    """
    rs = [safe_float(r) for r in (returns or [])]
    rs = [r for r in rs if r is not None]
    warnings: List[str] = []
    if len(rs) < 2:
        warnings.append(f"need >= 2 returns for a performance report, have {len(rs)}")
    base = _attr_pkg.perf.summarise(rs, benchmark=benchmark, rf=rf,
                                    periods=periods) if rs else {}
    out: Dict[str, Any] = {
        "ok": bool(rs),
        "n": len(rs),
        "summary": base,
        "drawdown": drawdown_stats(rs) if rs else {},
        "underwater": underwater(rs) if rs else underwater([]),
        "rolling": rolling_series(rs, periods=periods) if rs
        else {"window": 60, "vol": [], "sharpe": []},
        "warnings": warnings,
    }
    # A strategy that has never recorded a positive day cannot have a real
    # Sharpe; flag rather than silently reporting a number.
    if rs and all(r <= 0 for r in rs):
        out["warnings"].append("no positive days in the sample")
    return out


def risk_report(returns: Optional[Sequence[float]], *,
                capital: Optional[float] = None,
                level: float = 0.95,
                scenarios: Optional[Dict[str, Dict[str, Any]]] = None,
                beta: Optional[float] = None
                ) -> Dict[str, Any]:
    """Tail-risk report on the return series: VaR, CVaR and stress losses.

    ``capital`` (optional) converts percentages into currency, which is what a
    PM actually needs: "a 95% day costs me ₹2.4 lakh", not "2.4%".

    ``beta`` (optional) scales the named stress scenarios for the book.
    ``risk/tail.stress_test`` takes the *market* shock and multiplies by beta,
    so a beta-1.3 book feels a NIFTY crash 1.3× harder.

    Returns ``{"ok", "level", "var", "cvar", "tail", "stress", "capital",
    "amounts", "warnings"}``.
    """
    rs = [safe_float(r) for r in (returns or [])]
    rs = [r for r in rs if r is not None]
    warnings: List[str] = []
    tail_mod = _risk_pkg.tail
    cap = safe_float(capital)

    hist = tail_mod.historical_var(rs, level=level) if rs else None
    param = tail_mod.parametric_var(rs, level=level) if rs else None
    cf = tail_mod.cornish_fisher_var(rs, level=level) if rs else None
    es = tail_mod.cvar(rs, level=level) if rs else None

    if len(rs) < 30:
        warnings.append(
            f"only {len(rs)} observations: a {level:.0%} VaR is estimated from "
            f"the worst ~{max(1, int(len(rs) * (1 - level)))} day(s) and is "
            f"indicative only")
    if cf is not None and hist is not None and cf > hist * 1.5:
        warnings.append(
            "Cornish-Fisher VaR materially exceeds historical VaR: the sample "
            "has fat tails / negative skew, so trust the CF figure, not the "
            "Gaussian one")

    stress = tail_mod.stress_test(beta if beta is not None else 1.0,
                                  scenarios=scenarios)
    amounts: Dict[str, Any] = {}
    if cap:
        amounts = {
            "var_hist": None if hist is None else round(hist * cap, 2),
            "var_cf": None if cf is None else round(cf * cap, 2),
            "cvar": None if es is None else round(es * cap, 2),
            "stress": {s["scenario"]: round(s["pnl_pct"] * cap, 2)
                       for s in stress},
        }
    return {
        "ok": bool(rs),
        "n": len(rs),
        "level": level,
        "var": {"historical": hist, "parametric": param,
                "cornish_fisher": cf},
        "cvar": es,
        "tail": tail_mod.summarise(rs, levels=(0.95, 0.99)) if rs else {},
        "stress": stress,
        "capital": cap,
        "amounts": amounts,
        "warnings": warnings,
    }


def report(nav_rows: Optional[List[Dict[str, Any]]], *,
           capital: Optional[float] = None, rf: float = 0.0,
           benchmark: Optional[Sequence[float]] = None,
           level: float = 0.95, beta: Optional[float] = None,
           rolling_window: int = 60) -> Dict[str, Any]:
    """The single Phase-2 entry point: equity rows in, full report out.

    Takes exactly what ``PortfolioManager.get_equity_curve()["rows"]`` returns
    and computes the return series itself via ``tracker.daily_returns`` so the
    desk has **one** definition of a daily return.

    Capital defaults to the first NAV row when not supplied, so the currency
    figures are always populated in practice.
    """
    try:
        from . import tracker as _tracker
        rows = list(nav_rows or [])
        rets = _tracker.daily_returns(rows)
        dates = [r.get("date") for r in rows]
        # Infer the spacing of the store and annualise with it. A live store is
        # daily (~252/yr); a backfilled monthly-rebalance store is ~12/yr. Using
        # a hard-coded 252 on a monthly series overstates volatility by 4.6x.
        ppy = _tracker.periods_per_year(rows)
        cap = safe_float(capital)
        if cap is None:
            # Prefer the INCEPTION row's NAV as the capital base. Taking rows[0]
            # is wrong whenever the window clips the curve: the first visible row
            # may already reflect gains, so every metric would be measured from
            # an invisible mid-flight point rather than from committed capital.
            for r in rows:
                if r.get("inception"):
                    cap = safe_float(r.get("nav"))
                    break
        if cap is None and rows:
            cap = safe_float(rows[0].get("nav"))
        perf = performance_report(rets, benchmark=benchmark, rf=rf,
                                  periods=ppy)
        # NOTE: call the module-level function by its real name. Naming a local
        # ``risk_report`` here would shadow the module function inside this
        # module (the function IS risk_report), silently breaking every later
        # call in the process.
        risk = globals()["risk_report"](rets, capital=cap, level=level,
                                        beta=beta)
        perf["rolling"] = rolling_series(rets, window=rolling_window,
                                         periods=ppy)
        # Align the underwater x-axis with real dates (returns start at day 2).
        perf["underwater"] = underwater(rets, dates=dates[1:] if len(dates) > 1
                                       else None)
        return {
            "ok": bool(rets),
            "n_nav_rows": len(rows),
            "n_returns": len(rets),
            "periods_per_year": ppy,
            "capital": cap,
            "performance": perf,
            "risk": risk,
            "dates": dates,
            "warnings": (perf.get("warnings") or []) + (risk.get("warnings") or []),
        }
    except Exception as exc:  # noqa: BLE001 - a report must never break a page
        logger.warning("risk_report.report failed: %r", exc)
        return {"ok": False, "n_nav_rows": 0, "n_returns": 0, "capital": None,
                "performance": {}, "risk": {}, "dates": [],
                "warnings": [f"report failed: {exc}"]}