"""Automatic kill trigger on a daily realized-loss breach (decision 0020)."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import run_paper  # noqa: E402
from quant.monitor.ledger import Ledger  # noqa: E402
from quant.risk.auto_kill import daily_loss_breach  # noqa: E402


def test_no_limit_never_trips():
    assert daily_loss_breach(-1e9, None) is None


def test_loss_below_limit_does_not_trip():
    assert daily_loss_breach(-499.0, 500.0) is None


def test_loss_at_limit_trips():
    reason = daily_loss_breach(-500.0, 500.0)
    assert reason and "reached limit" in reason


def test_gain_never_trips():
    assert daily_loss_breach(1000.0, 500.0) is None


def _limits_file(tmp_path, max_daily_loss):
    p = tmp_path / "limits.yaml"
    p.write_text(
        "max_order_quantity: 1000\nmax_order_notional: 5000000\nmax_open_exposure: 20000000\n"
        f"max_daily_loss: {max_daily_loss}\nmax_strategy_loss: 100000\nmax_adv_pct: 0.05\nblocked: false\n",
        encoding="utf-8",
    )
    return p


def _seed(out, fills):
    ledger = Ledger(out / "ledger.jsonl")
    for i, (sym, q, px) in enumerate(fills):
        ledger.append("intent", intent_id=f"SIG-{i}", symbol=sym, side="BUY" if q > 0 else "SELL",
                      qty=abs(q), strategy_id="S1")
        ledger.append("fill", intent_id=f"SIG-{i}", symbol=sym, signed_qty=q, price=px, order_id=f"O{i}")


def _exit_signal(tmp_path, symbol, qty, price):
    p = tmp_path / "signals.csv"
    p.write_text("intent_id,strategy_id,symbol,side,qty,ref_price,kind\n"
                 f"EXIT-{symbol},S1,{symbol},SELL,{qty},{price},exit\n", encoding="utf-8")
    return p


def test_realized_loss_trips_kill_and_closes_the_rest(tmp_path):
    out = tmp_path / "paper"
    # A: long 10 @100, sold at 50 -> realized about -500. B: long 4 @100, still open.
    _seed(out, [("A", 10, 100.0), ("B", 4, 100.0)])
    s = run_paper.run(_exit_signal(tmp_path, "A", 10, 50.0), _limits_file(tmp_path, 400), out)
    assert s["auto_kill"] and "reached limit" in s["auto_kill"]
    assert s["kill_closes"] == 1
    assert s["positions"] == {}
    kinds = [r["kind"] for r in run_paper.AuditLog(out / "audit.jsonl").read_all()]
    assert "kill_switch" in kinds and "kill_close" in kinds
    assert s["audit_verified"] is True


def test_loss_inside_limit_does_not_trip(tmp_path):
    out = tmp_path / "paper"
    _seed(out, [("A", 10, 100.0)])
    s = run_paper.run(_exit_signal(tmp_path, "A", 10, 50.0), _limits_file(tmp_path, 600), out)
    assert s["auto_kill"] is None and s["kill_closes"] == 0
    assert not (out / "kill_switch.json").exists() or '"active": false' in (out / "kill_switch.json").read_text()


def test_trip_on_the_last_fill_still_closes_positions(tmp_path):
    out = tmp_path / "paper"
    _seed(out, [("A", 10, 100.0), ("B", 4, 100.0)])
    # Only one signal, and it is the one that breaches. The final flatten must still run.
    s = run_paper.run(_exit_signal(tmp_path, "A", 10, 50.0), _limits_file(tmp_path, 400), out)
    assert s["kill_closes"] == 1
