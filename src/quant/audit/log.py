"""Append-only, hash-chained audit log. Every decision and override is recorded.

Flow stage: Audit log
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/ledger.py
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

GENESIS = "0" * 64


def _digest(body: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class AuditLog:
    """Each record stores the hash of the previous record. Editing any line breaks verify()."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last = self._last_hash()

    def _last_hash(self) -> str:
        records = self.read_all()
        return records[-1]["hash"] if records else GENESIS

    def append(self, kind: str, payload: Dict[str, Any]) -> str:
        body = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "payload": payload,
            "prev": self._last,
        }
        digest = _digest(body)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"hash": digest, **body}, sort_keys=True, default=str) + "\n")
        self._last = digest
        return digest

    def read_all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def verify(self) -> bool:
        prev = GENESIS
        for rec in self.read_all():
            body = {k: rec[k] for k in ("ts", "kind", "payload", "prev")}
            if rec["prev"] != prev or _digest(body) != rec["hash"]:
                return False
            prev = rec["hash"]
        return True
