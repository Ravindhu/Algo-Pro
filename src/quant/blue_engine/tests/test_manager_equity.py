"""
tests/test_manager_equity.py — Phase 1 manager-level tests.

Verifies the wiring between ``PortfolioManager`` and ``EquityTracker``:
idempotent trade ingestion (the dashboard re-sends its whole trade list every
poll), a persisted NAV row, and the equity-curve read-back that Phases 2-5
consume.

Run::

    & ..\\openalgo\\.venv\\Scripts\\python.exe -m pytest tests -q
    & ..\\openalgo\\.venv\\Scripts\\python.exe tests\\test_manager_equity.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.portfolio import portfolio_manager as pm_mod  # noqa: E402
from blue_engine.portfolio import tracker as tr  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _pm(tmp_path: Path, **over) -> pm_mod.PortfolioManager:
    cfg = {"persist": False, "starting_cash": 1_000_000.0,
           "charges_bps": 0.0, "nav_dir": str(tmp_path)}
    cfg.update(over)
    return pm_mod.PortfolioManager.create(cfg)


def _trade(symbol="NIFTY", side="BUY", qty=10, price=100.0, ts=1.0):
    return {"symbol": symbol, "exchange": "NSE_INDEX", "side": side,
            "qty": qty, "price": price, "ts": ts}


# --------------------------------------------------------------------------- #
# 1. Idempotent ingestion (the 5s-poll double-count trap)
# --------------------------------------------------------------------------- #
def test_ingest_is_idempotent_across_polls(tmp_path):
    """The dashboard sends its whole trade list each poll; only new ones book."""
    pm = _pm(tmp_path)
    trades = [_trade(qty=10), _trade(qty=5, price=200.0)]

    first = pm.ingest_trades(trades)
    _check(first["applied"] == 2, f"first poll books both, got {first}")
    _check(first["total_ingested"] == 2, "cursor should be 2")

    second = pm.ingest_trades(trades)          # same list again
    _check(second["applied"] == 0, f"no new fills, got {second}")
    _check(pm.tracker.lot("NIFTY", "NSE_INDEX").qty == 15,
           "position must NOT double-count")

    third = pm.ingest_trades(trades + [_trade(qty=1, price=50.0)])
    _check(third["applied"] == 1, f"only the new fill books, got {third}")
    _check(pm.tracker.lot("NIFTY", "NSE_INDEX").qty == 16,
           f"final qty should be 16, got {pm.tracker.lot('NIFTY','NSE_INDEX').qty}")


def test_ingest_tolerates_objects_and_bad_rows(tmp_path):
    class _T:
        symbol, exchange, side, qty, price, ts = (
            "IT", "NSE_INDEX", "BUY", 5, 200.0, 1.0)

    pm = _pm(tmp_path)
    out = pm.ingest_trades([_T(), {"symbol": "X"}, None])
    _check(out["ok"] is True, "a bad row must not fail ingestion")
    _check(pm.tracker.lot("IT", "NSE_INDEX").qty == 5, "object trade applied")


# --------------------------------------------------------------------------- #
# 2. NAV snapshot + persistence
# --------------------------------------------------------------------------- #
def test_snapshot_nav_persists_and_reports(tmp_path):
    pm = _pm(tmp_path)
    pm.ingest_trades([_trade(qty=10, price=100.0)])
    # Real IST end-of-session timestamp so the row lands on a real trading date.
    ts = datetime(2026, 9, 25, 16, 0, tzinfo=tr.IST).timestamp()

    out = pm.snapshot_nav({"NIFTY": 110.0}, ts=ts, extra={"regime": "RISK_ON"},
                          persist=True)
    _check(out["ok"] is True, f"snapshot should succeed: {out}")
    _check(out["stored"] is not None, "a NAV file path should be returned")
    _check(abs(out["nav"] - 1_000_100.0) < 1e-6,
           f"NAV = 999,000 cash + 1,100 mark, got {out['nav']}")
    _check(out["regime"] == "RISK_ON", "telemetry should be merged")

    rows = tr.read_nav(months=1, directory=tmp_path)
    _check(len(rows) == 1, f"one row should persist, got {len(rows)}")
    _check(rows[0]["regime"] == "RISK_ON", "regime survives the round trip")
    _check(rows[0]["date"] == "2026-09-25",
           f"row should be dated by the fill/snapshot ts, got {rows[0]['date']}")


def test_snapshot_nav_can_skip_persistence(tmp_path):
    pm = _pm(tmp_path)
    out = pm.snapshot_nav({}, persist=False)
    _check(out["ok"] is True and out["stored"] is None,
           "persist=False must not write")
    _check(tr.read_nav(months=1, directory=tmp_path) == [],
           "nothing should be on disk")


# --------------------------------------------------------------------------- #
# 3. Equity-curve read-back (the Phase-2 hand-off)
# --------------------------------------------------------------------------- #
def test_equity_curve_returns_nav_path_and_returns(tmp_path):
    pm = _pm(tmp_path)
    # Write three days directly (simulating three post-close snapshots).
    for day, nav in (("2026-09-23", 100.0), ("2026-09-24", 110.0),
                     ("2026-09-25", 99.0)):
        ts = datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), 16, 0,
                      tzinfo=tr.IST).timestamp()
        tr.store_nav({"ts": ts, "date": day, "nav": nav}, directory=tmp_path)

    curve = pm.get_equity_curve(months=1)
    _check(curve["ok"] is True, f"curve should load: {curve}")
    _check(curve["n"] == 3, f"3 NAV rows expected, got {curve['n']}")
    _check(curve["n_returns"] == 2, f"2 returns expected, got {curve['n_returns']}")
    _check(abs(curve["returns"][0] - 0.10) < 1e-9, "100->110 = +10%")
    _check(abs(curve["returns"][1] + 0.10) < 1e-9, "110->99 = -10%")
    _check(curve["dates"][0] == "2026-09-23", "dates must be ordered oldest first")


def test_equity_curve_degrades_when_empty(tmp_path):
    pm = _pm(tmp_path)
    curve = pm.get_equity_curve()
    _check(curve["ok"] is True, "an empty curve is not an error state")
    _check(curve["n"] == 0 and curve["returns"] == [],
           f"empty curve should be empty, got {curve}")


# --------------------------------------------------------------------------- #
# 4. End-to-end: trades -> NAV -> returns
# --------------------------------------------------------------------------- #
def test_round_trip_strategy_produces_a_clean_return_series(tmp_path):
    """Buy on day 1, mark up on day 2, sell on day 3 -> two real returns.

    The point of the three-day shape: NAV must *move with the mark while the
    position is open*. A test that buys and sells on the same mark proves
    nothing (both days legitimately show the same NAV — cost in, cost out).
    """
    pm = _pm(tmp_path)
    d1 = datetime(2026, 9, 23, 16, 0, tzinfo=tr.IST).timestamp()
    d2 = datetime(2026, 9, 24, 16, 0, tzinfo=tr.IST).timestamp()
    d3 = datetime(2026, 9, 25, 16, 0, tzinfo=tr.IST).timestamp()

    pm.ingest_trades([_trade(qty=10, price=100.0, ts=d1)])
    day1 = pm.snapshot_nav({"NIFTY": 100.0}, ts=d1, persist=True)   # at cost
    day2 = pm.snapshot_nav({"NIFTY": 110.0}, ts=d2, persist=True)   # +10% mark
    pm.ingest_trades([_trade(side="SELL", qty=10, price=110.0, ts=d3)])
    day3 = pm.snapshot_nav({"NIFTY": 110.0}, ts=d3, persist=True)   # realised

    _check(day1["nav"] < day2["nav"],
           f"NAV must rise with the mark: {day1['nav']} -> {day2['nav']}")
    _check(abs(day2["nav"] - (day1["nav"] + 100.0)) < 1e-6,
           f"a +10 mark on 10 shares is +100: {day1['nav']} -> {day2['nav']}")
    _check(abs(day3["realized_pnl"] - 100.0) < 1e-6,
           f"selling at 110 realises 100, got {day3['realized_pnl']}")

    rets = pm.get_equity_curve(months=1)["returns"]
    _check(len(rets) == 2, f"3 NAV days -> 2 returns, got {rets}")
    _check(rets[0] > 0, f"day 1->2 is a gain, got {rets[0]}")
    _check(abs(rets[1]) < 1e-5,
           f"day 2->3 is flat (sold at the mark), got {rets[1]}")


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