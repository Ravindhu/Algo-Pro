"""Automatic kill-switch trigger: daily realized loss at or beyond max_daily_loss.

Pure function. The runner decides when to call it and writes the audit record.
Uses REALIZED P&L only. Unrealized losses do not trip the switch (decision 0020).
A limit that is not set (None) never trips the switch here; entries already fail closed.

Flow stage: Kill switch, Loss budget breaker
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/kill_switch_store.py
"""
from __future__ import annotations

from typing import Optional


def daily_loss_breach(realized_pnl: float, max_daily_loss: Optional[float]) -> Optional[str]:
    """Return the trip reason when realized_pnl <= -max_daily_loss, else None."""
    if max_daily_loss is None or max_daily_loss <= 0:
        return None
    if realized_pnl <= -max_daily_loss:
        return f"daily realized loss {realized_pnl:,.2f} reached limit -{max_daily_loss:,.2f}"
    return None
