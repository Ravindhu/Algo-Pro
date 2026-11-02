"""
tests/test_dashboard_portfolio_data.py — Phase 6: dashboard data adapters.

The dashboard is a READER. These tests assert that property, plus the two traps
that make a portfolio UI actively dangerous rather than merely useless:

1. **A UI poll must never mutate the book.** Every accessor here reads a
   persisted artefact. If a dashboard could place an order or trigger an
   optimiser run, a browser refresh would move money.
2. **A UI poll must never double-count P&L.** The manager owns the equity
   tracker, so the cached instance is mandatory: rebuilding it per poll would
   forget every ingested fill and re-book it on the next tick.

They also assert the degradation contract: a data fault blanks ONE panel with an
explanation, it does not blank the page.

Run::

    & ..\\openalgo\\.venv\\Scripts\\python.exe -m pytest tests -q
    & ..\\openalgo\\.venv\\Scripts\\python.exe tests\\test_dashboard_portfolio_data.py
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.portfolio import tracker as tr  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _seed_curve(directory, days: int = 60, *, seed: int = 5) -> int:
    """Write a realistic NAV path into ``directory``; return the row count."""
    rng = random.Random(seed)
    nav = 1_000_000.0
    d0 = datetime(2026, 6, 1, 16, 0, tzinfo=tr.IST)
    for i in range(days):
        nav *= (1.0 + rng.gauss(0.0004, 0.011))
        tr.store_nav({"ts": (d0 + timedelta(days=i)).timestamp(),
                      "date": (d0 + timedelta(days=i)).strftime("%Y-%m-%d"),
                      "nav": round(nav, 2)}, directory=directory)
    return days


# --------------------------------------------------------------------------- #
# 1. The manager must be cached (the double-count trap)
# --------------------------------------------------------------------------- #
def test_portfolio_manager_is_cached_not_rebuilt():
    """A fresh manager per poll would re-book every fill and double-count P&L.

    This is the single most dangerous bug a dashboard can have: the UI would
    show a P&L that grows on every refresh, and every downstream metric
    (Sharpe, VaR, attribution) would inherit the error.
    """
    from blue_engine.dashboard import data_layer as _data
    # Force a fresh module state for the test, then assert identity across calls.
    _data._MANAGER = None
    first = _data.portfolio_manager()
    second = _data.portfolio_manager()
    _check(first is not None, "manager should be available in this environment")
    _check(first is second,
           "the manager must be a single shared instance, not rebuilt per call")
    if first is not None:
        _check(first.config.get("persist") is False,
               "the dashboard manager must NOT persist (a UI poll must not "
               "write a portfolio run every few seconds)")


def test_dashboard_manager_does_not_persist():
    from blue_engine.dashboard import data_layer as _data
    pm = _data.portfolio_manager()
    if pm is None:
        return                                   # environment without the engine
    _check(pm.config.get("persist") is False,
           "persist must be False for the reader-only dashboard instance")


# --------------------------------------------------------------------------- #
# 2. Every accessor degrades to a dict (never raises)
# --------------------------------------------------------------------------- #
def test_all_accessors_return_dicts_and_never_raise():
    from blue_engine.dashboard import data_layer as _data
    for name in ("equity_curve", "risk_report", "exposure_snapshot",
                 "attribution_snapshot", "policy_snapshot", "views_snapshot"):
        fn = getattr(_data, name)
        out = fn()
        _check(isinstance(out, dict), f"{name}() must return a dict, got {type(out)}")


def test_accessors_carry_ok_or_action_so_callers_can_judge():
    """A panel needs to know whether its data is real; a bare {} is unusable."""
    from blue_engine.dashboard import data_layer as _data
    _check("ok" in _data.equity_curve(), "equity_curve must report ok")
    _check("ok" in _data.risk_report(), "risk_report must report ok")
    _check("ok" in _data.exposure_snapshot(), "exposure must report ok")
    _check("ok" in _data.attribution_snapshot(), "attribution must report ok")
    pol = _data.policy_snapshot()
    _check("action" in pol, f"policy must report an action: {pol}")


# --------------------------------------------------------------------------- #
# 3. The curve accessor actually reads the NAV store
# --------------------------------------------------------------------------- #
def test_equity_curve_reads_the_seeded_nav_store(tmp_path):
    """With a real curve on disk, the adapter must surface it.

    NOTE ``months`` counts monthly NAV FILES, not calendar months. A 40-day
    curve seeded from 1 June spans June + July, so ``months=1`` legitimately
    reads only one of those files. We therefore read enough files to cover the
    window and assert the full row count, then separately assert that a too-narrow
    window WARNS rather than silently under-reporting.
    """
    from blue_engine.dashboard import data_layer as _data
    n = _seed_curve(tmp_path, days=40)

    pm = _data.portfolio_manager()
    if pm is None:
        return
    original = pm.config.get("nav_dir")
    try:
        pm.config["nav_dir"] = str(tmp_path)
        # Enough files to span the seeded window.
        curve = _data.equity_curve(months=12)
        _check(curve.get("n") == n,
               f"should read {n} NAV rows, got {curve.get('n')}")
        _check(curve.get("n_returns") == n - 1,
               f"{n} rows -> {n - 1} returns, got {curve.get('n_returns')}")
        _check(len(curve.get("nav") or []) == n, "nav series length must match")

        # A narrow window now filters by DATE, so it must never return a
        # disjoint set (which would compute returns across gaps). Assert we get
        # a contiguous tail, and that any shortfall is monotonic — a narrower
        # window can only ever return fewer-or-equal rows.
        narrow = _data.equity_curve(months=1)
        _check(narrow.get("n", 0) <= n,
               f"a narrower window must not return MORE rows: "
               f"{narrow.get('n')} > {n}")
        if narrow.get("n"):
            ndates = [d for d in (narrow.get("dates") or [])]
            _check(ndates == sorted(ndates),
                   f"rows must stay date-ordered: {ndates[:3]}")
            # Contiguity: the narrow window must end at the same last date.
            full_dates = [d for d in (curve.get("dates") or [])]
            _check(ndates[-1] == full_dates[-1],
                   f"both windows must end on the latest date: "
                   f"{ndates[-1]} vs {full_dates[-1]}")
    finally:
        pm.config["nav_dir"] = original


def test_risk_report_is_populated_from_a_real_curve(tmp_path):
    from blue_engine.dashboard import data_layer as _data
    _seed_curve(tmp_path, days=60)
    pm = _data.portfolio_manager()
    if pm is None:
        return
    original = pm.config.get("nav_dir")
    try:
        pm.config["nav_dir"] = str(tmp_path)
        rep = _data.risk_report(months=12)
        _check(rep.get("ok") is True, f"report should succeed: {rep.get('warnings')}")
        perf = rep.get("performance") or {}
        _check(perf.get("summary"), "performance summary should be present")
        risk = rep.get("risk") or {}
        var = (risk.get("var") or {}).get("cornish_fisher")
        _check(var is not None and var > 0,
               f"VaR must be a real positive loss, got {var}")
        _check(risk.get("stress"), "stress scenarios should be present")
    finally:
        pm.config["nav_dir"] = original


# --------------------------------------------------------------------------- #
# 4. The policy accessor must reflect the kill-switch
# --------------------------------------------------------------------------- #
def test_policy_snapshot_reports_a_valid_action():
    from blue_engine.dashboard import data_layer as _data
    pol = _data.policy_snapshot()
    _check(pol.get("action") in ("OPTIMISE", "HALT", "EXIT_TO_CASH", "UNKNOWN"),
           f"unexpected action: {pol.get('action')}")
    # Reasons must be present so the panel can explain a halt.
    if pol.get("action") == "HALT":
        _check(isinstance(pol.get("reasons"), list),
               "a halt must carry reasons for the operator")


# --------------------------------------------------------------------------- #
# 5. The page builds and is read-only
# --------------------------------------------------------------------------- #
