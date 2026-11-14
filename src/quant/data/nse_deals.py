"""Bulk and block deals for the screened universe, with screen-lifetime tracking.

Flow stage: Bulk and block deals
Status: PHASE 2 IMPLEMENTED (2026-11-14)
Existing code to reuse or wrap (do not copy):
    src/quant/data/nse_mcp_client.py  (the source adapter: nse-mcp ->
        jugaad-data -> direct NSE JSON, reporting which one answered)

WHAT THIS MODULE DOES
---------------------
Fetches bulk and block deals every evening for the CURRENTLY SCREENED symbols
and stores them. A deal row is kept while its symbol stays in the screener; when
the symbol drops out, the row is MARKED ``removed_at`` rather than deleted.

Why soft delete: a deleted row cannot answer "what did this name's large holders
do before it left the screen", which is the exact question a researcher asks.
Retention is a status change, not data loss.

SCREEN LIFETIME
---------------
Each fetch records the screen's symbol set in ``screen_membership``. A symbol
that was present yesterday and absent today gets ``removed_at`` stamped on its
deal rows and on its membership row. If it returns to the screen, the membership
row is REOPENED (``removed_at`` cleared) and its deal history is intact.

DEAL IDENTITY
-------------
A deal is keyed by (trade_date, deal_type, symbol, client_name, buy_sell,
quantity, price). The same client can trade a name more than once in a session,
so quantity and price are part of the key; without them, two distinct deals
collapse into one and the totals are wrong.

FAIL-CLOSED RULES
-----------------
- An empty screen means NO fetch. Fetching deals for a universe we cannot verify
  wastes calls and stores rows nobody asked for.
- A fetch that returns zero deals is recorded as ``ok`` with ``rows: 0``. It is
  NOT treated as "no deals happened" unless the source itself said so.
- Nothing is ever hard-deleted by this module.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from quant.data import nse_mcp_client

logger = logging.getLogger(__name__)

__all__ = [
    "DealsStore",
    "default_db_path",
    "fetch_and_store",
    "active_deals",
    "deals_for_symbol",
    "sync_screen_membership",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deals (
    trade_date   TEXT NOT NULL,
    deal_type    TEXT NOT NULL,          -- block | bulk
    symbol       TEXT NOT NULL,
    client_name  TEXT NOT NULL DEFAULT '',
    buy_sell     TEXT NOT NULL DEFAULT '',
    quantity     REAL NOT NULL,
    price        REAL,
    value_inr    REAL,
    source       TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    removed_at   TEXT,                   -- set when the symbol leaves the screen
    PRIMARY KEY (trade_date, deal_type, symbol, client_name, buy_sell, quantity, price)
);
CREATE TABLE IF NOT EXISTS screen_membership (
    symbol      TEXT PRIMARY KEY,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    removed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_deals_symbol ON deals(symbol);
CREATE INDEX IF NOT EXISTS idx_deals_date ON deals(trade_date);
"""


def default_db_path() -> Path:
    """Repo-relative default: data/deals/deals.sqlite. ``QUANT_DEALS_DB`` overrides."""
    override = os.getenv("QUANT_DEALS_DB")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "data" / "deals" / "deals.sqlite"


