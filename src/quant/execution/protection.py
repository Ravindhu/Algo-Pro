"""Protective stop placed right after a fill. It is an exit intent, so it still goes through the gate.

Flow stage: Protection
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/protective_orders.py
"""
from __future__ import annotations

from quant.core.types import TradeIntent


def protective_stop_intent(entry_intent_id: str, symbol: str, strategy_id: str,
                           position_qty: int, stop_price: float) -> TradeIntent:
    """position_qty > 0 is a long, so the stop sells. A short position would buy back."""
    if position_qty == 0:
        raise ValueError("no position to protect")
    side = "SELL" if position_qty > 0 else "BUY"
    return TradeIntent(
        intent_id=f"{entry_intent_id}-STOP",
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=abs(position_qty),
        ref_price=stop_price,
        kind="protection",
    )
