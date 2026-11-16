"""The paper runner passes the ledger's held quantity to the gate for exits.

Without it, a SELL exit larger than the position would be approved on this long-only book,
which would open a short.
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from quant.monitor.ledger import Ledger  # noqa: E402
import run_paper  # noqa: E402

LIMITS = ROOT / "config" / "risk_limits.yaml"
HEADER = "intent_id,strategy_id,symbol,side,qty,ref_price,kind\n"


class _RecordingBroker:
    def __init__(self):
        self.placed = []

    def place(self, order, decision):
        self.placed.append(order)
        return {"status": "FILLED", "order_id": "X", "filled_qty": order.qty,
                "avg_price": order.ref_price, "reason": ""}


def _seed_long(out, symbol, qty, price):
    ledger = Ledger(out / "ledger.jsonl")
    ledger.append("intent", intent_id="SEED-1", symbol=symbol, side="BUY", qty=qty, strategy_id="S1")
    ledger.append("fill", intent_id="SEED-1", symbol=symbol, signed_qty=qty, price=price, order_id="SEED")


def _audit_reasons(out):
    rows = [json.loads(line) for line in (out / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return [r["payload"] for r in rows if r.get("kind") == "decision"]


def _exit_csv(tmp_path, qty):
    p = tmp_path / "signals.csv"
    p.write_text(HEADER + f"EXIT-1,S1,RELIANCE,SELL,{qty},100.0,exit\n", encoding="utf-8")
    return p


def test_sell_exit_larger_than_held_is_refused(tmp_path):
    out = tmp_path / "paper"
    out.mkdir()
    _seed_long(out, "RELIANCE", 5, 100.0)
    broker = _RecordingBroker()
    run_paper.run(_exit_csv(tmp_path, qty=10), LIMITS, out, broker=broker)
    assert broker.placed == [], "an oversized SELL exit must not reach the broker"
    decisions = _audit_reasons(out)
    assert decisions and decisions[0]["approved"] is False
    assert any("exceeds held qty" in r for r in decisions[0]["reasons"])


def test_sell_exit_within_held_is_not_refused_for_size(tmp_path):
    out = tmp_path / "paper"
    out.mkdir()
    _seed_long(out, "RELIANCE", 5, 100.0)
    run_paper.run(_exit_csv(tmp_path, qty=5), LIMITS, out, broker=_RecordingBroker())
    decisions = _audit_reasons(out)
    assert decisions
    assert not any("exceeds held qty" in r for r in decisions[0]["reasons"])
