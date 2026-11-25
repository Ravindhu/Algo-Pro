"""Details of each child order for the Execution log (owner 2026-11-25).

For every filled child order we keep: the strategy (short label), the risk % it was sized on, and the level of
each timeframe it came from (MTF, HTF, LTF). Stored by the broker order id, so the log can join a tradebook fill
to it. Fills from before this existed have no record and show a dash.

Flow stage: Execution (order record), read by the dashboard
Status: PHASE 3 IMPLEMENTED
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

#: Short labels the owner reads in the Execution log. Unknown strategy names are shown as they are.
STRATEGY_LABEL = {
    "engulfing": "EG-engulfing",
    "ocl_keylevel": "OCL-OCL",
    "engulfing_fail": "EGF-engulfing fail",
    "ocl_sell_fail_top_seq": "FOCL-T sell fail ocl top",
    "ocl_sell_fail_bottom_seq": "FOCL-B sell fail ocl bottom",
}

_LEAF = {"leaf_aggressive": "agg", "leaf_conservative": "con"}
_MAX_RECORDS = 5000  # the file keeps the newest records; the oldest are dropped


def level_text(stage: Optional[str], slot: Optional[int]) -> str:
    """'agg 2', 'con 1' for MTF/HTF slots; 'agg' or 'con' for an LTF touch; a dash when there is no level."""
    if stage in _LEAF:
        return _LEAF[stage]
    if stage == "aggressive" and slot:
        return f"agg {slot}"
    if stage == "conservative" and slot:
        return f"con {slot}"
    return "—"


def build_details(strategy_id: str, risk_pct: float, context: tuple) -> Dict[str, Any]:
    """context = (mtf (stage, slot) or None, htf (stage, slot) or None, leaf stage) from risk_tree.Decision.context."""
    mtf, htf, leaf = (tuple(context) + (None, None, None))[:3] if context else (None, None, None)
    mtf_txt = level_text(*mtf) if mtf else "—"
    htf_txt = level_text(*htf) if htf else "—"
    return {
        "strategy": STRATEGY_LABEL.get(strategy_id, strategy_id),
        "risk_pct": round(float(risk_pct), 3),
        "mtf": mtf_txt,
        "htf": htf_txt,
        "ltf": level_text(leaf, None),
    }


class OrderDetailStore:
    """Broker order id -> details, kept in one JSON file. Writes are atomic. No path = memory only (tests)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else None
        self._records: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self._path is None or not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}  # an unreadable file reads as empty: the log shows dashes, nothing is guessed
        return data if isinstance(data, dict) else {}

    def get(self, orderid: str) -> Optional[Dict[str, Any]]:
        return dict(self._records[orderid]) if orderid in self._records else None

    def put(self, orderid: str, details: Dict[str, Any]) -> None:
        if not orderid:
            return
        self._records.pop(orderid, None)
        self._records[orderid] = dict(details)
        while len(self._records) > _MAX_RECORDS:
            self._records.pop(next(iter(self._records)))
        self._save()

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._records, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)
