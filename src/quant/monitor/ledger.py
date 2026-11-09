"""Intent ID trail and position ledger. Rebuilt on restart so no order is sent twice.

Flow stage: Ledger and recon, Restart and recovery
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/ledger_store.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set


class Ledger:
    def __init__(self, path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, **data) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": event, **data}, sort_keys=True, default=str) + "\n")

    def events(self) -> List[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def seen_intent_ids(self) -> Set[str]:
        return {e["intent_id"] for e in self.events() if e["event"] == "intent"}

    def positions(self) -> Dict[str, int]:
        pos: Dict[str, int] = defaultdict(int)
        for e in self.events():
            if e["event"] == "fill":
                pos[e["symbol"]] += int(e["signed_qty"])
        return {s: q for s, q in pos.items() if q != 0}

    def recon(self, broker_positions: Dict[str, int]) -> Dict[str, tuple]:
        """Symbols where ledger and broker disagree: {symbol: (ledger_qty, broker_qty)}."""
        ledger = self.positions()
        symbols = set(ledger) | set(broker_positions)
        return {
            s: (ledger.get(s, 0), broker_positions.get(s, 0))
            for s in symbols
            if ledger.get(s, 0) != broker_positions.get(s, 0)
        }
