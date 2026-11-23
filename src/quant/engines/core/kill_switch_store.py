"""
kill_switch_store.py - durable persistence for the latching KillSwitch.

Why this exists
---------------
``portfolio.optim_policy.KillSwitch`` is deliberately sticky: once a drawdown
or daily-loss breach trips it, trading must not resume until a named operator
resets it. But until now that state lived ONLY in process memory. If the
process crashed right after the trip, the fresh process built a brand-new,
UN-TRIPPED switch — silently re-arming live trading after a critical failure,
which the recovery policy (AGENTS.md: never auto-resume unless all safety
checks pass) explicitly forbids.

This module mirrors the trip/reset state into plain sqlite so the halt
survives restarts. On the first ``KillSwitch`` creation in a fresh process,
``optim_policy`` loads the persisted state; a persisted trip re-arms as
tripped with a ``"persisted: ..."`` reason.

Design rules (mirroring core/ledger_store.py)
---------------------------------------------
* NEVER RAISES on the hot path: every call is fail-soft; on the first sqlite
  error the store degrades for the rest of the process and the switch simply
  behaves as before (in-memory only), with one loud log line.
* Thread-safe via a single lock; plain sqlite3 + stdlib only; WAL mode.
* Production-safe storage: ``<repo>/data/kill_switch.db`` (override with the
  ``BLUE_KILL_SWITCH_DB_PATH`` env var). Never under Temp/.
* Persistence records STATE, not decisions: the switch itself remains the
  authority inside the process; the store only guarantees the halt survives
  a restart.

Schema
------
    killswitch(
        id            INTEGER PRIMARY KEY CHECK (id = 1),   -- singleton row
        tripped       INTEGER NOT NULL,                     -- 0/1
        reason        TEXT,
        trigger_value REAL,
        updated_at    TEXT NOT NULL
    )
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from ..runtime_paths import runtime_data_root

logger = logging.getLogger(__name__)

__all__ = [
    "default_db_path", "load_state", "save_state", "clear_state",
    "store_available", "require_durable", "close_store",
]

_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None
_DEGRADED = False
_DEGRADE_DETAIL: str = ""


def default_db_path() -> Path:
    """Resolve where the kill-switch state lives.

    Priority:
        1. ``BLUE_KILL_SWITCH_DB_PATH`` env var (absolute path)
        2. ``<repo_root>/data/kill_switch.db``
    """
    override = os.getenv("BLUE_KILL_SWITCH_DB_PATH")
    if override:
        return Path(override)
    return runtime_data_root() / "kill_switch.db"


def _get_conn() -> Optional[sqlite3.Connection]:
    """Open (once) and return the sqlite connection, or None when degraded."""
    global _CONN, _DEGRADED
    if _DEGRADED:
        return None
    if _CONN is not None:
        return _CONN
    with _LOCK:
        if _CONN is not None:
            return _CONN
        try:
            path = default_db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), check_same_thread=False,
                                   timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS killswitch ("
                " id INTEGER PRIMARY KEY CHECK (id = 1),"
                " tripped INTEGER NOT NULL,"
                " reason TEXT,"
                " trigger_value REAL,"
                " updated_at TEXT NOT NULL)")
            conn.commit()
            _CONN = conn
            logger.info("kill_switch_store: durable state at %s", path)
            return _CONN
        except Exception as exc:  # noqa: BLE001 - fail-soft by contract
            _degrade(exc, phase="init")
            return None


def _degrade(exc: Exception, phase: str = "sqlite") -> None:
    """Disable the store for the rest of the process after a failure."""
    global _DEGRADED, _DEGRADE_DETAIL
    if not _DEGRADED:
        _DEGRADED = True
        _DEGRADE_DETAIL = f"{phase}: {exc}"
        logger.critical("kill_switch_store: degrading to in-memory-only after "
                     "%s error: %s", phase, exc)


def _unreadable_state() -> dict:
    """The state returned when the store cannot be read.

    FAIL-CLOSED: an unreadable store means the persisted halt status is
    UNKNOWN, so it is reported as tripped. Reporting "clear" here would let a
    restart re-arm trading after a disk fault (the exact failure this module
    exists to prevent).
    """
    return {"tripped": True,
            "reason": ("kill-switch state unreadable (fail-closed): "
                       f"{_DEGRADE_DETAIL or 'store unavailable'}"),
            "trigger_value": None,
            "unreadable": True}


def store_available() -> bool:
    """True when the store has not degraded (best-effort health probe)."""
    return not _DEGRADED and _get_conn() is not None


def require_durable() -> None:
    """Raise unless halts can survive a restart. Call before any live trading.

    A degraded store means a trip is kept in memory only, so a restart would
    silently re-arm trading. Live runs must refuse to start in that state.
    """
    if not store_available():
        raise RuntimeError(
            "kill-switch store unavailable: halts would not survive a restart")


def close_store() -> None:
    """Close the connection (for tests and clean shutdown). Never raises."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:  # noqa: BLE001
                pass
            _CONN = None


def _write(tripped: bool, reason: Optional[str],
           trigger_value: Optional[float]) -> bool:
    """Upsert the singleton state row. Internal, fail-soft."""
    conn = _get_conn()
    if conn is None:
        return False
    try:
        now = datetime.now(timezone.utc).isoformat()
        with _LOCK:
            conn.execute(
                "INSERT INTO killswitch (id, tripped, reason, trigger_value,"
                " updated_at) VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET tripped=excluded.tripped,"
                " reason=excluded.reason, trigger_value=excluded.trigger_value,"
                " updated_at=excluded.updated_at",
                (1 if tripped else 0, reason, trigger_value, now))
            conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - fail-soft by contract
        _degrade(exc, phase="write")
        return False


def save_state(reason: Optional[str],
               trigger_value: Optional[float]) -> bool:
    """Persist a TRIPPED state (call exactly once, on the transition)."""
    return _write(True, reason, trigger_value)


def clear_state() -> bool:
    """Persist a CLEARED state (after an operator reset)."""
    return _write(False, None, None)


def load_state() -> dict:
    """Load the persisted state.

    Returns:
        ``{"tripped": bool, "reason": str|None, "trigger_value": float|None}``
        * A readable store with no row (fresh install) -> not tripped.
        * A readable store with a row -> the persisted state.
        * An UNREADABLE store -> tripped, with ``"unreadable": True``
          (fail-closed; see :func:`_unreadable_state`).
        Never raises.
    """
    conn = _get_conn()
    if conn is None:
        return _unreadable_state()
    try:
        with _LOCK:
            cur = conn.execute(
                "SELECT tripped, reason, trigger_value FROM killswitch "
                "WHERE id = 1")
            row = cur.fetchone()
        if not row:
            return {"tripped": False, "reason": None, "trigger_value": None}
        return {"tripped": bool(row[0]), "reason": row[1],
                "trigger_value": row[2]}
    except Exception as exc:  # noqa: BLE001 - fail-soft by contract
        _degrade(exc, phase="read")
        return _unreadable_state()
