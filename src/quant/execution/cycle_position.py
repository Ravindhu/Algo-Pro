"""One position per HTF cycle: quantity, POV fills, pause and resume, average price. Pure logic, no I/O.

Flow stage: Strategy bot framework, Protection, Algo router
Status: PHASE 2 IMPLEMENTED (2026-11-19). Part 2 of 2. Rules: docs/STRATEGY_RULES_2026-11-19.md sections 11 and 12.
Not used by strategy_bot.py: the bot keeps the same state in _Parent and Position (see docs/IMPLEMENTATION_PLAN_2026-11-19_CYCLE.md, known gaps).

Rules:
- Quantity is sized once, at the first trigger: qty = floor(risk_amount / (trigger_price - htf_low)).
- Later triggers in the same cycle keep the same quantity. They add nothing.
- LTF invalidated (candle close below the LTF OCL low): filling pauses. Unfilled quantity is kept.
- Next LTF OCL touch: filling resumes with the remaining quantity.
- HTF invalidated (candle close below the HTF OCL low): the cycle closes. Remaining quantity is dropped.
- Average entry = quantity-weighted average of the actual fill prices.
- Exit mode: 'trail' once a conservative touch happened; otherwise 'target' at average + N x R (R = average - htf_low),
  or 'target_pending' while N is not set by backtest.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


def size_cycle(risk_amount: float, trigger_price: float, htf_low: float) -> int:
    """Quantity for the whole cycle. 0 when the inputs cannot give a valid stop distance."""
    per_share = trigger_price - htf_low
    if risk_amount <= 0 or per_share <= 0 or not math.isfinite(per_share):
        return 0
    return int(math.floor(risk_amount / per_share))


@dataclass
class CyclePosition:
    key: str
    htf_low: float
    max_qty: int
    status: str = "idle"        # idle | filling | paused | complete | closed
    filled: int = 0
    fills: List[Tuple[int, float]] = field(default_factory=list)
    cons_touched: bool = False
    target_r: Optional[float] = None

    @classmethod
    def open_at(cls, key: str, htf_low: float, risk_amount: float, trigger_price: float) -> "CyclePosition":
        return cls(key=key, htf_low=htf_low, max_qty=size_cycle(risk_amount, trigger_price, htf_low))

    @property
    def remaining(self) -> int:
        return max(0, self.max_qty - self.filled)

    @property
    def avg_price(self) -> Optional[float]:
        if self.filled <= 0:
            return None
        return sum(q * p for q, p in self.fills) / self.filled

    def on_trigger(self, conservative: bool = False) -> None:
        """A trigger touch. First trigger starts filling; later triggers keep the same quantity."""
        if self.status in ("closed", "complete"):
            return
        if conservative:
            self.cons_touched = True
        if self.max_qty <= 0:
            return
        if self.status in ("idle", "paused"):
            self.status = "filling"

    def on_ltf_invalidated(self) -> None:
        if self.status == "filling":
            self.status = "paused"

    def record_fill(self, qty: int, price: float) -> None:
        if qty <= 0 or self.status != "filling":
            return  # POV only fills while filling (paused, idle, complete and closed take no fills)
        qty = min(qty, self.remaining)
        if qty <= 0:
            return
        self.fills.append((qty, price))
        self.filled += qty
        if self.remaining == 0:
            self.status = "complete"

    def on_htf_invalidated(self) -> None:
        self.status = "closed"  # remaining quantity is dropped (rules section 12)

    def exit_mode(self) -> Tuple[str, Optional[float]]:
        """('trail', None) after a conservative touch; otherwise ('target', price) or ('target_pending', None)."""
        if self.avg_price is None:
            return ("none", None)
        if self.cons_touched:
            return ("trail", None)
        if self.target_r is None:
            return ("target_pending", None)
        r = self.avg_price - self.htf_low
        return ("target", self.avg_price + self.target_r * r)
