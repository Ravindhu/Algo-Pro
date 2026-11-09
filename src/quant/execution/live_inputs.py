"""Read-only live inputs: available funds, position per symbol, open orders. All reads, no orders.

Flow stage: Cash and margin manager, Risk gate, Order state machine
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): none (new, flow items 10, 6)

Fail closed: any read error or unexpected reply counts as "not safe":
- funds error -> available funds 0 (entries refused),
- open-order or position error -> no exit retry (execution/retry.py).

Assumptions to confirm on a live account:
- positionbook 'quantity' is signed (negative = short). If it is unsigned, buy-to-cover retries are
  refused, which is the safe direction.
- orderbook has no documented intent tag, so a match is by symbol and side. Any open order for the
  same symbol and side blocks the retry.
"""
from __future__ import annotations

from typing import Any, Dict

from quant.core.types import TradeIntent

CLOSED_STATES = {"complete", "completed", "rejected", "cancelled", "canceled"}


def _ok(reply: Any) -> Dict:
    if not isinstance(reply, dict) or reply.get("status") != "success":
        raise ValueError(f"read failed: {reply!r}"[:200])
    return reply


def available_funds(reader, api_key: str) -> float:
    data = _ok(reader.funds(api_key))["data"]
    return float(data["availablecash"])


def position_qty(reader, api_key: str, symbol: str) -> int:
    rows = _ok(reader.positionbook(api_key))["data"]
    return sum(int(float(r["quantity"])) for r in rows if str(r.get("symbol")) == symbol)


def has_open_order(reader, api_key: str, symbol: str, side: str) -> bool:
    orders = _ok(reader.orderbook(api_key))["data"]["orders"]
    for o in orders:
        if str(o.get("symbol")) == symbol and str(o.get("action")).upper() == side:
            if str(o.get("order_status", "")).lower() not in CLOSED_STATES:
                return True
    return False


class LiveReaders:
    """Wraps the read-only client and one API key. Every method fails closed on error."""

    def __init__(self, reader, api_key: str) -> None:
        if not api_key:
            raise ValueError("LiveReaders needs an API key")
        self._reader = reader
        self._key = api_key

    def funds(self) -> float:
        try:
            return available_funds(self._reader, self._key)
        except Exception:
            return 0.0  # no funds known: entries are refused

    def not_placed(self, intent: TradeIntent) -> bool:
        try:
            return not has_open_order(self._reader, self._key, intent.symbol, intent.side)
        except Exception:
            return False  # unknown: do not retry

    def still_open(self, intent: TradeIntent) -> bool:
        try:
            qty = position_qty(self._reader, self._key, intent.symbol)
        except Exception:
            return False  # unknown: do not retry
        if intent.side == "SELL":  # closing a long
            return qty >= intent.qty
        return qty <= -intent.qty  # BUY: closing a short
