"""Pipeline orchestrator wrapper, part 2: supervisor journal and market-hours gate, read only.

Flow stage: Pipeline orchestrator
Status: PHASE 5 IMPLEMENTED (wrap; read only; the supervisor is never started from here)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/supervisor.py
(read_status, market_hours_gate)

The supervisor restarts the trading engine, and its broker gates call the broker. Both
stay in Blue and are NOT wrapped. This module only reads the journal and checks the
continuous session window.

FAIL-CLOSED RULES
-----------------
- A missing or unreadable journal reports no state. It never invents one.
- Blue unavailable: the market-hours gate is refused (``allowed=False``).
- Never raises.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Dict, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["journal_status", "market_hours"]


def journal_status(journal_path: Optional[str] = None, tail: int = 20) -> Dict[str, Any]:
    """The supervisor journal summary, read only. Never raises."""
    try:
        read_status = load("core.supervisor").read_status
    except BlueEngineUnavailable as exc:
        return {"ok": False, "status": None, "reason": f"Blue supervisor unavailable: {exc}"}
    try:
        status = read_status(journal_path, tail)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": None, "reason": f"journal read failed: {exc!r}"}
    return {"ok": isinstance(status, dict), "status": status, "reason": ""}


def market_hours(symbol: str = "", allow_outside: bool = False,
                 entry_cutoff: str = "14:45", now: Optional[Any] = None) -> Dict[str, Any]:
    """Is the continuous session open for new entries? Refused on any failure. Never raises."""
    try:
        gate = load("core.supervisor").market_hours_gate
    except BlueEngineUnavailable as exc:
        return {"ok": False, "allowed": False, "reason": f"Blue supervisor unavailable: {exc}"}
    try:
        result = gate(symbol=symbol, allow_outside=allow_outside,
                      entry_cutoff=entry_cutoff, now=now)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "allowed": False, "reason": f"market-hours check failed: {exc!r}"}
    if dataclasses.is_dataclass(result):
        fields = dataclasses.asdict(result)
    elif isinstance(result, dict):
        fields = dict(result)
    else:
        return {"ok": False, "allowed": False, "reason": "gate returned no result"}
    allowed = bool(fields.get("ok", fields.get("allowed", False)))
    return {"ok": True, "allowed": allowed, "reason": str(fields.get("reason") or "")}
