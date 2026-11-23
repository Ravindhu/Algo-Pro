"""
tests/test_tearsheet.py — unit tests for attribution/tearsheet.py (M2).

Run with::

    & ..\\openalgo\\.venv\\Scripts\\python.exe -m pytest tests -q

or as a plain script (no pytest needed)::

    & ..\\openalgo\\.venv\\Scripts\\python.exe tests\\test_tearsheet.py

Design
------
The tearsheet is PURE: it takes snapshots (drift history) and run rows
(gross/turnover/cost history) as plain dicts and must never raise. Each test
asserts both the happy path AND the degradation path (empty/missing data).
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.portfolio.attribution import tearsheet as ts  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# Two synthetic snapshots + two run rows, same shape portfolio_state writes.
SNAPS = [
    {"ts": 1.0, "target": {"NIFTY": 0.5, "IT": 0.5}, "actual": {"NIFTY": 0.6, "IT": 0.4}},
    {"ts": 2.0, "target": {"NIFTY": 0.4, "IT": 0.4, "PHARMA": 0.2},
     "actual": {"NIFTY": 0.5, "IT": 0.4, "PHARMA": 0.1}},
]
ROWS = [
    {"ts": 1.0, "gross": 1.3, "cash_weight": 0.1, "cost_bps": 3.0, "turnover": 0.04},
    {"ts": 2.0, "gross": 1.1, "cash_weight": 0.2, "cost_bps": 5.0, "turnover": 0.08},
]


def test_weight_history_shape():
    out = ts.weight_history(SNAPS)
    _check(out["ts"] == [1.0, 2.0], "ts should be the snapshot timestamps")
    _check(set(out["sleeves"]) == {"NIFTY", "IT", "PHARMA"},
           f"all sleeves collected: {out['sleeves']}")
    _check(out["target"]["NIFTY"][0] == 0.5, "first NIFTY target weight")
    # PHARMA appears only in row 2 -> None backfilled in row 1
    _check(out["target"]["PHARMA"][0] is None, "later sleeve backfilled with None")
    _check(out["actual"]["PHARMA"][1] == 0.1, "actual PHARMA row 2")


def test_weight_history_empty_degrades():
    out = ts.weight_history([])
    _check(out == {"ts": [], "target": {}, "actual": {}, "sleeves": []},
           f"empty snapshots -> empty series: {out}")


def test_turnover_series_planned_and_tracking():
    out = ts.turnover_series(SNAPS)
    _check(out["planned"][0] is None, "first row has no previous target")
    # planned[1] = half-L1({0.5,0.5}, {0.4,0.4,0.2}) = 0.5*(0.1+0.1+0.2) = 0.2
    _check(abs(out["planned"][1] - 0.2) < 1e-9, f"planned turnover: {out['planned']}")
    # tracking[0] = half-L1(actual, target) = 0.5*(0.1+0.1) = 0.1
    _check(abs(out["tracking"][0] - 0.1) < 1e-9, f"tracking turnover: {out['tracking']}")


def test_turnover_series_empty_degrades():
    out = ts.turnover_series([])
    _check(out == {"ts": [], "planned": [], "tracking": []}, "empty -> empty")


def test_drift_history_totals():
    out = ts.drift_history(SNAPS)
    # snap1 total abs drift = 0.2 ; snap2 = 0.2
    _check(abs(out["total_abs_drift"][0] - 0.2) < 1e-9, "drift snap1")
    _check(abs(out["total_abs_drift"][1] - 0.2) < 1e-9, "drift snap2")
    _check(out["per_sleeve"], "per-sleeve drift series should be populated")


def test_hhi_series_values():
    out = ts.hhi_series(SNAPS)
    # snap1 target {0.5, 0.5} -> HHI = 0.25+0.25 = 0.5
    _check(abs(out["hhi"][0] - 0.5) < 1e-9, f"HHI snap1: {out['hhi'][0]}")
    # snap2 {0.4,0.4,0.2} -> 0.16+0.16+0.04 = 0.36
    _check(abs(out["hhi"][1] - 0.36) < 1e-9, f"HHI snap2: {out['hhi'][1]}")


def test_gross_series_from_rows():
    out = ts.gross_series(ROWS)
    _check(out["gross"] == [1.3, 1.1], "gross path")
    _check(out["cost_bps"] == [3.0, 5.0], "cost path")
    _check(out["turnover"] == [0.04, 0.08], "turnover path")


def test_rolling_stats_window():
    # 25 stable returns of +0.001 -> known vol ~ 0, sharpe clipped to 0-ish
    rs = [0.001] * 25
    out = ts.rolling_stats(rs, window=20)
    _check(out["ok"] is True, "enough returns -> ok")
    _check(len(out["vol"]) == len(rs) - 20 + 1, "one point per window end")
    _check(all(v is not None for v in out["vol"]), "vol series complete")


def test_rolling_stats_short_degrades():
    out = ts.rolling_stats([0.001, 0.002])
    _check(out["ok"] is False, "too few returns -> ok False")
    out2 = ts.rolling_stats(None)
    _check(out2["ok"] is False, "None returns -> ok False")


def test_build_full_shape():
    out = ts.build(SNAPS, ROWS, returns=[0.001] * 30)
    _check(out["ok"] is True, f"build should be ok: {out.get('warnings')}")
    _check(out["runs"] == 2, "runs = number of snapshots")
    for key in ("weight_history", "turnover", "drift", "hhi", "gross",
                "rolling", "summary"):
        _check(key in out, f"missing tearsheet key '{key}'")
    _check(out["summary"]["avg_hhi"] is not None, "avg HHI computed")
    _check(out["summary"]["last_target"].get("PHARMA") == 0.2,
           "last target snapshot included")


def test_build_empty_never_raises():
    out = ts.build([], [])
    _check(out["ok"] is True, "empty input must still build, not raise")
    _check(out["runs"] == 0, "zero runs")
    _check(out["summary"]["avg_hhi"] is None, "no data -> None summary values")


def test_portfolio_manager_get_tearsheet_wire():
    """The PM API must return the tearsheet without raising (degrade contract)."""
    from blue_engine.portfolio import portfolio_manager as pm_mod
    pm = pm_mod.PortfolioManager.create({"persist": False})
    out = pm.get_tearsheet(days=7)
    _check("ok" in out, "tearsheet response must carry an ok flag")
    _check(out["ok"] in (True, False), "ok must be a bool (degraded allowed)")


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
