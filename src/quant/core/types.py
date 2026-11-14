"""Shared types: trade intents and gate decisions.

Flow stage: Trade intent
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/risk_context.py
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Optional, Tuple

Side = Literal["BUY", "SELL"]
Kind = Literal["entry", "exit", "protection"]


@dataclass(frozen=True)
class TradeIntent:
    """One proposed order. Only the Risk gate may approve it."""

    intent_id: str
    strategy_id: str
    symbol: str
    side: Side
    qty: int
    ref_price: float
    kind: Kind = "entry"
    lot_size: int = 1
    prev_close: Optional[float] = None
    circuit_pct: Optional[float] = None
    adv_shares: Optional[float] = None
    asm_gsm: bool = False
    trade_day: Optional[date] = None  # needed for the event-blackout check on entries
    algo_id: Optional[str] = None  # exchange-approved algo ID, when one is configured

    @property
    def notional(self) -> float:
        return self.qty * self.ref_price

    @property
    def signed_qty(self) -> int:
        return self.qty if self.side == "BUY" else -self.qty


@dataclass(frozen=True)
class GateDecision:
    """Result of the Risk gate. Only the gate builds approved decisions."""

    intent_id: str
    approved: bool
    reasons: Tuple[str, ...] = ()
