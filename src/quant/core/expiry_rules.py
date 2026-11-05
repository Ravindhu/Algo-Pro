"""Expiry-day and lot-size rules for NSE/BSE cash equities (owner rules, 2026-11-06).

Flow stage: Trading calendar (X-1)
Status: PHASE 1 IMPLEMENTED 2026-11-06 (unit tests: tests/unit/test_expiry_lot_rules.py)

Owner rules:
- Delivery (CNC) positions have NO expiry day.
- Intraday (MIS) positions expire on the SAME trading day they are opened.
- Lot size applies ONLY to SME-segment stocks. All other cash equities trade in 1 share.
- The SME lot-size table is config/calendar/sme_lot_sizes.csv. It is empty until the owner
  fills it. A symbol in the SME segment with no lot size is refused (fail closed).

Product names: "delivery" or "CNC" mean delivery; "intraday" or "MIS" mean intraday.
This matches the names used in risk/controls.py and risk/squareoff.py (MIS).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

DELIVERY_NAMES = frozenset({"delivery", "cnc"})
INTRADAY_NAMES = frozenset({"intraday", "mis"})

DEFAULT_SME_LOT_CSV = Path(__file__).resolve().parents[3] / "config" / "calendar" / "sme_lot_sizes.csv"


class UnknownProduct(ValueError):
    """Product name is not delivery or intraday. Refused, never guessed."""


class LotSizeUnknown(ValueError):
    """SME symbol has no lot size in the owner table. Refused, never guessed."""


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


def load_sme_lots(path: Path | str = DEFAULT_SME_LOT_CSV) -> Dict[str, int]:
    """Read symbol,lot_size rows. A file with only a header returns an empty table."""
    df = pd.read_csv(Path(path), dtype={"symbol": str})
    if df.empty:
        return {}
    out: Dict[str, int] = {}
    for _, row in df.iterrows():
        sym = str(row["symbol"]).strip().upper()
        lot = int(row["lot_size"])
        if not sym or lot <= 0:
            raise ValueError(f"bad SME lot row: {row.to_dict()}")
        out[sym] = lot
    return out


def lot_size(symbol: str, segment: str, sme_lots: Dict[str, int]) -> int:
    """Shares per lot. Non-SME cash equities: 1. SME: owner table; missing -> refused."""
    if str(segment).strip().upper() != "SME":
        return 1
    key = str(symbol).strip().upper()
    if key not in sme_lots:
        raise LotSizeUnknown(f"no SME lot size for {symbol!r}; owner must add it to sme_lot_sizes.csv")
    return sme_lots[key]
