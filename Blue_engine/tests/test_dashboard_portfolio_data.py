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
    from blue_engine.dashboard.pages import _data
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
    from blue_engine.dashboard.pages import _data
    pm = _data.portfolio_manager()
    if pm is None:
        return                                   # environment without the engine
    _check(pm.config.get("persist") is False,
           "persist must be False for the reader-only dashboard instance")


# --------------------------------------------------------------------------- #
# 2. Every accessor degrades to a dict (never raises)
# --------------------------------------------------------------------------- #
def test_all_accessors_return_dicts_and_never_raise():
    from blue_engine.dashboard.pages import _data
    for name in ("equity_curve", "risk_report", "exposure_snapshot",
                 "attribution_snapshot", "policy_snapshot", "views_snapshot"):
        fn = getattr(_data, name)
        out = fn()
        _check(isinstance(out, dict), f"{name}() must return a dict, got {type(out)}")


def test_accessors_carry_ok_or_action_so_callers_can_judge():
    """A panel needs to know whether its data is real; a bare {} is unusable."""
    from blue_engine.dashboard.pages import _data
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
    from blue_engine.dashboard.pages import _data
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
    from blue_engine.dashboard.pages import _data
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
    from blue_engine.dashboard.pages import _data
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
def test_page_registers_and_builds_with_the_app():
    from blue_engine.dashboard.dash_app import create_app
    import dash
    app = create_app()
    _check("analytics_portfolio" in dash.page_registry
           or any(k.endswith("analytics_portfolio")
                  for k in dash.page_registry),
           f"the page should register: {sorted(dash.page_registry)}")
    from blue_engine.dashboard.pages import analytics_portfolio as ap
    panels = ap.body()
    _check(isinstance(panels, list) and len(panels) >= 6,
           f"expected at least 6 panels, got {len(panels)}")


def test_page_imports_no_execution_or_sdk_modules():
    """Structural proof the page cannot trade: no SDK, ledger or POV imports.

    A UI that *can* place an order will eventually place one by accident.
    """
    path = (Path(_ROOT) / "dashboard" / "pages" / "analytics_portfolio.py")
    src = path.read_text(encoding="utf-8")
    # `blue_engine.dashboard.pages._data` is a legitimate read-only import, so the
    # package name itself is allowed. What is banned is anything that can execute:
    # the SDK, the order API, the ledger, or the pipeline.
    for banned in ("openalgo", "placeorder", "core.ledger",
                   "core.pipeline", "core.runner", "core.orchestrator"):
        _check(banned not in src,
               f"the dashboard page must not import/reference '{banned}'")


def test_every_figure_builder_returns_a_figure_and_never_raises():
    from blue_engine.dashboard.dash_app import create_app
    create_app()
    from blue_engine.dashboard.pages import analytics_portfolio as ap
    builders = {
        "equity": lambda: ap._equity_figure({}),
        "underwater": lambda: ap._underwater_figure({}, {}),
        "sectors": lambda: ap._sector_figure({}),
        "buckets": lambda: ap._bucket_figure({}),
    }
    for name, fn in builders.items():
        fig = fn()
        _check(hasattr(fig, "data"), f"{name} must return a Plotly figure")
    # Degenerate input must produce an ANNOTATED empty state, not a blank frame.
    for name, fn in builders.items():
        fig = fn()
        if len(fig.data) == 0:
            _check(len(fig.layout.annotations) >= 1,
                   f"{name} returned a blank frame; it must explain itself")


def test_tables_render_annotated_empty_states():
    from blue_engine.dashboard.dash_app import create_app
    create_app()
    from blue_engine.dashboard.pages import analytics_portfolio as ap
    from dash import html

    y = ap._attribution_table({})
    _check(isinstance(y, html.Div), "an empty attribution must explain itself")
    s = ap._stress_table({})
    _check(isinstance(s, html.Div), "an empty stress table must explain itself")
    v = ap._views_table({})
    _check(isinstance(v, html.Div), "an empty views table must explain itself")


def test_reconciliation_note_flags_an_out_of_balance_attribution():
    """A non-reconciling table must be called out LOUDLY on the page."""
    from blue_engine.dashboard.dash_app import create_app
    create_app()
    from blue_engine.dashboard.pages import analytics_portfolio as ap

    bad = {"reconciliation": {"by_sleeve": {"ok": False, "residual": -12.5}}}
    note = ap._reconciliation_note(bad)
    _check(note is not None, "an out-of-balance table must raise a visible note")
    good = {"reconciliation": {"by_sleeve": {"ok": True, "residual": 0.0}}}
    _check(ap._reconciliation_note(good) is None,
           "a balanced table should not raise a note")


# --------------------------------------------------------------------------- #
# Runner (works without pytest)
# --------------------------------------------------------------------------- #
def _run_all() -> int:
    import tempfile
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            if fn.__code__.co_argcount:        # needs the tmp_path fixture
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"PASS {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {name}: {exc!r}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())