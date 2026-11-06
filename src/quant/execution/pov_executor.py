"""POV child-order executor skeleton (decision 0021). Parent -> child intents -> gate -> queue -> client.

Flow stage: Algo router, Risk gate, Approved order queue, OpenAlgo
Status: PHASE 3 IMPLEMENTED (skeleton; tested with fakes and the real gate; no live runner)

Order path for every child (no step may be skipped):
  1. make_child: build a child TradeIntent from the parent (qty from pov_child_qty).
  2. submit_child: refuse a child id already sent in this executor (fail closed).
  3. gate: the injected gate function (normally risk/gate.py check_intent). Rejected -> audit, stop.
  4. ApprovedOrderQueue.push / pop: only an approved decision for the same intent id gets in.
  5. sender.place_child: the one order client. It checks the live guard again.
  6. audit: every gate result, send and refusal is written to the audit log.

The executor never calls a broker itself and never reads one. The POV loop (timing, stale
cancels, reconciliation) is ported in a later step and will call this class.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Protocol, Set, Tuple

from quant.core.types import GateDecision, TradeIntent
from quant.execution.queue import ApprovedOrderQueue
from quant.execution.router import pov_child_qty

GateFn = Callable[[TradeIntent], GateDecision]


class ChildSender(Protocol):
    def place_child(
        self,
        intent: TradeIntent,
        api_key: str,
        decision: GateDecision,
        price_type: str = "MARKET",
        limit_price: Optional[float] = None,
    ) -> Dict: ...


class AuditSink(Protocol):
    def append(self, kind: str, payload: Dict) -> str: ...


@dataclass(frozen=True)
class ChildOutcome:
    intent_id: str
    sent: bool
    reasons: Tuple[str, ...]
    response: Optional[Dict]


class PovExecutor:
    def __init__(
        self,
        gate: GateFn,
        sender: ChildSender,
        api_key: str,
        audit: AuditSink,
        pov: float = 0.1,
    ) -> None:
        if not api_key:
            raise ValueError("executor needs an API key")
        if not 0 < pov <= 1:
            raise ValueError("pov must be in (0, 1]")
        self._gate = gate
        self._sender = sender
        self._api_key = api_key
        self._audit = audit
        self.pov = pov
        self._queue = ApprovedOrderQueue()
        self._sent_ids: Set[str] = set()

    def child_qty(self, remaining: int, bar_volume: float) -> int:
        """Child size: pov share of the last bar's volume, never more than what is left."""
        return pov_child_qty(remaining, bar_volume, self.pov)

    @staticmethod
    def make_child(parent: TradeIntent, qty: int, seq: int) -> TradeIntent:
        """Child intent: same symbol, side, strategy and kind as the parent. Id = parent id + sequence."""
        if qty < 1:
            raise ValueError("child qty must be at least 1")
        if seq < 1:
            raise ValueError("child sequence starts at 1")
        return TradeIntent(
            intent_id=f"{parent.intent_id}-C{seq:04d}",
            strategy_id=parent.strategy_id,
            symbol=parent.symbol,
            side=parent.side,
            qty=qty,
            ref_price=parent.ref_price,
            kind=parent.kind,
            lot_size=parent.lot_size,
            prev_close=parent.prev_close,
            circuit_pct=parent.circuit_pct,
            adv_shares=parent.adv_shares,
            asm_gsm=parent.asm_gsm,
            trade_day=parent.trade_day,
        )

    def submit_child(
        self,
        child: TradeIntent,
        price_type: str = "MARKET",
        limit_price: Optional[float] = None,
    ) -> ChildOutcome:
        if child.intent_id in self._sent_ids:
            reasons = ("duplicate child intent id in this executor",)
            self._audit.append("child_refused", {"intent_id": child.intent_id, "reasons": list(reasons)})
            return ChildOutcome(child.intent_id, False, reasons, None)

        decision = self._gate(child)
        self._audit.append("gate", {
            "intent_id": child.intent_id, "approved": decision.approved, "reasons": list(decision.reasons),
        })
        if not decision.approved:
            return ChildOutcome(child.intent_id, False, tuple(decision.reasons), None)

        self._queue.push(child, decision)
        queued = self._queue.pop()
        try:
            response = self._sender.place_child(queued, self._api_key, decision, price_type, limit_price)
        except Exception as exc:
            self._audit.append("child_refused", {"intent_id": child.intent_id, "error": repr(exc)})
            raise
        self._sent_ids.add(child.intent_id)
        self._audit.append("child_sent", {
            "intent_id": child.intent_id, "qty": child.qty, "price_type": price_type,
        })
        return ChildOutcome(child.intent_id, True, (), response)
