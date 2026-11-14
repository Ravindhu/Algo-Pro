"""Order state machine. Idempotent: one intent ID is one order, however many retries.

Flow stage: Order state machine
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/order_updates.py
"""
from __future__ import annotations

from enum import Enum


class OrderState(str, Enum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


_ALLOWED = {
    OrderState.NEW: {OrderState.SUBMITTED, OrderState.REJECTED},
    OrderState.SUBMITTED: {OrderState.ACCEPTED, OrderState.PARTIAL, OrderState.FILLED, OrderState.REJECTED},
    OrderState.ACCEPTED: {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED},
    OrderState.PARTIAL: {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED},
    OrderState.FILLED: set(),
    OrderState.REJECTED: set(),
    OrderState.CANCELLED: set(),
}


class InvalidTransition(RuntimeError):
    pass


class OrderFSM:
    def __init__(self, intent_id: str, qty: int) -> None:
        self.intent_id = intent_id
        self.qty = qty
        self.filled_qty = 0
        self.state = OrderState.NEW

    def move(self, new: OrderState, filled_qty: int = None) -> None:
        # Retry-safe: validate everything first, change nothing on refusal.
        new = OrderState(new)
        if filled_qty is not None and not 0 <= filled_qty <= self.qty:
            raise ValueError("filled_qty out of range")
        fill = self.filled_qty if filled_qty is None else filled_qty
        if fill < self.filled_qty:
            raise ValueError(f"{self.intent_id}: fill cannot go down (stale report)")
        if new is self.state:
            # Repeated report. Same fill is a no-op; a changed fill is refused, except PARTIAL progress.
            if fill != self.filled_qty and new is not OrderState.PARTIAL:
                raise ValueError(f"{self.intent_id}: conflicting repeat of {new.value}")
            self.filled_qty = fill
            return
        if new not in _ALLOWED[self.state]:
            raise InvalidTransition(f"{self.intent_id}: {self.state.value} -> {new.value}")
        if new is OrderState.FILLED and fill != self.qty:
            raise ValueError("FILLED needs filled_qty == qty")
        self.filled_qty = fill
        self.state = new
