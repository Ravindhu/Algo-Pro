"""Expiry-day and lot-size rules for NSE/BSE cash equities (owner rules, 2026-11-06).

Flow stage: Trading calendar (X-1)
Status: PHASE 1 IMPLEMENTED 2026-11-06 (unit tests: tests/unit/test_expiry_lot_rules.py)

Owner rules:
- Delivery (CNC) positions have NO expiry day.
- Intraday (MIS) positions expire on the SAME trading day they are opened.
- Lot size applies ONLY to SME-segment stocks. All other cash equities trade in 1 share.
- SME lot size is read from the BROKER (OpenAlgo symbol lookup, field "lotsize") at order
  time, not from a local table. Orders for SME stocks use whole lots only: qty = lots x lot size.
- If the broker gives no valid lot size for an SME symbol, the order is refused (fail closed).

Product names: "delivery" or "CNC" mean delivery; "intraday" or "MIS" mean intraday.
This matches the names used in risk/controls.py and risk/squareoff.py (MIS).

No order is placed here. This module only computes and checks quantities. Order placement
stays in the execution package, behind the risk gate (Phase 3, not enabled).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

DELIVERY_NAMES = frozenset({"delivery", "cnc"})
INTRADAY_NAMES = frozenset({"intraday", "mis"})


class UnknownProduct(ValueError):
    """Product name is not delivery or intraday. Refused, never guessed."""


class LotSizeUnknown(ValueError):
    """SME symbol has no valid broker lot size. Refused, never guessed."""


class LotMultipleError(ValueError):
    """Order quantity is not a whole number of lots."""


def _product_kind(product: str) -> str:
    p = str(product).strip().lower()
    if p in DELIVERY_NAMES:
        return "delivery"
    if p in INTRADAY_NAMES:
        return "intraday"
    raise UnknownProduct(f"unknown product {product!r}; use delivery/CNC or intraday/MIS")


def expiry_day(product: str, trade_day: date) -> Optional[date]:
    """Return the expiry day for a position, or None when it has no expiry.

    Delivery: None (no expiry). Intraday: the same day the position was opened.
    """
    if _product_kind(product) == "delivery":
        return None
    return trade_day


def must_close_by(product: str, trade_day: date) -> Optional[date]:
    """Day by which a position must be closed. Same as expiry_day for this rule set."""
    return expiry_day(product, trade_day)


def lot_size_from_broker(client: Any, symbol: str, exchange: str = "NSE") -> int:
    """Ask the broker (via OpenAlgo) for the lot size of one symbol.

    `client` is any object with `symbol(symbol=, exchange=)` returning a dict, like the
    vendored OpenAlgo SDK. Missing, zero, negative or non-numeric lot sizes are refused.
    """
    try:
        resp = client.symbol(symbol=symbol, exchange=exchange)
    except Exception as exc:  # broker or network failure: fail closed
        raise LotSizeUnknown(f"broker lookup failed for {symbol!r}: {exc}") from exc
    data = resp.get("data", resp) if isinstance(resp, dict) else None
    raw = data.get("lotsize") if isinstance(data, dict) else None
    try:
        lot = int(raw)
    except (TypeError, ValueError):
        raise LotSizeUnknown(f"broker gave no lot size for {symbol!r}: {raw!r}")
    if lot <= 0:
        raise LotSizeUnknown(f"broker lot size for {symbol!r} must be positive, got {lot}")
    return lot


def lot_size(segment: str, broker_lot: Optional[int]) -> int:
    """Shares per lot. Non-SME cash equities: 1. SME: the broker lot size; missing -> refused."""
    if str(segment).strip().upper() != "SME":
        return 1
    if broker_lot is None or int(broker_lot) <= 0:
        raise LotSizeUnknown("SME order needs a positive broker lot size")
    return int(broker_lot)


def validate_lot_multiple(qty: int, lot: int) -> int:
    """Return qty if it is a positive whole number of lots. Otherwise refuse."""
    if lot <= 0:
        raise LotMultipleError(f"lot size must be positive, got {lot}")
    if qty <= 0 or qty % lot != 0:
        raise LotMultipleError(f"qty {qty} is not a positive multiple of lot {lot}")
    return qty


def sme_order_quantity(lots: int, lot: int) -> int:
    """Shares for an SME order: whole lots only (lots x lot size)."""
    if lots < 1:
        raise LotMultipleError(f"need at least 1 lot, got {lots}")
    return validate_lot_multiple(lots * lot, lot)
