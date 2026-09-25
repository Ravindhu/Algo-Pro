"""
tests/test_backfill_nav.py — the NAV reconstruction tool + period inference.

Two things are tested here, and the second is the one that matters:

1. ``_align`` builds a rectangular dated panel, and *intersects* rather than
   unioning sleeve histories (a fill-forward would fabricate zero-return days and
   understate volatility — the stale-data trap the policy layer halts on).
2. ``periods_per_year`` infers the store's spacing, because annualising a
   monthly series at a hard-coded 252 overstates volatility by sqrt(252/12) =
   4.6x. A 10% book reported as a 46% book is worse than no report.

The walk-forward loop is also asserted to TERMINATE on a short window: a
clamping bug there once made the job spin forever because `t` stopped advancing.

Run::

    & ..\\openalgo\\.venv\\Scripts\\python.exe -m pytest tests -q
    & ..\\openalgo\\.venv\\Scripts\\python.exe tests\\test_backfill_nav.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.jobs import backfill_nav as bf  # noqa: E402
from blue_engine.portfolio import tracker as tr  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _series(days: int = 300, *, start: str = "2024-01-01",
            price: float = 100.0, drift: float = 0.0004, seed: int = 1,
            missing_every: int = 0):
    """A dated (date, close) series; optionally drop every Nth date."""
    import random
    rng = random.Random(seed)
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    out = []
    px = price
    for i in range(days):
        px *= (1.0 + rng.gauss(drift, 0.010))
        d = (d0 + timedelta(days=i))
        if missing_every and i % missing_every == 0:
            continue
        out.append((d.strftime("%Y-%m-%d"), round(px, 4)))
    return out


# --------------------------------------------------------------------------- #
# 1. Alignment must INTERSECT, not union
# --------------------------------------------------------------------------- #
def test_align_intersects_sleeve_histories():
    """Dates are the INTERSECTION of every sleeve's history.

    Unioning would force a fill-forward for the missing sleeve, fabricating a
    zero-return day — which silently understates volatility on exactly the days
    something was wrong. Dropping the date is the honest choice.
    """
    a = _series(100, seed=1)
    b = _series(100, seed=2, missing_every=7)      # b is missing some dates
    names, dates, prices = bf._align({"A": a, "B": b}, None, None)
    _check(names == ["A", "B"], f"sleeves sorted: {names}")
    da, db = {d for d, _ in a}, {d for d, _ in b}
    _check(set(dates) == (da & db),
           f"dates must be the intersection: {len(dates)} vs "
           f"{len(da & db)} expected")
    _check(all(len(row) == len(dates) for row in prices),
           "every sleeve must have one price per aligned date")


def test_align_applies_start_and_end_filters():
    s = _series(200)
    _names, dates, _p = bf._align({"A": s}, "2024-02-01", "2024-03-01")
    _check(all("2024-02-01" <= d <= "2024-03-01" for d in dates),
           f"window filter not applied: {dates[:2]} .. {dates[-2:]}")


def test_align_returns_empty_when_nothing_overlaps():
    a = _series(60, start="2024-01-01", seed=1)
    b = _series(60, start="2025-06-01", seed=2)      # disjoint window
    _names, dates, _p = bf._align({"A": a, "B": b}, None, None)
    _check(dates == [], "disjoint histories have no overlap")


def test_align_needs_at_least_two_sleeves_to_be_useful():
    _check(bf._align({}, None, None) == ([], [], []), "empty input")
    names, _d, _p = bf._align({"A": _series(50)}, None, None)
    _check(names == ["A"], "one sleeve still aligns (the caller enforces >= 2)")


# --------------------------------------------------------------------------- #
# 2. Period inference — the 4.6x annualisation trap
# --------------------------------------------------------------------------- #
def test_periods_per_year_infers_daily_spacing():
    """A live store is ~1 row per session -> the factor must be ~252."""
    import random
    rng = random.Random(4)
    nav = 1_000_000.0
    d0 = datetime(2026, 1, 1, 16, 0, tzinfo=tr.IST)
    rows = []
    for i in range(60):
        nav *= (1 + rng.gauss(0, 0.01))
        d = d0 + timedelta(days=i)
        rows.append({"ts": d.timestamp(), "date": d.strftime("%Y-%m-%d"),
                     "nav": round(nav, 2)})
    ppy = tr.periods_per_year(rows)
    _check(240 <= ppy <= 366, f"daily spacing should infer ~252, got {ppy}")


def test_periods_per_year_infers_monthly_spacing():
    """A backfilled monthly-rebalance store -> the factor must be ~12.

    This is the bug that mattered: annualising a monthly series at 252 inflated
    a 10.4% book into a 46.7% book.
    """
    d0 = datetime(2024, 1, 31, 16, 0, tzinfo=tr.IST)
    rows = []
    for i in range(24):
        d = (d0 + timedelta(days=int(30.44 * i)))
        rows.append({"ts": d.timestamp(), "date": d.strftime("%Y-%m-%d"),
                     "nav": 1_000_000.0 + i * 1000})
    ppy = tr.periods_per_year(rows)
    _check(11.0 <= ppy <= 13.0, f"monthly spacing should infer ~12, got {ppy}")


def test_annualisation_factor_reproduces_the_real_volatility():
    """The end-to-end assertion: a monthly series must NOT be scaled by 252."""
    d0 = datetime(2024, 1, 31, 16, 0, tzinfo=tr.IST)
    rows = []
    nav = 1_000_000.0
    import random
    rng = random.Random(9)
    for i in range(36):
        nav *= (1.0 + rng.gauss(0.004, 0.02))       # ~2%/month
        d = (d0 + timedelta(days=int(30.44 * i)))
        rows.append({"ts": d.timestamp(), "date": d.strftime("%Y-%m-%d"),
                     "nav": round(nav, 2)})
    from blue_engine.portfolio import risk_report as rr
    rep = rr.report(rows)
    ppy = rep.get("periods_per_year")
    _check(ppy is not None and ppy < 20,
           f"report must infer the monthly spacing, got {ppy}")
    vol = (rep.get("performance") or {}).get("summary", {}).get("annual_vol")
    _check(vol is not None and vol < 0.25,
           f"2%/month vol annualises to ~7%, not {vol} "
           f"(a 252 factor would give ~33%)")


def test_periods_per_year_falls_back_safely():
    _check(tr.periods_per_year([]) == 252.0, "no rows -> default")
    _check(tr.periods_per_year(None) == 252.0, "None -> default")
    one = [{"date": "2026-01-01", "nav": 100.0}]
    _check(tr.periods_per_year(one) == 252.0,
           "fewer than 3 rows -> default (cannot infer spacing)")
    same = [{"date": "2026-01-01", "nav": 100.0}] * 5
    _check(tr.periods_per_year(same) == 252.0,
           "a zero-span series -> default, never a divide-by-zero")


# --------------------------------------------------------------------------- #
# 3. The walk-forward must TERMINATE
# --------------------------------------------------------------------------- #
def test_run_terminates_on_a_short_window(tmp_path):
    """A clamping bug once made `t` stop advancing, so the job spun forever.

    The guard is `if nxt <= t: break`. Without it a window whose `t + step`
    exceeds the array length clamps `nxt` to the last index, the mark-to-market
    loop becomes empty, and `t` never moves again.
    """
    import time
    t0 = time.perf_counter()
    out = bf.run(dry_run=True, start="2025-01-01", step=20, window=60,
                 log=lambda m: None)
    elapsed = time.perf_counter() - t0
    _check(elapsed < 60.0,
           f"the backfill must terminate promptly, took {elapsed:.1f}s")
    _check(out["stage"] in ("done", "no_rows", "no_data", "too_short", "failed"),
           f"unexpected stage: {out['stage']}")


def test_run_reports_a_clean_degradation_without_a_cache(tmp_path):
    """No usable prices -> a documented stage, never an exception."""
    out = bf.run(dry_run=True, cache_db=str(tmp_path / "missing.db"),
                 log=lambda m: None)
    _check(out["ok"] is False, "no cache -> ok False")
    _check(out["stage"] in ("no_data", "failed"),
           f"should report a clean stage, got {out['stage']}")
    _check(out["warnings"] or out["error"], "should explain why")


# --------------------------------------------------------------------------- #
# 4. Backfill rows are PROVENANCE-TAGGED
# --------------------------------------------------------------------------- #
def test_backfilled_rows_are_tagged_and_lose_to_live_rows(tmp_path):
    """Reconstructed history must never overwrite what actually happened.

    A panel that silently blends backfill with live data is a panel that lies,
    so the row carries ``source='backfill'`` and ``daily_returns`` prefers the
    live row when both exist for a date.
    """
    d = "2026-09-25"
    tr.store_nav({"ts": 1.0, "date": d, "nav": 1_100_000.0,
                  "source": "backfill"}, directory=tmp_path)
    tr.store_nav({"ts": 2.0, "date": d, "nav": 1_050_000.0},
                 directory=tmp_path)                 # live row, same date
    rows = tr.read_nav(months=12, directory=tmp_path)
    _check(len(rows) == 2, f"both rows are stored, got {len(rows)}")

    # A live row on a later date makes the return computable.
    tr.store_nav({"ts": 3.0, "date": "2026-09-26", "nav": 1_060_000.0},
                 directory=tmp_path)
    # The series must be built from the LIVE 1,050,000, not the backfill 1,100,000.
    rows = tr.read_nav(months=12, directory=tmp_path)
    rets = tr.daily_returns(rows)
    _check(len(rets) == 1, f"one return expected, got {len(rets)}")
    expected = 1_060_000.0 / 1_050_000.0 - 1.0
    _check(abs(rets[0] - expected) < 1e-9,
           f"live row must win: expected {expected:.5f}, got {rets[0]:.5f}")


def test_read_nav_windows_by_date_not_by_file_count(tmp_path):
    """A sparse store must not yield a disjoint window.

    Slicing the last N FILES on a store with gaps returns a set spanning the
    gaps, and daily_returns then computes returns ACROSS them — which turned a
    +9.65% curve into a -86% annual return.
    """
    d0 = datetime(2024, 1, 31, 16, 0, tzinfo=tr.IST)
    # Month 0, then a gap, then months 20..23
    for i in [0, 20, 21, 22, 23]:
        d = (d0 + timedelta(days=int(30.44 * i)))
        tr.store_nav({"ts": d.timestamp(), "date": d.strftime("%Y-%m-%d"),
                      "nav": 1_000_000.0 + i * 1_000}, directory=tmp_path)

    windowed = tr.read_nav(months=6, directory=tmp_path)
    dates = [r["date"] for r in windowed]
    _check(dates == sorted(dates), "rows must stay ordered")
    # 6 months back from the last date must EXCLUDE the old month-0 row.
    _check(all(d > "2025-06-01" for d in dates),
           f"the window must be date-based, old rows leaked in: {dates}")


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