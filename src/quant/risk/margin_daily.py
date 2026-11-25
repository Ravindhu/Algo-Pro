"""Once-a-day broker margin reading per stock (owner 2026-11-25). Stores the reading for the day only.

Flow stage: Risk gate (margin funding), daily facility record
Status: PHASE 3 IMPLEMENTED
A reading says whether margin funding is available for a stock and the funded share. It is fetched
once per stock per trade day and reused for every MTF entry in that stock that day. A failed or
unusable reading is stored as unavailable, so it is not retried all day. A new trade day clears the file.
The file is written atomically. An unreadable file is treated as empty, so readings are fetched again (fail closed).
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional


class DailyMarginStore:
    def __init__(self, path=None) -> None:
        self._path = Path(path) if path is not None else None
        self._day: Optional[str] = None
        self._records: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("records"), dict):
                self._day = str(doc.get("day"))
                self._records = {str(k): dict(v) for k, v in doc["records"].items() if isinstance(v, dict)}
        except (OSError, ValueError):
            self._day, self._records = None, {}

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"day": self._day, "records": self._records}, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)

    def get(self, symbol: str, day: date) -> Optional[Dict[str, Any]]:
        if self._day != day.isoformat():
            return None
        rec = self._records.get(symbol)
        return dict(rec) if rec is not None else None

    def put(self, symbol: str, day: date, record: Dict[str, Any]) -> None:
        if self._day != day.isoformat():
            self._day = day.isoformat()
            self._records = {}
        self._records[symbol] = dict(record)
        self._save()
