"""Pick the exchange (NSE or BSE) for an entry, and the exit price on the held exchange.

Status: IMPLEMENTED as pure logic (tests in tests/unit/test_exchange_router.py).
Plan: docs/IMPLEMENTATION_PLAN_2026-11-25_EXCHANGE_ROUTING.md

Owner rules (2026-11-25):
* Entry (buy): NSE by default. BSE only when its ask is at least 0.05% cheaper.
  A stock listed only on BSE trades on BSE.
* No liquidity skip.
* Exit (long): sell at the best bid on the exchange the position is held on.
  Exits never switch exchange.
* Fail closed: a missing, zero, non-finite or crossed quote is never used.

Pure functions only: no network, no order placement. Wiring into the order path is a
separate step.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

NSE, BSE = "NSE", "BSE"
#: BSE must be at least this fraction cheaper than NSE to be chosen.
BSE_EDGE = 0.0005  # 0.05%


@dataclass(frozen=True)
class Quote:
    """Best bid and best ask for one exchange. None means no quote."""
    bid: Optional[float]
    ask: Optional[float]


@dataclass(frozen=True)
class Choice:
    """Where and at what price to trade. exchange None = do not trade."""
    exchange: Optional[str]
    price: Optional[float]
    reason: str


def _pos(value) -> Optional[float]:
    """Finite and above zero, else None. Booleans are not prices."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) and out > 0 else None


def quote_from_reply(reply) -> Quote:
    """Quote from one OpenAlgo /quotes reply. Anything unexpected = no quote (fail closed)."""
    if not isinstance(reply, dict) or reply.get("status") != "success":
        return Quote(bid=None, ask=None)
    data = reply.get("data")
    if not isinstance(data, dict):
        return Quote(bid=None, ask=None)
    return Quote(bid=data.get("bid"), ask=data.get("ask"))


def _usable_ask(q: Optional[Quote]) -> Optional[float]:
    if q is None:
        return None
    ask, bid = _pos(q.ask), _pos(q.bid)
    if ask is None or bid is None or bid > ask:  # crossed or missing: unusable
        return None
    return ask


def choose_entry(nse: Optional[Quote], bse: Optional[Quote]) -> Choice:
    """Exchange and buy price for a new long entry."""
    nse_ask, bse_ask = _usable_ask(nse), _usable_ask(bse)
    if nse_ask is None and bse_ask is None:
        return Choice(None, None, "no usable quote on NSE or BSE")
    if nse_ask is None:
        return Choice(BSE, bse_ask, "listed on BSE only or no usable NSE quote")
    if bse_ask is None:
        return Choice(NSE, nse_ask, "no usable BSE quote")
    if bse_ask <= nse_ask * (1.0 - BSE_EDGE):
        return Choice(BSE, bse_ask, "BSE at least 0.05% cheaper than NSE")
    return Choice(NSE, nse_ask, "NSE (BSE not 0.05% cheaper)")


def exit_price(held_exchange: str, quotes: dict) -> Choice:
    """Sell price for a long exit: best bid on the exchange the position is held on.

    Never switches exchange. No usable bid on that exchange = do not trade.
    """
    if held_exchange not in (NSE, BSE):
        return Choice(None, None, f"unknown exchange {held_exchange!r}")
    q = quotes.get(held_exchange)
    bid = _pos(q.bid) if q is not None else None
    ask = _pos(q.ask) if q is not None else None
    if bid is None or ask is None or bid > ask:
        return Choice(None, None, f"no usable bid on {held_exchange}")
    return Choice(held_exchange, bid, f"best bid on {held_exchange}")


def listing_exchange(nse: Optional[Quote], bse: Optional[Quote]) -> Optional[str]:
    """Where a stock is listed for the signal (owner rule 2026-11-25): NSE if NSE has a
    usable quote, else BSE if BSE has one, else None (no signal data)."""
    if _usable_ask(nse) is not None:
        return NSE
    if _usable_ask(bse) is not None:
        return BSE
    return None
