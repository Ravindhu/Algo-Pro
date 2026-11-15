"""Persistent open-position state for the strategy bot (audit fix 2, 2026-11-15).

Flow stage: Position book (restart-safe)
Status: PHASE 3 IMPLEMENTED (2026-11-15)

Before this file, a restart forgot every open position, its broker stop and the open
exposure. The bot now writes its state to one JSON file on every change, and reads it
back at start. The startup reconciliation then compares the restored book with the
broker's book, and entries stay blocked until they match.

Saved: open positions (all fields), broker stop order ids, open notional, booked
notional per position, mark-to-market per position, and the parents already traded
today (so a restarted bot does not enter the same signal twice).

Not saved: parents still being sliced. A restarted bot keeps the quantity that was
already filled and does not finish slicing it.

Writes are atomic (temp file, then rename). An unreadable file loads as None, and the
bot then starts with no restored positions. The startup reconciliation catches that
case when the broker still holds positions.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


class PositionStore:
    """JSON file with one document: {"day", "open_notional", "booked", "marks", "done", "positions"}."""

    def __init__(self, path) -> None:
        self.path = Path(path)

    def load(self) -> Optional[Dict[str, Any]]:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(doc, dict) or not isinstance(doc.get("positions"), list):
            return None
        return doc

    def save(self, doc: Dict[str, Any]) -> None:
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