class DealsStore:
    """SQLite store for bulk/block deals plus screen membership."""

    def __init__(self, path=None) -> None:
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- writes ---------------------------------------------------------- #
    def save(self, rows: Sequence[Dict[str, Any]], *, source: str,
             fetched_at: Optional[str] = None) -> Dict[str, int]:
        """Insert deals. Re-saving the same deal is a no-op (idempotent)."""
        ts = fetched_at or datetime.now().isoformat(timespec="seconds")
        counts = {"inserted": 0, "existing": 0}
        with closing(self._conn()) as conn:
            for r in rows:
                try:
                    conn.execute(
                        "INSERT INTO deals (trade_date, deal_type, symbol, client_name, "
                        "buy_sell, quantity, price, value_inr, source, fetched_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (r["trade_date"], r["deal_type"], r["symbol"],
                         r.get("client_name") or "", r.get("buy_sell") or "",
                         float(r["quantity"]), r.get("price"), r.get("value_inr"),
                         source, ts))
                    counts["inserted"] += 1
                except sqlite3.IntegrityError:
                    counts["existing"] += 1
            conn.commit()
        return counts

    def sync_membership(self, symbols: Sequence[str], *, today: Optional[date] = None
                        ) -> Dict[str, int]:
        """Update screen membership and mark departures. Soft delete only.

        For every symbol NOT in ``symbols`` that is currently active, stamp
        ``removed_at`` on its membership row and on all its deal rows.
        """
        day = (today or date.today()).isoformat()
        counts = {"added": 0, "still_in": 0, "removed": 0, "reopened": 0}
        wanted = [s.strip().upper() for s in symbols if str(s).strip()]

        with closing(self._conn()) as conn:
            active = {r["symbol"]: r for r in conn.execute(
                "SELECT * FROM screen_membership WHERE removed_at IS NULL")}

            for sym in wanted:
                row = active.get(sym)
                if row is None:
                    existing = conn.execute(
                        "SELECT * FROM screen_membership WHERE symbol=?", (sym,)).fetchone()
                    if existing is None:
                        conn.execute(
                            "INSERT INTO screen_membership (symbol, first_seen, last_seen) "
                            "VALUES (?,?,?)", (sym, day, day))
                        counts["added"] += 1
                    else:
                        # Back on the screen: reopen, keep the deal history.
                        conn.execute(
                            "UPDATE screen_membership SET removed_at=NULL, last_seen=? "
                            "WHERE symbol=?", (day, sym))
                        conn.execute(
                            "UPDATE deals SET removed_at=NULL WHERE symbol=?", (sym,))
                        counts["reopened"] += 1
                else:
                    conn.execute(
                        "UPDATE screen_membership SET last_seen=? WHERE symbol=?", (day, sym))
                    counts["still_in"] += 1

            now = datetime.now().isoformat(timespec="seconds")
            for sym in active:
                if sym in wanted:
                    continue
                conn.execute(
                    "UPDATE screen_membership SET removed_at=? WHERE symbol=?", (now, sym))
                conn.execute(
                    "UPDATE deals SET removed_at=? "
                    "WHERE symbol=? AND removed_at IS NULL", (now, sym))
                counts["removed"] += 1
            conn.commit()
        return counts

    # ---- reads ------------------------------------------------------------ #
    def active_deals(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Deals whose symbol is still on the screen (removed_at IS NULL)."""
        sql = "SELECT * FROM deals WHERE removed_at IS NULL"
        params: List[Any] = []
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol.strip().upper())
        sql += " ORDER BY trade_date DESC, symbol"
        with closing(self._conn()) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def all_deals(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every deal ever stored, including those for removed symbols."""
        sql = "SELECT * FROM deals"
        params: List[Any] = []
        if symbol:
            sql += " WHERE symbol=?"
            params.append(symbol.strip().upper())
        sql += " ORDER BY trade_date DESC, symbol"
        with closing(self._conn()) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def membership(self) -> List[Dict[str, Any]]:
        with closing(self._conn()) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM screen_membership ORDER BY symbol").fetchall()]

    def summary(self) -> Dict[str, Any]:
        """Dashboard view: counts that distinguish active from retained history."""
        active = self.active_deals()
        every = self.all_deals()
        return {
            "active_deals": len(active),
            "total_deals": len(every),
            "removed_deals": len(every) - len(active),
            "screen_symbols": sum(1 for m in self.membership() if not m["removed_at"]),
            "block": sum(1 for d in active if d["deal_type"] == "block"),
            "bulk": sum(1 for d in active if d["deal_type"] == "bulk"),
        }


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def sync_screen_membership(store: DealsStore, symbols: Sequence[str],
                           *, today: Optional[date] = None) -> Dict[str, int]:
    """Public wrapper so a caller does not need the class."""
    return store.sync_membership(symbols, today=today)


def fetch_and_store(store: Optional[DealsStore] = None, *,
                    symbols: Optional[Sequence[str]] = None,
                    trade_date: Optional[date] = None,
                    http_get=None, prefer: Optional[str] = None,
                    screen_path=None) -> Dict[str, Any]:
    """Fetch block+bulk deals for the screened universe, store them, sync membership.

    Returns a report::

        {"ok": bool, "symbols": int, "block": {...}, "bulk": {...},
         "membership": {...}, "detail": str}

    An EMPTY screen does nothing and says so: fetching for a universe we cannot
    verify would store rows nobody asked for.
    """
    store = store or DealsStore()

    if symbols is None:
        symbols = nse_mcp_client._screened_symbols(screen_path)
    symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]

    membership = store.sync_membership(symbols, today=trade_date)

    if not symbols:
        return {"ok": False, "symbols": 0, "block": {}, "bulk": {},
                "membership": membership,
                "detail": "screened universe is empty; nothing fetched"}

    block = nse_mcp_client.fetch_block_deals(
        trade_date=trade_date, http_get=http_get, prefer=prefer)
    bulk = nse_mcp_client.fetch_bulk_deals(
        trade_date=trade_date, http_get=http_get, prefer=prefer)

    wanted = set(symbols)
    # Only deals for symbols we actually screen are stored.
    block_rows = [r for r in block.get("rows") or [] if r["symbol"] in wanted]
    bulk_rows = [r for r in bulk.get("rows") or [] if r["symbol"] in wanted]

    b_counts = store.save(block_rows, source=block.get("source", "unknown")) if block_rows else {}
    u_counts = store.save(bulk_rows, source=bulk.get("source", "unknown")) if bulk_rows else {}

    ok = bool(block.get("ok") or bulk.get("ok"))
    return {
        "ok": ok,
        "symbols": len(symbols),
        "block": {"found": len(block.get("rows") or []),
                  "stored": len(block_rows), "source": block.get("source"),
                  "counts": b_counts, "detail": block.get("detail", "")},
        "bulk": {"found": len(bulk.get("rows") or []),
                 "stored": len(bulk_rows), "source": bulk.get("source"),
                 "counts": u_counts, "detail": bulk.get("detail", "")},
        "membership": membership,
        "detail": "" if ok else "both sources failed; nothing stored",
    }


def active_deals(store: Optional[DealsStore] = None, symbol: Optional[str] = None
                 ) -> List[Dict[str, Any]]:
    """Deals for symbols still on the screen."""
    return (store or DealsStore()).active_deals(symbol)


def deals_for_symbol(symbol: str, store: Optional[DealsStore] = None) -> List[Dict[str, Any]]:
    """Every deal ever stored for one symbol, including after removal."""
    return (store or DealsStore()).all_deals(symbol)
