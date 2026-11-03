"""Paper runner: signals CSV -> Risk gate -> approved queue -> paper broker -> ledger.

No capital, no network. Restarting with the same --out folder does not resend old intents.
Usage: python scripts/run_paper.py --signals signals.csv --limits config/risk_limits.yaml --out data/paper

Flow stage: Paper trading stage
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/paper_broker.py
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from quant.audit.log import AuditLog  # noqa: E402
from quant.core.config import RiskLimits  # noqa: E402
from quant.core.types import TradeIntent  # noqa: E402
from quant.execution.openalgo_client import PaperBroker  # noqa: E402
from quant.execution.order_fsm import OrderFSM, OrderState  # noqa: E402
from quant.execution.queue import ApprovedOrderQueue  # noqa: E402
from quant.monitor.ledger import Ledger  # noqa: E402
from quant.monitor.safe_mode import SafeMode  # noqa: E402
from quant.risk.gate import RiskState, check_intent  # noqa: E402
from quant.risk.kill_switch import KillSwitch  # noqa: E402

OPTIONAL_NUMERIC = ("prev_close", "circuit_pct", "adv_shares")


def _intent_from_row(row: dict) -> TradeIntent:
    clean = {}
    for k, v in row.items():
        if k in OPTIONAL_NUMERIC:
            clean[k] = None if pd.isna(v) else float(v)
        elif k == "asm_gsm":
            clean[k] = bool(v) if not pd.isna(v) else False
        elif k in ("kind",):
            clean[k] = "entry" if pd.isna(v) else str(v)
        elif k == "lot_size":
            clean[k] = 1 if pd.isna(v) else int(v)
        elif k in ("qty",):
            clean[k] = int(v)
        elif k == "ref_price":
            clean[k] = float(v)
        else:
            clean[k] = str(v)
    return TradeIntent(**clean)


def run(signals_csv, limits_path, out_dir) -> dict:
    limits = RiskLimits.from_yaml(limits_path)
    out = pathlib.Path(out_dir)
    audit = AuditLog(out / "audit.jsonl")
    ledger = Ledger(out / "ledger.jsonl")
    kill = KillSwitch(out / "kill_switch.json")
    safe = SafeMode(audit)
    state = RiskState(seen_ids=ledger.seen_intent_ids())
    queue, broker = ApprovedOrderQueue(), PaperBroker()
    summary = {"approved": 0, "blocked": 0, "filled": 0, "skipped_duplicates": 0}

    for row in pd.read_csv(signals_csv, dtype={"intent_id": str}).to_dict("records"):
        intent = _intent_from_row(row)
        if intent.intent_id in state.seen_ids:
            summary["skipped_duplicates"] += 1
            continue
        decision = check_intent(intent, state, limits, kill, safe)
        audit.append("decision", {"intent_id": intent.intent_id, "approved": decision.approved,
                                  "reasons": list(decision.reasons)})
        if not decision.approved:
            summary["blocked"] += 1
            continue
        summary["approved"] += 1
        state.seen_ids.add(intent.intent_id)
        ledger.append("intent", intent_id=intent.intent_id, symbol=intent.symbol, side=intent.side,
                      qty=intent.qty, strategy_id=intent.strategy_id)
        queue.push(intent, decision)
        order = queue.pop()
        fsm = OrderFSM(order.intent_id, order.qty)
        fsm.move(OrderState.SUBMITTED)
        result = broker.place(order)
        fsm.move(OrderState.FILLED, result["filled_qty"])
        ledger.append("fill", intent_id=order.intent_id, symbol=order.symbol, signed_qty=order.signed_qty,
                      price=result["avg_price"], order_id=result["order_id"])
        audit.append("fill", {"intent_id": order.intent_id, "order_id": result["order_id"],
                              "state": fsm.state.value, "price": result["avg_price"]})
        summary["filled"] += 1
        flow = order.notional if order.kind == "entry" else -order.notional
        state.open_notional = max(0.0, state.open_notional + flow)

    summary["positions"] = ledger.positions()
    summary["audit_verified"] = audit.verify()
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--signals", required=True)
    p.add_argument("--limits", default=str(ROOT / "config" / "risk_limits.yaml"))
    p.add_argument("--out", default=str(ROOT / "data" / "paper"))
    args = p.parse_args()
    print(json.dumps(run(args.signals, args.limits, args.out), indent=2, default=str))


if __name__ == "__main__":
    main()
