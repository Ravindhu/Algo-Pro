"""
tests/test_pnl_attribution.py — Phase 3b tests: where the P&L came from.

Covers IST session bucketing (including the AFTER red-flag), P&L by sleeve,
strategy and bucket, and — most importantly — that every decomposition
RECONCILES back to the total. An attribution table that does not add up is worse
than no table, because someone will trade on the gap.

Run::

    & ..\\openalgo\\.venv\\Scripts\\python.exe -m pytest tests -q
    & ..\\openalgo\\.venv\\Scripts\\python.exe tests\\test_pnl_attribution.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.portfolio import pnl_attribution as pa  # noqa: E402
from blue_engine.portfolio import tracker as tr  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _ist(day: int, hour: int, minute: int = 0, *, month: int = 9) -> float:
    """Epoch seconds for an IST wall-clock time on 2026-<month>-<day>."""
    return datetime(2026, month, day, hour, minute, tzinfo=pa.IST).timestamp()


def _tracker(**kw) -> tr.EquityTracker:
    return tr.EquityTracker(starting_cash=1_000_000.0, charges_bps=0.0, **kw)


# --------------------------------------------------------------------------- #
# 1. Session buckets
# --------------------------------------------------------------------------- #
def test_bucket_boundaries():
    _check(pa.bucket_for(_ist(25, 9, 20)) == "OPEN", "09:20 is OPEN")
    _check(pa.bucket_for(_ist(25, 9, 59)) == "OPEN", "09:59 is OPEN")
    _check(pa.bucket_for(_ist(25, 10, 0)) == "MID", "10:00 starts MID")
    _check(pa.bucket_for(_ist(25, 13, 59)) == "MID", "13:59 is MID")
    _check(pa.bucket_for(_ist(25, 14, 0)) == "CLOSE", "14:00 starts CLOSE")
    _check(pa.bucket_for(_ist(25, 15, 29)) == "CLOSE", "15:29 is CLOSE")
    _check(pa.bucket_for(_ist(25, 15, 30)) == "AFTER", "15:30 is AFTER")


def test_bucket_pre_open_is_flagged_separately():
    """Pre-open auction fills have different economics; never fold into OPEN."""
    _check(pa.bucket_for(_ist(25, 9, 0)) == "PRE", "09:00 is PRE")
    _check(pa.bucket_for(_ist(25, 3, 0)) == "PRE", "03:00 is PRE")


def test_bucket_is_ist_not_utc():
    """14:00 IST = 08:30 UTC. A UTC-based bucket would call it OPEN."""
    ts = _ist(25, 14, 0)
    _check(pa.bucket_for(ts) == "CLOSE",
           f"14:00 IST must be CLOSE, got {pa.bucket_for(ts)}")


def test_bucket_weekend_and_bad_timestamps():
    # 2026-09-26 is a Saturday
    _check(pa.bucket_for(_ist(26, 11, 0)) == "WEEKEND", "Saturday is WEEKEND")
    _check(pa.bucket_for(None) == "UNKNOWN", "no ts -> UNKNOWN")


# --------------------------------------------------------------------------- #
# 2. P&L by sleeve
# --------------------------------------------------------------------------- #
def test_by_sleeve_splits_gross_and_fees():
    t = _tracker()
    t.on_fill("NIFTY", "NSE_INDEX", "BUY", 10, 100.0, ts=_ist(25, 9, 30))
    t.on_fill("NIFTY", "NSE_INDEX", "SELL", 10, 110.0, ts=_ist(25, 9, 45))
    t.on_fill("IT", "NSE_INDEX", "BUY", 5, 200.0, ts=_ist(25, 14, 10))
    t.on_fill("IT", "NSE_INDEX", "SELL", 5, 190.0, ts=_ist(25, 14, 30))

    out = pa.by_sleeve(t)
    _check(out["ok"] is True, "attribution should run")
    _check(out["groups"]["NIFTY"]["realized_pnl"] == 100.0,
           f"NIFTY won 100, got {out['groups']['NIFTY']['realized_pnl']}")
    _check(out["groups"]["IT"]["realized_pnl"] == -50.0,
           f"IT lost 50, got {out['groups']['IT']['realized_pnl']}")
    _check(out["total_realized"] == 50.0, f"net 50 total, got {out['total_realized']}")


def test_by_sleeve_orders_best_first():
    t = _tracker()
    t.on_fill("LOSER", "NSE", "BUY", 10, 100.0, ts=_ist(25, 10, 0))
    t.on_fill("LOSER", "NSE", "SELL", 10, 90.0, ts=_ist(25, 10, 5))
    t.on_fill("WINNER", "NSE", "BUY", 10, 100.0, ts=_ist(25, 10, 10))
    t.on_fill("WINNER", "NSE", "SELL", 10, 130.0, ts=_ist(25, 10, 15))
    out = pa.by_sleeve(t)
    _check(list(out["groups"])[0] == "WINNER",
           f"best net P&L first, got {list(out['groups'])}")


def test_by_sleeve_separates_gross_edge_from_cost_problem():
    """Gross-positive but net-negative = a COST problem, not a signal problem."""
    t = tr.EquityTracker(starting_cash=1_000_000.0, charges_bps=50.0)
    t.on_fill("X", "NSE", "BUY", 100, 100.0, ts=_ist(25, 10, 0))
    t.on_fill("X", "NSE", "SELL", 100, 100.5, ts=_ist(25, 10, 5))
    out = pa.by_sleeve(t)
    g = out["groups"]["X"]
    _check(g["realized_pnl"] == 50.0, f"gross edge is 50, got {g['realized_pnl']}")
    _check(g["net_pnl"] < 0,
           f"at 50bps/side the trade must be net-negative, got {g['net_pnl']}")
    _check(g["fees"] > 50.0, f"fees should exceed the edge: {g['fees']}")


# --------------------------------------------------------------------------- #
# 3. P&L by session bucket
# --------------------------------------------------------------------------- #
def test_by_bucket_separates_open_from_close():
    t = _tracker()
    # Winning trade in the opening hour
    t.on_fill("A", "NSE", "BUY", 10, 100.0, ts=_ist(25, 9, 30))
    t.on_fill("A", "NSE", "SELL", 10, 110.0, ts=_ist(25, 9, 45))
    # Losing trade into the close
    t.on_fill("B", "NSE", "BUY", 10, 100.0, ts=_ist(25, 14, 10))
    t.on_fill("B", "NSE", "SELL", 10, 95.0, ts=_ist(25, 14, 30))

    out = pa.by_bucket(t)
    _check(out["buckets"]["OPEN"]["net_pnl"] == 100.0,
           f"opening-hour P&L, got {out['buckets'].get('OPEN')}")
    _check(out["buckets"]["CLOSE"]["net_pnl"] == -50.0,
           f"closing P&L, got {out['buckets'].get('CLOSE')}")


def test_by_bucket_preserves_session_order():
    t = _tracker()
    t.on_fill("A", "NSE", "BUY", 1, 100.0, ts=_ist(25, 14, 10))
    t.on_fill("A", "NSE", "SELL", 1, 101.0, ts=_ist(25, 14, 20))
    t.on_fill("B", "NSE", "BUY", 1, 100.0, ts=_ist(25, 9, 30))
    t.on_fill("B", "NSE", "SELL", 1, 110.0, ts=_ist(25, 9, 40))
    out = pa.by_bucket(t)
    _check(list(out["buckets"]) == ["OPEN", "CLOSE"],
           f"buckets must be in session order, not P&L order: {list(out['buckets'])}")


def test_by_bucket_flags_after_hours_fills():
    """A fill after 15:30 IST is impossible for cash equity — must be flagged."""
    t = _tracker()
    t.on_fill("A", "NSE", "BUY", 10, 100.0, ts=_ist(25, 16, 0))
    t.on_fill("A", "NSE", "SELL", 10, 105.0, ts=_ist(25, 16, 30))
    out = pa.by_bucket(t)
    _check("AFTER" in out["buckets"], "the AFTER bucket must be visible")
    _check(any("AFTER 15:30" in w for w in out["warnings"]),
           f"after-hours fills must raise a warning: {out['warnings']}")


# --------------------------------------------------------------------------- #
# 4. P&L by strategy
# --------------------------------------------------------------------------- #
def test_by_strategy_groups_with_a_labeller():
    class _F:
        def __init__(self, sym, side, qty, px, ts):
            self.symbol, self.exchange, self.side = sym, "NSE", side
            self.qty, self.price, self.ts = qty, px, ts
            self.charges = 0.0

    t = _tracker()
    t.on_fill("A", "NSE", "BUY", 10, 100.0, ts=_ist(25, 10, 0))
    t.on_fill("A", "NSE", "SELL", 10, 110.0, ts=_ist(25, 10, 5))

    def lab(f):
        return "POV" if f.symbol == "A" else "MANUAL"

    out = pa.by_strategy(t, strategy_of=lab)
    _check("POV" in out["groups"], f"labelled group missing: {out['groups']}")


def test_by_strategy_warns_when_unlabelled():
    t = _tracker()
    t.on_fill("A", "NSE", "BUY", 1, 100.0, ts=_ist(25, 10, 0))
    t.on_fill("A", "NSE", "SELL", 1, 101.0, ts=_ist(25, 10, 5))
    out = pa.by_strategy(t)
    _check(any("no strategy labels" in w for w in out["warnings"]),
           f"unlabelled fills must be flagged: {out['warnings']}")


# --------------------------------------------------------------------------- #
# 5. Reconciliation — the guard that keeps attribution honest
# --------------------------------------------------------------------------- #
def test_reconcile_detects_a_tampered_section():
    outer = {"total_net": 100.0,
             "groups": {"A": {"net_pnl": 60.0}, "B": {"net_pnl": 30.0}}}
    out = pa.reconcile(outer, key="groups")
    _check(out["ok"] is False, "60 + 30 != 100 must be caught")
    _check(abs(out["residual"] + 10.0) < 1e-9,
           f"residual should be -10, got {out['residual']}")


def test_reconcile_accepts_an_exact_split():
    outer = {"total_net": 100.0,
             "groups": {"A": {"net_pnl": 60.0}, "B": {"net_pnl": 40.0}}}
    _check(pa.reconcile(outer, key="groups")["ok"] is True,
           "an exact split must reconcile")


def test_every_decomposition_reconciles():
    """The core guarantee: all three views sum to the same total."""
    t = _tracker()
    t.on_fill("A", "NSE", "BUY", 10, 100.0, ts=_ist(25, 9, 30))
    t.on_fill("A", "NSE", "SELL", 10, 110.0, ts=_ist(25, 9, 45))
    t.on_fill("B", "NSE", "BUY", 10, 50.0, ts=_ist(25, 12, 0))
    t.on_fill("B", "NSE", "SELL", 10, 45.0, ts=_ist(25, 12, 30))
    t.on_fill("C", "NSE", "BUY", 4, 200.0, ts=_ist(25, 14, 30))
    t.on_fill("C", "NSE", "SELL", 4, 210.0, ts=_ist(25, 14, 45))

    out = pa.attribute(t)
    _check(out["ok"] is True,
           f"all decompositions must reconcile: {out['warnings']}")
    for name in ("by_sleeve", "by_bucket", "by_strategy"):
        chk = out["reconciliation"][name]
        _check(chk["ok"] is True,
               f"{name} residual {chk['residual']} should be ~0")
    # And the three views must agree with each other.
    totals = {out["by_sleeve"]["total_net"], out["by_bucket"]["total_net"],
              out["by_strategy"]["total_net"]}
    _check(len(totals) == 1,
           f"all views must report the same net P&L, got {totals}")


def test_attribute_reports_fill_count():
    t = _tracker()
    t.on_fill("A", "NSE", "BUY", 1, 100.0, ts=_ist(25, 10, 0))
    t.on_fill("A", "NSE", "SELL", 1, 101.0, ts=_ist(25, 10, 5))
    out = pa.attribute(t)
    _check(out["n_fills"] == 2, f"fill count, got {out['n_fills']}")


# --------------------------------------------------------------------------- #
# 6. Degradation
# --------------------------------------------------------------------------- #
def test_empty_tracker_degrades_cleanly():
    t = _tracker()
    out = pa.attribute(t)
    _check(out["ok"] is True, "an empty book is not an error")
    _check(out["n_fills"] == 0, "no fills")
    _check(out["total_net"] == 0.0, "no P&L")


def test_attribute_never_raises_on_a_broken_tracker():
    class _Broken:
        def fills(self):
            raise RuntimeError("boom")

    out = pa.attribute(_Broken())
    _check(out["ok"] is False, "a broken tracker -> ok False, not a raise")
    _check(out["warnings"], "should record the failure")


def test_open_position_has_no_realised_pnl():
    """Holding a position is not P&L; only a close realises."""
    t = _tracker()
    t.on_fill("A", "NSE", "BUY", 10, 100.0, ts=_ist(25, 10, 0))
    out = pa.by_sleeve(t)
    _check(out["groups"]["A"]["net_pnl"] == 0.0,
           f"an open position realises nothing, got {out['groups']['A']['net_pnl']}")


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