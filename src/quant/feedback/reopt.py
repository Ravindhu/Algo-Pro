"""Re-optimization and re-research requests. Nothing runs until a person approves it.

Flow stage: Re-opt request, Re-run research
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/jobs/sector_hm_report.py
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List


class ReoptQueue:
    def __init__(self, path) -> None:
        self.path = Path(path)
        self.items: List[Dict] = json.loads(self.path.read_text()) if self.path.exists() else []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, indent=2))

    def request(self, strategy_id: str, start: str, end: str, reason: str) -> str:
        rid = f"REOPT-{len(self.items) + 1}"
        self.items.append({"id": rid, "strategy_id": strategy_id, "start": start, "end": end,
                           "reason": reason, "approved_by": None, "status": "pending"})
        self._save()
        return rid

    def approve(self, rid: str, actor: str, reason: str) -> None:
        if not actor or not reason:
            raise ValueError("approval needs an actor and a reason")
        item = next(i for i in self.items if i["id"] == rid)
        item.update(approved_by=actor, status="approved", approval_reason=reason)
        self._save()

    def runnable(self) -> List[Dict]:
        return [i for i in self.items if i["status"] == "approved"]
