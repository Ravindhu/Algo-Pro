"""Daily sync of MTF holdings with the broker (owner 2026-11-25). Pure.

Flow stage: Risk gate (margin funding), broker reconciliation
Status: PHASE 3 IMPLEMENTED
The broker's position book gives MTF quantities and average prices. Our own MTF quantities must match
per symbol. Any mismatch blocks new MTF entries until it is cleared. CNC is not affected.
MTF % = borrowed amount (our record, funded share x value) / value of the MTF holdings at the broker.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

MTF_PRODUCTS = ("MTF", "MARGIN")


def _num(value) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if math.isfinite(out) else float("nan")


def parse_mtf_rows(body: Any, exchange: str = "NSE") -> Dict[str, Dict[str, float]]:
    """{symbol: {"qty": int, "value": float}} for MTF rows of one exchange. Raises ValueError on a bad body."""
    if not isinstance(body, dict) or body.get("status") != "success":
        raise ValueError("positionbook not successful")
    rows = body.get("data")
    if not isinstance(rows, list):
        raise ValueError("positionbook data is not a list")
    out: Dict[str, Dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, dict) or "symbol" not in row:
            raise ValueError("positionbook row without symbol")
        if str(row.get("exchange", exchange)) != exchange:
            continue
        if str(row.get("product", "")).upper() not in MTF_PRODUCTS:
            continue
        qty = _num(row.get("netqty", row.get("quantity", 0)))
        avg = _num(row.get("average_price", row.get("avgprice", 0)))
        if math.isnan(qty) or math.isnan(avg):
            raise ValueError(f"MTF row with unreadable quantity or price for {row.get('symbol')!r}")
        sym = str(row["symbol"])
        cur = out.setdefault(sym, {"qty": 0.0, "value": 0.0})
        cur["qty"] += qty
        cur["value"] += qty * avg
    return out


def reconcile_mtf(ours: Dict[str, int], broker: Dict[str, Dict[str, float]]) -> Tuple[bool, List[str], float]:
    """(ok, issues, broker MTF value). ours: our MTF quantity per symbol. Any symbol that differs is an issue."""
    issues: List[str] = []
    for sym in sorted(set(ours) | set(broker)):
        have = int(ours.get(sym, 0))
        theirs = int(round(broker.get(sym, {}).get("qty", 0.0)))
        if have != theirs:
            issues.append(f"{sym}: ours {have}, broker {theirs}")
    value = sum(float(v.get("value", 0.0)) for v in broker.values())
    return (not issues), issues, value
