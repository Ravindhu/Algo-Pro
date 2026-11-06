"""Kill switch closes every open position through the Risk gate (decision 0020)."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from quant.monitor.ledger import Ledger  # noqa: E402
from quant.reporting.dashboard_data import safety_events  # noqa: E402
from quant.risk.kill_close import kill_exit_intents  # noqa: E402
import run_paper  # noqa: E402

LIMITS = ROOT / "config" / "risk_limits.yaml"
HEADER = "intent_id,strategy_id,symbol,side,qty,ref_price,kind\n"


def _signals(tmp_path):
    p = tmp_path / "signals.csv"
    p.write_text(HEADER, encoding="utf-8")  # no new signals: only the kill close runs
    return p


def _seed(out, positions):
    """positions: list of (symbol, signed_qty, price). Writes an intent and a fill for each."""
    ledger = Ledger(out / "ledger.jsonl")
    for i, (sym, q, px) in enumerate(positions):
        iid = f"SIG-{i}"
        ledger.append("intent", intent_id=iid, symbol=sym, side="BUY" if q > 0 else "SELL",
                      qty=abs(q), strategy_id="S1")
        ledger.append("fill", intent_id=iid, symbol=sym, signed_qty=q, price=px, order_id=f"O{i}")


def _trip(out, reason="test"):
    (out / "kill_switch.json").write_text(
        json.dumps({"active": True, "reason": f"automatic: {reason}"}), encoding="utf-8")


def test_long_is_sold_and_short_is_bought():
    intents, skipped = kill_exit_intents({"A": 5, "B": -3}, {"A": 100.0, "B": 50.0}, [])
    assert skipped == []
    assert [(i.symbol, i.side, i.qty, i.kind, i.strategy_id) for i in intents] == [
        ("A", "SELL", 5, "exit", "KILL"), ("B", "BUY", 3, "exit", "KILL")]


def test_zero_position_and_missing_price_are_skipped():
    intents, skipped = kill_exit_intents({"A": 0, "B": 4}, {}, [])
    assert intents == []
    assert skipped == [("B", "no reference price")]


def test_earlier_close_not_confirmed_blocks_a_second_attempt():
    intents, skipped = kill_exit_intents({"A": 5}, {"A": 100.0}, ["KILL-A-1"])
    assert intents == []
    assert skipped and "not confirmed" in skipped[0][1]


def test_kill_switch_closes_all_positions_in_a_paper_run(tmp_path):
    out = tmp_path / "paper"
    _seed(out, [("A", 5, 100.0), ("B", -3, 50.0)])
    _trip(out)
    s = run_paper.run(_signals(tmp_path), LIMITS, out)
    assert s["kill_closes"] == 2
    assert s["filled"] == 2
    assert s["positions"] == {}
    assert s["audit_verified"] is True
    closes = [r for r in run_paper.AuditLog(out / "audit.jsonl").read_all() if r["kind"] == "kill_close"]
    assert len(closes) == 1 and closes[0]["payload"]["closed"] == ["A", "B"]


def test_restart_does_not_close_twice(tmp_path):
    out = tmp_path / "paper"
    _seed(out, [("A", 5, 100.0)])
    _trip(out)
    run_paper.run(_signals(tmp_path), LIMITS, out)
    s = run_paper.run(_signals(tmp_path), LIMITS, out)
    assert s["kill_closes"] == 0 and s["filled"] == 0 and s["positions"] == {}


def test_no_kill_switch_means_no_close(tmp_path):
    out = tmp_path / "paper"
    _seed(out, [("A", 5, 100.0)])
    s = run_paper.run(_signals(tmp_path), LIMITS, out)
    assert s["kill_closes"] == 0 and s["filled"] == 0
    assert s["positions"] == {"A": 5}


def test_unconfirmed_earlier_close_is_skipped_in_a_run(tmp_path):
    out = tmp_path / "paper"
    _seed(out, [("A", 5, 100.0)])
    Ledger(out / "ledger.jsonl").append("intent", intent_id="KILL-A-1", symbol="A", side="SELL",
                                        qty=5, strategy_id="KILL")
    _trip(out)
    s = run_paper.run(_signals(tmp_path), LIMITS, out)
    assert s["kill_closes"] == 0 and s["filled"] == 0
    assert s["positions"] == {"A": 5}


def test_dashboard_shows_kill_close_as_critical():
    recs = [{"kind": "kill_close", "ts": "t1",
             "payload": {"reason": "closed 2 position(s): A, B", "closed": ["A", "B"]}}]
    ev = safety_events(recs, {"active": False})
    assert ev[0]["severity"] == "CRITICAL"
    assert ev[0]["kind"] == "kill_close"
    assert "closed 2 position(s)" in ev[0]["message"]
