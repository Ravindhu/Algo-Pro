"""Notifications for the dashboard's Execution log > Notification tab (2026-11-27).

Status: PHASE 1 IMPLEMENTED (read-only reader; no order path).

Read-only. It reads the tail of each bot audit file and keeps the rows that mean the quant bot
needs a person's attention: every ``alert`` the bot raised, plus a short list of warning kinds
the bot already writes to its audit trail. Newest first, capped. A missing or unreadable file
gives an empty list; this module never raises and never writes.
"""
from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from quant.core.time_ist import naive_ist

MAX_ITEMS = 50
TAIL_LINES = 5000
MESSAGE_MAX = 200

#: Warning-class audit kinds (other than ``alert``) and the level each one shows.
WARNING_KINDS: Dict[str, str] = {
    "exit_halted": "CRITICAL",
    "exit_held_stale_bar": "CRITICAL",
    "positions_unmanaged": "CRITICAL",
    "exit_not_sent": "WARNING",
    "cycle_skipped": "WARNING",
    "entries_blocked": "WARNING",
    "limits_refresh_failed": "WARNING",
}
_DETAIL_KEYS = ("symbol", "key", "status", "reason", "error", "exit_code")

#: Rows about the machine, not the trading book (2026-11-28). They show on the Alert tab.
#: Everything else is a trading alert and shows on the Notification tab. An ``alert`` row can
#: set ``payload["category"] = "system"`` to move itself to the Alert tab.
SYSTEM_KINDS = frozenset({"screen_refresh"})
CATEGORIES = ("trading", "system")


def _category(kind: str, payload: Dict[str, Any]) -> str:
    if kind in SYSTEM_KINDS:
        return "system"
    if kind == "alert" and str(payload.get("category") or "") == "system":
        return "system"
    return "trading"


def _tail(path: Path) -> Iterable[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return list(deque(fh, maxlen=TAIL_LINES))
    except OSError:
        return []


def _ist_text(ts: Any) -> str:
    try:
        stamp = naive_ist(datetime.fromisoformat(str(ts)))
        return stamp.isoformat(sep=" ", timespec="seconds")
    except (TypeError, ValueError):
        return "—"


def _message(kind: str, payload: Dict[str, Any]) -> Optional[str]:
    """Text for one row, or None when the row is not a notification."""
    if kind == "alert":
        text = payload.get("message")
        return str(text) if text else "alert"
    if kind == "screen_refresh":
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        code = result.get("exit_code")
        if code in (None, 0):
            return None
        return "screen refresh: " + str(result.get("reason") or f"exit code {code}")
    if kind == "startup_recon":
        if payload.get("status") in (None, "match", "skipped"):
            return None
        return f"startup reconcile: {payload.get('status')}"
    if kind in WARNING_KINDS:
        parts = [kind.replace("_", " ")]
        parts += [f"{k}={payload[k]}" for k in _DETAIL_KEYS if payload.get(k) not in (None, "")]
        return " · ".join(parts)
    return None


def _level(kind: str, payload: Dict[str, Any]) -> str:
    if kind == "alert":
        return str(payload.get("level") or "WARNING").upper()
    if kind == "screen_refresh" or kind == "startup_recon":
        return "WARNING"
    return WARNING_KINDS.get(kind, "WARNING")


def recent(audit_paths: Iterable[Path], limit: int = MAX_ITEMS,
           category: Optional[str] = None) -> Dict[str, Any]:
    """Newest notifications from the given audit files. ``category`` = "trading" or "system" (None = all). Never raises."""
    items: List[Dict[str, Any]] = []
    for path in audit_paths:
        for line in _tail(Path(path)):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind") or "")
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            message = _message(kind, payload)
            if message is None:
                continue
            items.append({"ts": str(row.get("ts") or ""), "time_ist": _ist_text(row.get("ts")),
                          "level": _level(kind, payload), "source": kind,
                          "category": _category(kind, payload),
                          "message": message[:MESSAGE_MAX]})
    if category is not None:
        items = [row for row in items if row["category"] == category]
    items.sort(key=lambda r: r["ts"], reverse=True)
    items = items[:max(0, int(limit))]
    return {"count": len(items), "items": items}
