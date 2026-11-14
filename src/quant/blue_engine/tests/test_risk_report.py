"""
tests/test_risk_report.py — Phase 2 tests: risk & performance report.

Covers:
* underwater curve (shape, sign, alignment with dates)
* drawdown stats: max DD, duration, recovery — including the two bugs found
  while wiring Phase 2 (see the regression tests at the bottom)
* rolling vol/Sharpe alignment
* performance report assembled from ``attribution.perf``
* risk report: VaR / CVaR / stress + currency conversion
* the one-call ``report()`` on a NAV path, and its degradation path

Run::

    & ..\\openalgo\\.venv\\Scripts\\python.exe -m pytest tests -q
    & ..\\openalgo\\.venv\\Scripts\\python.exe tests\\test_risk_report.py
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.portfolio import risk_report as rr  # noqa: E402
from blue_engine.portfolio import tracker as tr  # noqa: E402
from blue_engine.portfolio.risk import tail as tail_mod  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _nav_rows(days: int = 90, *, seed: int = 7, mu: float = 0.0005,
              sigma: float = 0.010, start: float = 1_000_000.0):
    """A synthetic NAV path shaped exactly like ``tracker.read_nav`` output."""
    rng = random.Random(seed)
    nav = start
    d0 = datetime(2026, 6, 1, 16, 0, tzinfo=tr.IST)
    rows = []
    for i in range(days):
        nav *= (1.0 + rng.gauss(mu, sigma))
        d = d0 + timedelta(days=i)
        rows.append({"ts": d.timestamp(), "date": d.strftime("%Y-%m-%d"),
                     "nav": round(nav, 2)})
    return rows


# --------------------------------------------------------------------------- #
# 1. Underwater
# --------------------------------------------------------------------------- #
def test_underwater_is_negative_and_zero_at_highs():
    rs = [0.10, -0.20, 0.05]
    uw = rr.underwater(rs)
    _check(len(uw["drawdown"]) == 3, "one drawdown point per return")
    _check(uw["drawdown"][0] == 0.0, "a new high has zero drawdown")
    _check(abs(uw["drawdown"][1] + 0.20) < 1e-9,
           f"after -20% the drawdown is -20%, got {uw['drawdown'][1]}")
    _check(all(d <= 1e-12 for d in uw["drawdown"]),
           "drawdown must never be positive")


def test_underwater_aligns_with_dates():
    rs = [0.01, -0.02, 0.03]
    dates = ["2026-09-23", "2026-09-24", "2026-09-25"]
    uw = rr.underwater(rs, dates=dates)
    _check(uw["dates"] == dates, "dates must pass through for the x-axis")


def test_underwater_empty_degrades():
    uw = rr.underwater([])
    _check(uw == {"dates": [], "drawdown": [], "peak": [], "equity": []},
           f"empty input should be empty series: {uw}")


# --------------------------------------------------------------------------- #
# 2. Drawdown stats
# --------------------------------------------------------------------------- #
def test_drawdown_stats_basic_values():
    # +10% then -20%: peak=1.10, trough=0.88 -> DD = 0.20
    d = rr.drawdown_stats([0.10, -0.20])
    _check(abs(d["max_drawdown"] - 0.20) < 1e-9,
           f"max DD should be 20%, got {d['max_drawdown']}")
    _check(d["peak_index"] == 0 and d["trough_index"] == 1,
           f"peak/trough indices wrong: {d}")
    _check(d["duration"] == 1, "the fall took one step")
    _check(d["recovered"] is False, "still under water at the end")
    _check(d["underwater_length"] is None,
           "unrecovered drawdown has no underwater length")
    _check(d["current_drawdown"] < 0, "current drawdown must be negative")


def test_drawdown_recovers_and_reports_length():
    d = rr.drawdown_stats([0.10, -0.20, 0.25])
    _check(d["recovered"] is True, f"the 25% leg restores the peak: {d}")
    _check(abs(d["current_drawdown"]) < 1e-9,
           f"back at the high water mark: {d['current_drawdown']}")
    _check(d["underwater_length"] == 2,
           f"2 steps from peak to recovery, got {d['underwater_length']}")


def test_drawdown_stats_empty_degrades():
    d = rr.drawdown_stats([])
    _check(d["max_drawdown"] is None and d["recovered"] is None,
           f"empty input should be None-valued, got {d}")


def test_drawdown_no_loss_at_all():
    d = rr.drawdown_stats([0.01, 0.02, 0.03])
    _check(d["max_drawdown"] == 0.0, "a monotone rise has no drawdown")
    _check(d["recovered"] is True, "a never-underwater curve is 'recovered'")


# --------------------------------------------------------------------------- #
# 3. Rolling series
# --------------------------------------------------------------------------- #
def test_rolling_series_aligns_and_warmup_is_none():
    rs = [0.001] * 30
    out = rr.rolling_series(rs, window=10)
    _check(len(out["vol"]) == 30, "series must align 1:1 with the input")
    _check(all(v is None for v in out["vol"][:9]), "warm-up should be None")
    _check(all(v is not None for v in out["vol"][9:]), "then populated")
    _check(all(abs(v) < 1e-9 for v in out["vol"][9:]),
           "constant returns have ~zero volatility")


def test_rolling_series_short_input_degrades():
    out = rr.rolling_series([0.01], window=60)
    # One observation cannot fill a 60-day window: the series is all warm-up
    # (None), not empty — it must still align with the input length so the
    # dashboard can zip it against a date axis without a length check.
    _check(out["vol"] == [None] and out["sharpe"] == [None],
           f"a sub-window sample should be all-None, got {out}")
    _check(rr.rolling_series([], window=60)["vol"] == [],
           "an empty input yields an empty series")


# --------------------------------------------------------------------------- #
# 4. Performance report
# --------------------------------------------------------------------------- #
def test_performance_report_has_all_sections():
    rng = random.Random(3)
    rs = [rng.gauss(0.0005, 0.01) for _ in range(120)]
    rep = rr.performance_report(rs)
    _check(rep["ok"] is True, f"report should be ok: {rep.get('warnings')}")
    _check(rep["n"] == 120, "n should be the return count")
    s = rep["summary"]
    for key in ("sharpe", "sortino", "calmar", "annual_return", "annual_vol",
                "max_drawdown", "hit_rate"):
        _check(key in s, f"summary missing '{key}'")
    for key in ("drawdown", "underwater", "rolling"):
        _check(key in rep, f"report missing '{key}'")
    _check(len(rep["underwater"]["drawdown"]) == 120,
           "underwater must align with the return series")


def test_performance_report_degrades_on_tiny_sample():
    rep = rr.performance_report([0.01])
    _check(rep["n"] == 1, "one return")
    _check(any("need >= 2" in w for w in rep["warnings"]),
           f"should warn about the short sample: {rep['warnings']}")


def test_performance_report_flags_all_negative_series():
    rep = rr.performance_report([-0.01, -0.02, -0.005])
    _check(any("no positive days" in w for w in rep["warnings"]),
           f"an all-loss series should be flagged: {rep['warnings']}")


# --------------------------------------------------------------------------- #
# 5. Risk report (VaR / CVaR / stress)
# --------------------------------------------------------------------------- #
def test_risk_report_var_ordering_and_signs():
    rng = random.Random(11)
    rs = [rng.gauss(0.0004, 0.01) for _ in range(500)]
    rep = rr.risk_report(rs)
    v = rep["var"]
    for name, val in v.items():
        _check(val is not None and val > 0,
               f"{name} VaR must be a positive loss, got {val}")
    _check(rep["cvar"] is not None and rep["cvar"] > 0, "CVaR must be positive")
    _check(rep["cvar"] >= min(x for x in v.values() if x is not None) - 1e-12,
           f"CVaR must be at least as severe as the mildest VaR: {rep}")


def test_risk_report_var_matches_theory_on_gaussian_data():
    """On pure Gaussian data at 95%, all three estimators should land near 1.645σ."""
    rng = random.Random(2)
    sigma = 0.01
    rs = [rng.gauss(0.0, sigma) for _ in range(4000)]
    v = rr.risk_report(rs)["var"]
    expected = 1.6449 * sigma
    for name in ("historical", "parametric", "cornish_fisher"):
        got = v[name]
        _check(got is not None and abs(got - expected) < 0.15 * expected,
               f"{name} VaR {got} should be within 15% of {expected:.5f}")


def test_risk_report_var_increases_with_confidence():
    rng = random.Random(5)
    rs = [rng.gauss(0, 0.01) for _ in range(500)]
    lo = rr.risk_report(rs, level=0.95)["var"]["historical"]
    hi = rr.risk_report(rs, level=0.99)["var"]["historical"]
    _check(hi > lo, f"99% VaR ({hi}) must exceed 95% VaR ({lo})")


def test_risk_report_parametric_and_cf_are_populated():
    """Every estimator must return a number on a well-behaved sample.

    Regression guard: the sign bug in the parametric VaR formula made all three
    estimators report 0.0000 (or None, once clipped).
    """
    rng = random.Random(31)
    rs = [rng.gauss(0.0003, 0.011) for _ in range(400)]
    v = rr.risk_report(rs)["var"]
    for name in ("historical", "parametric", "cornish_fisher"):
        _check(v[name] is not None and v[name] > 0,
               f"{name} VaR must be a real positive loss, got {v[name]}")


def test_risk_report_converts_to_currency():
    rng = random.Random(4)
    rs = [rng.gauss(0.0004, 0.012) for _ in range(300)]
    rep = rr.risk_report(rs, capital=2_000_000.0)
    a = rep["amounts"]
    _check(a["var_cf"] is not None and a["var_cf"] > 0,
           f"currency VaR expected, got {a}")
    _check(abs(a["var_cf"] / 2_000_000.0 - rep["var"]["cornish_fisher"]) < 1e-6,
           "currency amount must equal the fraction x capital")


def test_risk_report_stress_scales_with_beta():
    rs = [0.001] * 40
    low = rr.risk_report(rs, beta=0.5)["stress"]
    high = rr.risk_report(rs, beta=1.5)["stress"]
    # Stress rows are sorted worst-first, so compare the same scenario by name.
    def worst(pnl):
        return {r["scenario"]: r["pnl_pct"] for r in pnl}
    lo, hi = worst(low), worst(high)
    _check(hi["COVID_2020"] < lo["COVID_2020"],
           f"a higher-beta book must lose more in a crash: {hi} vs {lo}")


def test_risk_report_warns_on_small_sample():
    rep = rr.risk_report([0.001, -0.002, 0.0])
    _check(any("indicative only" in w for w in rep["warnings"]),
           f"a tiny sample should be flagged as indicative: {rep['warnings']}")


# --------------------------------------------------------------------------- #
# 6. One-call report on a NAV path
# --------------------------------------------------------------------------- #
def test_report_on_nav_path():
    rows = _nav_rows(90)
    rep = rr.report(rows, beta=1.0)
    _check(rep["ok"] is True, f"report should succeed: {rep.get('warnings')}")
    _check(rep["n_nav_rows"] == 90, "should see all NAV rows")
    _check(rep["n_returns"] == 89, f"90 NAV days -> 89 returns, got {rep['n_returns']}")
    _check(rep["capital"] is not None and rep["capital"] > 0,
           "capital should default to the first NAV")
    for section in ("performance", "risk", "dates"):
        _check(section in rep, f"report missing '{section}'")


def test_report_underwater_uses_real_dates():
    rows = _nav_rows(30)
    rep = rr.report(rows)
    uw = rep["performance"]["underwater"]
    _check(len(uw["dates"]) == len(uw["drawdown"]),
           "underwater dates and values must be the same length")
    _check(all(isinstance(d, str) for d in uw["dates"]),
           f"dates should be strings for a Plotly axis: {uw['dates'][:2]}")


def test_report_empty_nav_is_ok_false_not_a_raise():
    rep = rr.report([])
    _check(rep["ok"] is False, "no NAV rows -> ok False")
    _check(rep["n_returns"] == 0, "no returns")
    _check(rep["warnings"] == [] or isinstance(rep["warnings"], list),
           "warnings must still be a list")


# --------------------------------------------------------------------------- #
# 7. REGRESSION — the two bugs found while wiring Phase 2
# --------------------------------------------------------------------------- #
def test_regression_parametric_var_is_not_silently_zero():
    """BUG (fixed): ``-(mu - z*sd)`` with a NEGATIVE left-tail z flipped the
    sign, so the loss came out negative and ``max(..., 0)`` reported 0.0000 for
    every input. A risk report that says "no risk" is worse than none.

    Guard: on Gaussian data the parametric VaR must be a real, positive number
    close to 1.645 sigma.
    """
    rng = random.Random(21)
    rs = [rng.gauss(0.0, 0.01) for _ in range(2000)]
    pv = tail_mod.parametric_var(rs, level=0.95)
    _check(pv is not None and pv > 0.010,
           f"parametric VaR must be a real loss, got {pv}")
    cf = tail_mod.cornish_fisher_var(rs, level=0.95)
    _check(cf is not None and cf > 0.010,
           f"Cornish-Fisher VaR must be a real loss, got {cf}")


def test_regression_var_and_cvar_agree_in_sign_and_order():
    """All estimators must be positive losses, with CVaR the most severe."""
    rng = random.Random(22)
    rs = [rng.gauss(0.0, 0.012) for _ in range(1500)]
    hist = tail_mod.historical_var(rs, level=0.95)
    param = tail_mod.parametric_var(rs, level=0.95)
    cf = tail_mod.cornish_fisher_var(rs, level=0.95)
    cvar = tail_mod.cvar(rs, level=0.95)
    for name, val in (("hist", hist), ("param", param), ("cf", cf),
                      ("cvar", cvar)):
        _check(val is not None and val > 0,
               f"{name} must be a positive loss, got {val}")
    _check(cvar >= min(hist, param, cf) - 1e-9,
           f"CVaR {cvar} must be >= the mildest VaR: {hist}, {param}, {cf}")


def test_regression_recovery_loop_does_not_self_satisfy():
    """BUG (fixed): the recovery scan started AT the peak, so its first step
    re-applied the very return that caused the fall. On a mixed sample that
    could satisfy ``equity >= peak`` immediately and report ``recovered=True``
    while the current drawdown was still negative — telling a PM a hole was
    filled when it was not.

    Guard: recovered and current_drawdown must never contradict each other.
    """
    # Constructed so the naive loop would 'recover' on the first pass.
    cases = [
        [0.10, -0.20, 0.25],          # recovers properly
        [0.10, -0.30, 0.05],          # never recovers
        [0.50, -0.10, -0.05, 0.02],   # partial fall, no recovery
        [-0.30, 0.10, 0.10],          # fall first, partial recovery
    ]
    for rs in cases:
        d = rr.drawdown_stats(rs)
        if d["recovered"] is True:
            _check(abs(d["current_drawdown"]) < 1e-9,
                   f"recovered=True but still {d['current_drawdown']:.2%} "
                   f"under water for {rs}")
        else:
            _check(d["current_drawdown"] < -1e-12,
                   f"recovered=False but current drawdown is "
                   f"{d['current_drawdown']} for {rs}")


def test_regression_underwater_never_positive():
    """The underwater curve feeds a Plotly area chart; a positive value there
    would render as a spike ABOVE zero and misread as a gain."""
    rows = _nav_rows(60, seed=99)
    rep = rr.report(rows)
    dd = rep["performance"]["underwater"]["drawdown"]
    _check(all(v <= 1e-12 for v in dd),
           f"underwater values must be <= 0, worst was {max(dd)}")
    _check(min(dd) < 0, "a noisy path should have a real drawdown somewhere")


# --------------------------------------------------------------------------- #
# Runner (works without pytest)
# --------------------------------------------------------------------------- #
def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            if fn.__code__.co_argcount:        # needs pytest fixtures
                print(f"SKIP {name} (needs pytest fixtures)")
                continue
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