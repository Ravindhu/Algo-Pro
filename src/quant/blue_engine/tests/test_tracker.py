"""
tests/test_tracker.py — Phase 1 tests for the equity/P&L accounting core.

Run with::

    & ..\\openalgo\\.venv\\Scripts\\python.exe -m pytest tests -q

or as a plain script (no pytest needed)::

    & ..\\openalgo\\.venv\\Scripts\\python.exe tests\\test_tracker.py

What is covered
---------------
* weighted-average cost basis (buy, add, partial sell, full close, flip)
* charges are applied per side and net out of realised P&L AND NAV
* cash accounting: NAV = cash + Σ(qty·mark), unchanged by a round trip
* IST day boundary (not UTC) so a late-evening fill books to the right date
* snapshot shape + JSON-serialisability
* NAV persistence round-trip and the daily-return conversion
* degrade-never-halt: bad fills are skipped, never raise
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.portfolio import tracker as tr  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------- #
# 1. Cost basis
# --------------------------------------------------------------------------- #
def test_buy_sets_average_cost():
    t = tr.EquityTracker(starting_cash=1_000_000.0, charges_bps=0.0)
    t.on_fill("NIFTY", "NSE_INDEX", "BUY", 10, 22_000.0)
    lot = t.lot("NIFTY", "NSE_INDEX")
    _check(lot.qty == 10, "qty should be 10")
    _check(abs(lot.avg_price - 22_000.0) < 1e-9, "avg price should be the fill")
    _check(abs(t.cash - (1_000_000.0 - 220_000.0)) < 1e-6,
           f"cash should fall by notional, got {t.cash}")


def test_adding_to_a_long_averages_the_price():
    t = tr.EquityTracker(starting_cash=1_000_000.0, charges_bps=0.0)
    t.on_fill("NIFTY", "NSE_INDEX", "BUY", 10, 100.0)
    t.on_fill("NIFTY", "NSE_INDEX", "BUY", 30, 200.0)
    lot = t.lot("NIFTY", "NSE_INDEX")
    # (10*100 + 30*200) / 40 = 175
    _check(lot.qty == 40, "qty should accumulate")
    _check(abs(lot.avg_price - 175.0) < 1e-9,
           f"weighted average should be 175, got {lot.avg_price}")


def test_partial_sell_realises_against_average_and_keeps_it():
    t = tr.EquityTracker(starting_cash=1_000_000.0, charges_bps=0.0)
    t.on_fill("NIFTY", "NSE_INDEX", "BUY", 40, 175.0)
    t.on_fill("NIFTY", "NSE_INDEX", "SELL", 10, 200.0)
    lot = t.lot("NIFTY", "NSE_INDEX")
    _check(lot.qty == 30, f"qty should be 30, got {lot.qty}")
    _check(abs(lot.avg_price - 175.0) < 1e-9,
           "average cost must be unchanged by a partial sell")
    _check(abs(lot.realized_pnl - 250.0) < 1e-9,
           f"realised should be (200-175)*10 = 250, got {lot.realized_pnl}")


def test_full_close_zeroes_qty_and_cost():
    t = tr.EquityTracker(starting_cash=1_000_000.0, charges_bps=0.0)
    t.on_fill("NIFTY", "NSE_INDEX", "BUY", 10, 100.0)
    t.on_fill("NIFTY", "NSE_INDEX", "SELL", 10, 110.0)
    lot = t.lot("NIFTY", "NSE_INDEX")
    _check(lot.qty == 0, "position should be flat")
    _check(lot.avg_price == 0.0, "cost basis should reset when flat")
    _check(abs(lot.realized_pnl - 100.0) < 1e-9, "realised = 10 * 10")


def test_sell_more_than_held_flips_to_a_short():
    """A flip must open a short at the fill price, not create a negative cost."""
    t = tr.EquityTracker(starting_cash=1_000_000.0, charges_bps=0.0)
    t.on_fill("NIFTY", "NSE_INDEX", "BUY", 10, 100.0)
    t.on_fill("NIFTY", "NSE_INDEX", "SELL", 25, 120.0)
    lot = t.lot("NIFTY", "NSE_INDEX")
    _check(lot.qty == -15, f"should be short 15, got {lot.qty}")
    _check(abs(lot.avg_price - 120.0) < 1e-9,
           f"short cost basis should be the fill price, got {lot.avg_price}")
    _check(abs(lot.realized_pnl - 200.0) < 1e-9,
           "closing the 10 long shares realises (120-100)*10 = 200")


def test_buying_back_a_short_realises_correctly():
    t = tr.EquityTracker(starting_cash=1_000_000.0, charges_bps=0.0)
    t.on_fill("NIFTY", "NSE_INDEX", "SELL", 10, 120.0)     # open short @120
    t.on_fill("NIFTY", "NSE_INDEX", "BUY", 10, 100.0)      # cover @100
    lot = t.lot("NIFTY", "NSE_INDEX")
    _check(lot.qty == 0, "should be flat after covering")
    _check(abs(lot.realized_pnl - 200.0) < 1e-9,
           f"short profit = (120-100)*10 = 200, got {lot.realized_pnl}")


# --------------------------------------------------------------------------- #
# 2. Charges
# --------------------------------------------------------------------------- #
def test_charges_deduct_from_realised_pnl_and_cash():
    t = tr.EquityTracker(starting_cash=1_000_000.0)
    # 10 bps per side on 220,000 and 225,000 notional = 220 + 225
    t.on_fill("NIFTY", "NSE_INDEX", "BUY", 10, 22_000.0, charges_bps=10.0)
    t.on_fill("NIFTY", "NSE_INDEX", "SELL", 10, 22_500.0, charges_bps=10.0)
    fees = t.total_fees
    _check(abs(fees - 445.0) < 1e-6, f"fees should be 445, got {fees}")
    gross = (22_500.0 - 22_000.0) * 10
    _check(abs(t.realized_pnl - gross) < 1e-6,
           "lot.realized_pnl is gross; fees are tracked separately")
    nav = t.nav({})
    _check(abs(nav - (1_000_000.0 + gross - fees)) < 1e-6,
           f"NAV must be net of fees: {nav}")


def test_round_trip_nav_equals_cash_when_flat():
    t = tr.EquityTracker(starting_cash=500_000.0, charges_bps=0.0)
    t.on_fill("X", "NSE", "BUY", 5, 1_000.0)
    t.on_fill("X", "NSE", "SELL", 5, 1_000.0)
    _check(abs(t.nav({}) - 500_000.0) < 1e-6,
           "a flat, cost-free round trip must leave NAV unchanged")


# --------------------------------------------------------------------------- #
# 3. Valuation
# --------------------------------------------------------------------------- #
def test_nav_uses_marks_and_tracks_unrealised():
    t = tr.EquityTracker(starting_cash=1_000_000.0, charges_bps=0.0)
    t.on_fill("NIFTY", "NSE_INDEX", "BUY", 10, 100.0)
    nav = t.nav({"NIFTY": 110.0})
    # cash = 1,000,000 - 1,000 ; position = 1,100
    _check(abs(nav - (999_000.0 + 1_100.0)) < 1e-6, f"nav wrong: {nav}")
    _check(abs(t.unrealized_pnl({"NIFTY": 110.0}) - 100.0) < 1e-6,
           "unrealised should be +100")


def test_gross_vs_net_exposure_with_a_short():
    t = tr.EquityTracker(starting_cash=1_000_000.0, charges_bps=0.0)
    t.on_fill("A", "NSE", "BUY", 10, 100.0)
    t.on_fill("B", "NSE", "SELL", 5, 200.0)
    marks = {"A": 100.0, "B": 200.0}
    _check(abs(t.exposure(marks) - 2_000.0) < 1e-6,
           f"gross = |1000| + |1000| = 2000, got {t.exposure(marks)}")
    _check(abs(t.net_exposure(marks) - 0.0) < 1e-6,
           f"net should cancel to 0, got {t.net_exposure(marks)}")


# --------------------------------------------------------------------------- #
# 4. IST day boundary
# --------------------------------------------------------------------------- #
def test_ist_date_uses_kolkata_not_utc():
    """23:00 UTC is already the NEXT day in IST (04:30) — the date must differ."""
    # 2026-09-25T23:00Z -> 2026-09-26T04:30 IST
    ts = datetime(2026, 9, 25, 23, 0, tzinfo=tr.timezone.utc).timestamp()
    _check(tr.ist_date(ts) == "2026-09-26",
           f"expected IST date 2026-09-26, got {tr.ist_date(ts)}")


def test_ist_date_mid_session():
    # 10:00 IST == 04:30 UTC on the same date
    ts = datetime(2026, 9, 25, 4, 30, tzinfo=tr.timezone.utc).timestamp()
    _check(tr.ist_date(ts) == "2026-09-25",
           f"session date should be 2026-09-25, got {tr.ist_date(ts)}")


# --------------------------------------------------------------------------- #
# 5. Snapshot
# --------------------------------------------------------------------------- #
def test_snapshot_shape_is_json_serialisable():
    import json
    t = tr.EquityTracker(starting_cash=1_000_000.0, charges_bps=0.0)
    t.on_fill("NIFTY", "NSE_INDEX", "BUY", 10, 22_000.0)
    snap = t.snapshot({"NIFTY": 22_100.0})
    for key in ("ts", "date", "nav", "cash", "gross_exposure", "net_exposure",
                "realized_pnl", "unrealized_pnl", "total_fees", "total_pnl",
                "n_positions", "positions"):
        _check(key in snap, f"snapshot missing '{key}'")
    _check(snap["n_positions"] == 1, "one open position expected")
    json.dumps(snap)          # must not raise


def test_snapshot_extra_fields_merge():
    t = tr.EquityTracker(starting_cash=1_000.0, charges_bps=0.0)
    snap = t.snapshot({}, extra={"regime": "RISK_ON", "optimiser": "hrp"})
    _check(snap["regime"] == "RISK_ON" and snap["optimiser"] == "hrp",
           "extra telemetry should be merged for later slicing")


# --------------------------------------------------------------------------- #
# 6. Persistence + returns
# --------------------------------------------------------------------------- #
def test_nav_roundtrip_and_daily_returns(tmp_path):
    d = Path(tmp_path)
    # Simulate three IST days of NAV at 100 -> 110 -> 99
    for day, nav in (("2026-09-23", 100.0), ("2026-09-24", 110.0),
                     ("2026-09-25", 99.0)):
        ts = datetime(2026, int(day[5:7]), int(day[8:10]), 16, 0,
                      tzinfo=tr.IST).timestamp()
        tr.store_nav({"ts": ts, "date": day, "nav": nav}, directory=d)
    rows = tr.read_nav(months=1, directory=d)
    _check(len(rows) == 3, f"should read 3 rows, got {len(rows)}")
    _check([r["date"] for r in rows] == ["2026-09-23", "2026-09-24",
                                         "2026-09-25"],
           "rows must be date-ordered oldest first")
    rets = tr.daily_returns(rows)
    _check(len(rets) == 2, f"3 NAV points → 2 returns, got {len(rets)}")
    _check(abs(rets[0] - 0.10) < 1e-9, f"100→110 should be +10%, got {rets[0]}")
    _check(abs(rets[1] - (-0.10)) < 1e-9, f"110→99 should be -10%, got {rets[1]}")


def test_daily_returns_keeps_last_snapshot_per_day():
    rows = [
        {"date": "2026-09-25", "ts": 1.0, "nav": 100.0},
        {"date": "2026-09-25", "ts": 2.0, "nav": 120.0},   # EOD wins
        {"date": "2026-09-26", "ts": 3.0, "nav": 132.0},
    ]
    rets = tr.daily_returns(rows)
    _check(abs(rets[0] - 0.10) < 1e-9,
           f"120→132 = +10% (the 100.0 intraday row must be ignored), got {rets}")


def test_daily_returns_drops_bad_nav():
    rows = [{"date": "2026-09-24", "nav": 0.0},
            {"date": "2026-09-25", "nav": None},
            {"date": "2026-09-26", "nav": 100.0}]
    _check(tr.daily_returns(rows) == [],
           "non-positive / missing NAV must not produce a spurious return")


def test_store_nav_never_raises_on_bad_dir():
    # A file used as a directory → the write fails; it must be swallowed.
    with tempfile.NamedTemporaryFile(delete=False) as fh:
        bad = Path(fh.name)
    try:
        out = tr.store_nav({"date": "2026-09-25", "nav": 1.0},
                           directory=bad / "nope")
        _check(out is None, "a failed write returns None, not an exception")
    finally:
        bad.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 7. Degradation + integration
# --------------------------------------------------------------------------- #
def test_invalid_fills_are_skipped_not_raised():
    t = tr.EquityTracker(starting_cash=1_000.0, charges_bps=0.0)
    for bad in (("X", "NSE", "BUY", 0, 100.0),
                ("X", "NSE", "BUY", 10, 0.0),
                ("X", "NSE", "HOLD", 10, 100.0),
                ("X", "NSE", "BUY", -5, 100.0)):
        _check(t.on_fill(*bad) is None, f"should skip {bad}")
    _check(t.fills() == [], "no fills should be recorded")
    _check(t.cash == 1_000.0, "cash must be untouched")


def test_on_trades_accepts_objects_and_dicts():
    class _Trade:                      # mimics dashboard TradeRecord
        symbol, exchange, side, qty, price, ts = (
            "NIFTY", "NSE_INDEX", "BUY", 10, 100.0, 1.0)

    t = tr.EquityTracker(starting_cash=10_000.0, charges_bps=0.0)
    n = t.on_trades([_Trade(), {"symbol": "IT", "side": "BUY", "qty": 5,
                                "price": 200.0, "exchange": "NSE_INDEX"}])
    _check(n == 2, f"both trades should apply, got {n}")
    _check(t.lot("NIFTY", "NSE_INDEX").qty == 10, "object trade applied")
    _check(t.lot("IT", "NSE_INDEX").qty == 5, "dict trade applied")


def test_reset_realized_clears_but_keeps_fees():
    t = tr.EquityTracker(starting_cash=1_000_000.0, charges_bps=10.0)
    t.on_fill("NIFTY", "NSE_INDEX", "BUY", 10, 100.0)
    t.on_fill("NIFTY", "NSE_INDEX", "SELL", 10, 110.0)
    fees_before = t.total_fees
    cleared = t.reset_realized()
    _check(abs(cleared["NIFTY"] - 100.0) < 1e-6,
           f"cleared should report the realised P&L, got {cleared}")
    _check(t.realized_pnl == 0.0, "realised should now be zero")
    _check(abs(t.total_fees - fees_before) < 1e-9,
           "fees are cumulative and must survive a realised reset")


def test_docstring_worked_example():
    """The module docstring claims -534.0 fees and +4466.0 net on 10 lakh.

    Pin those numbers so the documented example cannot silently drift from the
    code. This is also the 'charges are per side' lesson: a round trip pays
    twice, so +5000 gross becomes +4466 net.
    """
    t = tr.EquityTracker(starting_cash=1_000_000.0)
    t.on_fill("NIFTY", "NSE_INDEX", "BUY", 10, 22_000.0, charges_bps=12.0)
    t.on_fill("NIFTY", "NSE_INDEX", "SELL", 10, 22_500.0, charges_bps=12.0)
    _check(round(t.total_fees, 2) == 534.0,
           f"docstring says 534.00 fees, got {round(t.total_fees, 2)}")
    _check(round(t.realized_pnl, 2) == 5_000.0,
           f"realised is gross here (fees separate), got {t.realized_pnl}")
    net = t.nav({"NIFTY": 22_500.0}) - t.starting_cash
    _check(round(net, 2) == 4_466.0,
           f"docstring says 4466.00 net, engine says {round(net, 2)}")


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