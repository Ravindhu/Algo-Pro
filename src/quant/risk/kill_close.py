"""Kill-switch flatten: one exit intent per open position.

The exit intents are ordinary TradeIntents. They go through the Risk gate and the approved queue
like every other order. The gate lets exits through while the kill switch is on.

Rules:
- Side is the opposite of the position. Qty is the full position.
- Intent id is KILL-<SYMBOL>-1. If that id is already in the ledger and the position is still open,
  the earlier close is not confirmed. That symbol is skipped and needs a human check (fail closed).
- A position with no reference price is skipped, never priced from a guess.

Flow stage: Kill switch
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): none. Orders go through risk/gate.py and execution/queue.py.
"""
from __future__ import annotations

from typing import Iterable, List, Mapping, Tuple

from quant.core.types import TradeIntent

KILL_STRATEGY_ID = "KILL"


def kill_exit_intents(
    positions: Mapping[str, int],
    ref_prices: Mapping[str, float],
    ledger_intent_ids: Iterable[str],
) -> Tuple[List[TradeIntent], List[Tuple[str, str]]]:
    """Return (exit intents, skipped). positions: symbol -> signed qty. ref_prices: symbol -> last price."""
    taken = set(ledger_intent_ids)
    intents: List[TradeIntent] = []
    skipped: List[Tuple[str, str]] = []
    for symbol in sorted(positions):
        qty = int(positions[symbol])
        if qty == 0:
            continue
        intent_id = f"KILL-{symbol}-1"
        if intent_id in taken:
            skipped.append((symbol, "earlier kill close not confirmed flat: check the broker"))
            continue
        px = ref_prices.get(symbol)
        if px is None or px <= 0:
            skipped.append((symbol, "no reference price"))
            continue
        intents.append(TradeIntent(
            intent_id=intent_id,
            strategy_id=KILL_STRATEGY_ID,
            symbol=symbol,
            side="SELL" if qty > 0 else "BUY",
            qty=abs(qty),
            ref_price=float(px),
            kind="exit",
        ))
    return intents, skipped
