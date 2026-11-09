"""Self-cross guard for PCAS sessions (decision 0025).

Flow stage: Risk gate
Status: PHASE 3 IMPLEMENTED (pure check; not yet wired into the gate or the POV loop)

Penalty rule (NSE PCAS page): if one client's maximum buy price is at or above its minimum sell
price, and those orders trade, a penalty applies. This module refuses a new order that would
create that state for the same client and symbol.

Market orders: a market buy counts as an infinitely high price, a market sell as infinitely low.
So a market order is refused whenever it could cross an open order on the other side.
Fail closed: invalid prices raise ValueError, they are never treated as zero.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Tuple

BUY, SELL = "BUY", "SELL"
_MARKET_BUY = math.inf
_MARKET_SELL = -math.inf


def _effective_price(side: str, price: Optional[float]) -> float:
    if side not in (BUY, SELL):
        raise ValueError(f"unknown side: {side!r}")
    if price is None:
        return _MARKET_BUY if side == BUY else _MARKET_SELL
    if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        raise ValueError(f"limit price must be finite and positive: {price!r}")
    return float(price)


def would_self_cross(
    side: str,
    price: Optional[float],
    resting: Iterable[Tuple[str, Optional[float]]] = (),
) -> Tuple[bool, str]:
    """Return (True, reason) when the new order would make max buy >= min sell.

    `resting` is the client's open orders for this symbol, as (side, price) pairs. Price None means
    a market order. Returns (False, "") when the order is safe.
    """
    new_price = _effective_price(side, price)
    orders = [(s, _effective_price(s, p)) for s, p in resting] + [(side, new_price)]
    buys = [p for s, p in orders if s == BUY]
    sells = [p for s, p in orders if s == SELL]
    if not buys or not sells:
        return False, ""
    max_buy, min_sell = max(buys), min(sells)
    if max_buy >= min_sell:
        return True, (
            f"self-cross: max buy {_fmt(max_buy)} >= min sell {_fmt(min_sell)} "
            "(PCAS penalty condition)"
        )
    return False, ""


def _fmt(price: float) -> str:
    if price == _MARKET_BUY:
        return "MKT(buy)"
    if price == _MARKET_SELL:
        return "MKT(sell)"
    return f"{price:g}"
