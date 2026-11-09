"""Paper runner: signals CSV -> Risk gate -> approved queue -> broker -> ledger.

No capital. Default broker is the in-process paper broker (no network).
--broker analyze sends through OpenAlgo analyzer mode (sandbox). The client refuses unless
OpenAlgo confirms analyze mode is on (docs/ANALYZER_MODE_PLAN.md).
Restarting with the same --out folder does not resend old intents.
If the kill switch is on, every open position gets an exit intent (src/quant/risk/kill_close.py).

Usage:
  python scripts/run_paper.py --signals signals.csv --out data/paper
  python scripts/run_paper.py --signals signals.csv --broker analyze --base-url <OpenAlgo URL> --out data/analyze
  (analyze needs OPENALGO_API_KEY in the environment or the repo-root .env)

Flow stage: Paper trading stage, Kill switch
Status: PHASE 1 IMPLEMENTED; analyze broker added 2026-11-06 (decision 0026, AZ2); kill close added 2026-11-06 (decision 0020)
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
from quant.execution.openalgo_client import (  # noqa: E402
    AnalyzeBroker, OpenAlgoClient, PaperBroker, http_transport,
)
from quant.execution.order_fsm import OrderFSM, OrderState  # noqa: E402
from quant.execution.queue import ApprovedOrderQueue  # noqa: E402
from quant.execution.live_inputs import LiveReaders  # noqa: E402
from quant.execution.readonly_client import OpenAlgoReadOnly  # noqa: E402
from quant.monitor.ledger import Ledger  # noqa: E402
from quant.notify.alerts import Alerts  # noqa: E402
from quant.risk.controls import HaltHandler  # noqa: E402
from quant.monitor.safe_mode import SafeMode  # noqa: E402
from quant.reporting.dashboard_data import fifo_pnl  # noqa: E402
from quant.risk.auto_kill import daily_loss_breach  # noqa: E402
from quant.risk.gate import RiskState, check_intent  # noqa: E402
from quant.risk.kill_close import kill_exit_intents  # noqa: E402
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


def _last_fill_prices(ledger: Ledger) -> dict:
    """Last filled price per symbol. Used as the reference price for kill closes (paper only)."""
    prices = {}
    for e in ledger.events():
        if e.get("event") == "fill":
            prices[e["symbol"]] = float(e["price"])
    return prices


def _open_symbols(ledger: Ledger) -> set:
    """Symbols with a non-zero net filled quantity. Used by the symbol cap in the Risk gate."""
    net: dict = {}
    for e in ledger.events():
        if e.get("event") == "fill":
            net[e["symbol"]] = net.get(e["symbol"], 0) + int(e["signed_qty"])
    return {sym for sym, qty in net.items() if qty != 0}


def _close_reason(closed: list, skipped: list) -> str:
    text = f"closed {len(closed)} position(s): {', '.join(closed) or 'none'}"
    if skipped:
        text += "; not closed: " + ", ".join(f"{s} ({why})" for s, why in skipped)
    return text


def run(signals_csv, limits_path, out_dir, broker=None, readers=None, *,
        client=None, api_key: str = "") -> dict:
    """Run one signals file. broker: any object with place(intent, decision) -> result dict.

    Result dict keys: status (FILLED / REJECTED / UNKNOWN), order_id, filled_qty, avg_price, reason.
    Only FILLED changes the ledger position and exposure. REJECTED and UNKNOWN are audited.

    ``client``/``api_key`` (live only, decision 0030): when given, every approved intent is
    additionally confirmed against the broker session guard before the broker sees it. The
    guards themselves live in execution/openalgo_client.py; the runner just refuses early
    and records why.
    """
    limits = RiskLimits.from_yaml(limits_path)
    out = pathlib.Path(out_dir)
    audit = AuditLog(out / "audit.jsonl")
    ledger = Ledger(out / "ledger.jsonl")
    kill = KillSwitch(out / "kill_switch.json")
    safe = SafeMode(audit)
    state = RiskState(seen_ids=ledger.seen_intent_ids())
    halt = HaltHandler()  # index circuit freezes; empty until a halt or index-move freeze is set
    alerts = Alerts(on_critical=lambda msg: audit.append("critical", {"message": msg}))
    if broker is not None and hasattr(broker, "attach_alerts"):
        broker.attach_alerts(alerts)
    queue = ApprovedOrderQueue()
    if broker is None:
        broker = PaperBroker()
    summary = {"approved": 0, "blocked": 0, "filled": 0, "rejected": 0, "unknown": 0, "skipped_duplicates": 0,
               "kill_closes": 0, "auto_kill": None}
    flattened = False

    def _live_guard(intent: TradeIntent) -> None:
        """Live only: refuse before the queue when the client's session guard would."""
        if client is None:
            return
        client._check_session_guard(api_key)   # raises PermissionError when not validly approved

    def _submit(intent: TradeIntent) -> None:
        state.open_symbols = _open_symbols(ledger)
        funds = readers.funds() if (readers is not None and intent.kind == "entry") else None
        decision = check_intent(intent, state, limits, kill, safe, available_funds=funds, halt=halt)
        audit.append("decision", {"intent_id": intent.intent_id, "approved": decision.approved,
                                  "reasons": list(decision.reasons)})
        if not decision.approved:
            summary["blocked"] += 1
            return
        try:
            _live_guard(intent)
        except PermissionError as exc:
            summary["blocked"] += 1
            audit.append("live_guard_refused", {"intent_id": intent.intent_id,
                                                "reason": str(exc)})
            return
        summary["approved"] += 1
        state.seen_ids.add(intent.intent_id)
        ledger.append("intent", intent_id=intent.intent_id, symbol=intent.symbol, side=intent.side,
                      qty=intent.qty, strategy_id=intent.strategy_id)
        queue.push(intent, decision)
        order = queue.pop()
        fsm = OrderFSM(order.intent_id, order.qty)
        fsm.move(OrderState.SUBMITTED)
        result = broker.place(order, decision)
        status = result.get("status")
        if status == "FILLED":
            fsm.move(OrderState.FILLED, result["filled_qty"])
            ledger.append("fill", intent_id=order.intent_id, symbol=order.symbol, signed_qty=order.signed_qty,
                          price=result["avg_price"], order_id=result["order_id"])
            summary["filled"] += 1
            flow = order.notional if order.kind == "entry" else -order.notional
            state.open_notional = max(0.0, state.open_notional + flow)
            _check_auto_kill()
        elif status == "REJECTED":
            fsm.move(OrderState.REJECTED)
            summary["rejected"] += 1
        else:
            # UNKNOWN: the order state is not confirmed. Stays SUBMITTED; no resend (intent is in the ledger).
            summary["unknown"] += 1
        audit.append("broker_result", {"intent_id": order.intent_id, "order_id": result.get("order_id", ""),
                                       "status": status, "state": fsm.state.value,
                                       "reason": result.get("reason", "")})

    def _flatten_if_killed() -> None:
        """Once per run: when the kill switch is on, send an exit for every open position."""
        nonlocal flattened
        if flattened or not kill.active:
            return
        flattened = True
        intents, skipped = kill_exit_intents(ledger.positions(), _last_fill_prices(ledger),
                                             ledger.seen_intent_ids())
        closed = [i.symbol for i in intents]
        audit.append("kill_close", {"reason": _close_reason(closed, skipped), "closed": closed,
                                    "skipped": [{"symbol": s, "why": w} for s, w in skipped],
                                    "kill_reason": kill.reason})
        summary["kill_closes"] = len(intents)
        for intent in intents:
            _submit(intent)

    def _check_auto_kill() -> None:
        """Trip the kill switch on a daily realized-loss breach (decision 0020)."""
        if kill.active:
            return
        fills = [e for e in ledger.events() if e.get("event") == "fill"]
        realized = sum(t["pnl"] for t in fifo_pnl(fills)["closed"])
        reason = daily_loss_breach(realized, limits.max_daily_loss)
        if reason:
            kill.trip(reason, source="automatic")
            audit.append("kill_switch", {"reason": reason, "source": "automatic"})
            summary["auto_kill"] = reason

    _flatten_if_killed()
    for row in pd.read_csv(signals_csv, dtype={"intent_id": str}).to_dict("records"):
        _flatten_if_killed()
        intent = _intent_from_row(row)
        if intent.intent_id in state.seen_ids:
            summary["skipped_duplicates"] += 1
            continue
        _submit(intent)
    _flatten_if_killed()  # a trip on the last fill still closes positions

    summary["positions"] = ledger.positions()
    summary["audit_verified"] = audit.verify()
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--signals", required=True)
    p.add_argument("--limits", default=str(ROOT / "config" / "risk_limits.yaml"))
    p.add_argument("--out", default=str(ROOT / "data" / "paper"))
    p.add_argument("--broker", choices=("paper", "analyze"), default="paper")
    p.add_argument("--base-url", default="", help="OpenAlgo base URL; required with --broker analyze")
    args = p.parse_args()

    broker = None
    if args.broker == "analyze":
        if not args.base_url:
            p.error("--broker analyze needs --base-url")
        client = OpenAlgoClient(args.base_url, http_transport, mode="analyze")
        key = OpenAlgoClient.api_key_from_env()
        readers = LiveReaders(OpenAlgoReadOnly(args.base_url, http_transport), key)
        broker = AnalyzeBroker(client, key, order_not_placed=readers.not_placed,
                               position_still_open=readers.still_open)
    print(json.dumps(run(args.signals, args.limits, args.out, broker=broker,
                         readers=readers if args.broker == "analyze" else None), indent=2, default=str))


if __name__ == "__main__":
    main()
