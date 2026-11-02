"""
ledger_store.py - durable sqlite persistence for the blue_engine POV ledger.

Why this exists (audit F3)
--------------------------
``blue_engine.core.ledger`` keeps child-order / fill / VWAP state in memory,
which is the AUTHORITATIVE implementation while the process is alive. But a
process crash loses that state, and the recovery policy (AGENTS.md: verify
orders and positions before resuming) needs the filled quantity to survive a
restart. This module gives the in-memory ledger a durable mirror on plain
sqlite3 - no ORM, no platform dependency, no OpenAlgo internals - so
``recover_filled_quantity()`` can answer from disk after a restart.

Design rules (mirroring core/ledger.py)
---------------------------------------
* NEVER RAISES on the hot path: every call is fail-soft; on the first sqlite
  error the store marks itself degraded for the rest of the process and the
  ledger silently continues in-memory-only (exactly like the retired DB mirror
  behaved, minus the silent-swallow bug - degrading is logged loudly once).
* Thread-safe: a single ``threading.Lock`` guards the connection; the POV loop
  and the order-update callback thread may write concurrently.
* Plain sqlite3 + stdlib only. WAL mode for crash-safe writes.
* Storage location is production-safe: ``<repo>/data/ledger_store.db``
  (override with the ``BLUE_LEDGER_DB_PATH`` env var). Never under Temp/.

Schema
------
    executions(
        execution_id TEXT PRIMARY KEY,
        snapshot     TEXT NOT NULL,   -- JSON, one per ledger row
        updated_at   TEXT NOT NULL    -- ISO timestamp, last write
    )

A "snapshot" is a plain dict produced by :func:`ledger._snapshot_row`; this
module stays format-agnostic (it serialises whatever dict it is handed), so
ledger.py can evolve its fields without a schema migration here.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from ..runtime_paths import runtime_data_root

logger = logging.getLogger(__name__)

__all__ = [
    "default_db_path",
    "store_row",
    "fetch_row",
    "fetch_all_ids",
    "store_available",
    "close_store",
]

# ---------------------------------------------------------------------------
# Connection management (lazy, thread-safe, fail-soft)
# ---------------------------------------------------------------------------
_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None
_DEGRADED = False


def default_db_path() -> Path:
    """Resolve where the durable ledger lives.

    Priority:
        1. ``BLUE_LEDGER_DB_PATH`` env var (absolute path)
        2. ``<repo_root>/data/ledger_store.db`` (repo root = two levels up
           from this file: Blue_engine/core/ledger_store.py)
    """
    override = os.getenv("BLUE_LEDGER_DB_PATH")
    if override:
        return Path(override)
    return runtime_data_root() / "ledger_store.db"


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
                "CREATE TABLE IF NOT EXISTS executions ("
                " execution_id TEXT PRIMARY KEY,"
                " snapshot     TEXT NOT NULL,"
                " updated_at   TEXT NOT NULL)")
            conn.commit()
            _CONN = conn
            logger.info("ledger_store: durable ledger at %s", path)
            return _CONN
        except Exception as exc:  # noqa: BLE001 - fail-soft by contract
            _DEGRADED = True
            logger.error("ledger_store: init failed, degrading to "
                         "in-memory-only: %s", exc)
            return None


def _degrade(exc: Exception) -> None:
    """Disable the store for the rest of the process after a failure."""
    global _DEGRADED
    if not _DEGRADED:
        _DEGRADED = True
        logger.error("ledger_store: degrading to in-memory-only after "
                     "sqlite error: %s", exc)


def store_available() -> bool:
    """True when the store has not degraded (best-effort health probe)."""
    return not _DEGRADED and _get_conn() is not None


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


# ---------------------------------------------------------------------------
# Persistence API
# ---------------------------------------------------------------------------
def store_row(execution_id: str, snapshot: Dict[str, Any]) -> bool:
    """Upsert one ledger-row snapshot (JSON) by execution id.

    Args:
        execution_id: the ledger execution id (primary key).
        snapshot: a JSON-serialisable dict describing the ledger row.

    Returns:
        True when written, False when the store is degraded or the write
        failed (the ledger must continue in memory either way).
    """
    conn = _get_conn()
    if conn is None:
        return False
    try:
        payload = json.dumps(snapshot, default=str)
        now = datetime.now(timezone.utc).isoformat()
        with _LOCK:
            conn.execute(
                "INSERT INTO executions (execution_id, snapshot, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(execution_id) DO UPDATE SET "
                " snapshot=excluded.snapshot, updated_at=excluded.updated_at",
                (str(execution_id), payload, now))
            conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - fail-soft by contract
        _degrade(exc)
        return False


def fetch_row(execution_id: str) -> Optional[Dict[str, Any]]:
    """Load one snapshot by execution id, or None (missing / degraded)."""
    conn = _get_conn()
    if conn is None:
        return None
    try:
        with _LOCK:
            cur = conn.execute(
                "SELECT snapshot FROM executions WHERE execution_id = ?",
                (str(execution_id),))
            row = cur.fetchone()
        if not row:
            return None
        return json.loads(row[0])
    except Exception as exc:  # noqa: BLE001 - fail-soft by contract
        _degrade(exc)
        return None


def fetch_all_ids() -> list[str]:
    """Return every execution id currently persisted (for bulk restore)."""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with _LOCK:
            cur = conn.execute("SELECT execution_id FROM executions")
            return [r[0] for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001 - fail-soft by contract
        _degrade(exc)
        return []
