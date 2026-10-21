"""protective_orders.py - Registry of SL/TP levels attached to executions.

========================================================================
The passthrough layer: when a strategy (OCL keylevel) fires an intent
carrying ``stop_loss`` / ``target``, the POV slicer records them here so
the position monitor and dashboard can:

  * show the protective levels next to each open execution,
  * place/sync real SL/TP orders at the broker (when order placement is
    enabled), and
  * audit after the fact whether exits respected the levels.

Design: a single in-process registry keyed by execution_id + symbol,
thread-safe, best-effort persistence to JSON so the levels survive a
restart (the file is overwritten on every update - it is a mirror, not
the source of truth; the source is the intent metadata itself).

This module does NOT place orders by itself. Broker-side placement is
the order manager's job; this registry is the shared handoff point.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()
_LEVELS: Dict[str, Dict[str, Any]] = {}
_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "protective_orders.json"


def register(execution_id: str, *, symbol: str, exchange: str,
             action: str, stop_loss: Optional[float],
             target: Optional[float],
             strategy: str = "", meta: Optional[Dict[str, Any]] = None,
             persist: bool = True) -> None:
    """Record SL/TP levels for one execution (idempotent per key)."""
    key = f"{execution_id}|{symbol}"
    with _LOCK:
        _LEVELS[key] = {
            "execution_id": execution_id,
            "symbol": symbol,
            "exchange": exchange,
            "action": action,
            "stop_loss": stop_loss,
            "target": target,
            "strategy": strategy,
            "exit_mode": (meta or {}).get("exit_mode"),
            "htf_stop_loss": (meta or {}).get("htf_stop_loss"),
            "registered_at": datetime.now().isoformat(),
        }
    if persist:
        _persist()


def update(execution_id: str, symbol: str, *,
           stop_loss: Optional[float] = None) -> None:
    """Ratchet the recorded stop (e.g. breakeven / ATR trail moves)."""
    key = f"{execution_id}|{symbol}"
    with _LOCK:
        rec = _LEVELS.get(key)
        if rec is None:
            return
        if stop_loss is not None:
            cur = rec.get("stop_loss")
            if cur is None or stop_loss > cur:      # BUY stops only ratchet up
                rec["stop_loss"] = stop_loss
                rec["updated_at"] = datetime.now().isoformat()
    _persist()


def clear(execution_id: str, symbol: str) -> None:
    """Drop the record once the position is closed."""
    with _LOCK:
        _LEVELS.pop(f"{execution_id}|{symbol}", None)
    _persist()


def snapshot() -> List[Dict[str, Any]]:
    """Current open protective levels (for the dashboard/monitor)."""
    with _LOCK:
        return [dict(v) for v in _LEVELS.values()]


def get(execution_id: str, symbol: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        rec = _LEVELS.get(f"{execution_id}|{symbol}")
        return dict(rec) if rec else None


def _persist() -> None:
    try:
        _DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            payload = dict(_LEVELS)
        _DEFAULT_PATH.write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except Exception:                               # noqa: BLE001
        # Persistence is best-effort; the in-process registry is the
        # operative store within a run.
        pass
