"""Persistent daily P&L state for the strategy bot.

Flow stage: Loss breaker inputs (realized + unrealized)
Status: PHASE 3 IMPLEMENTED (2026-11-11)

Fixes two AGENTS.md "known gaps":
- "Realized P&L counters reset on process restart": daily_pnl and per-strategy pnl
  survive a restart in a small JSON file next to kill.json.
- "Open positions are not marked to market": for delivery-style positions the bot adds
  the unrealized P&L of each open position to the counters on every cycle, so a falling
  book trips the daily loss limit even before exits realize the loss.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Dict, Optional


class PnlStore:
    """JSON file: {"day": "...", "daily_pnl": ..., "strategy_pnl": {...}}."""

    def __init__(self, path) -> None:
        self.path = Path(path)

    def load(self, today: date) -> Optional[Dict]:
        """Stored counters for today, or None (other day, unreadable, missing)."""
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(doc, dict) or doc.get("day") != today.isoformat():
            return None  # a new day starts at zero; old-day numbers are never reused
        daily = doc.get("daily_pnl")
        strat = doc.get("strategy_pnl")
        if not isinstance(daily, (int, float)) or not isinstance(strat, dict):
            return None
        return {"daily_pnl": float(daily), "strategy_pnl": {str(k): float(v) for k, v in strat.items()}}

    def save(self, today: date, daily_pnl: float, strategy_pnl: Dict[str, float]) -> None:
        doc = {"day": today.isoformat(), "daily_pnl": round(float(daily_pnl), 2),
               "strategy_pnl": {k: round(float(v), 2) for k, v in strategy_pnl.items()}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
