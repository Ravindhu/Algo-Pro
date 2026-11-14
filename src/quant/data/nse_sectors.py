"""NSE sector and index levels: weekly fetch, persist, serve for relative strength.

Flow stage: Relative strength, India flow data (sector half)
Status: PHASE 2 IMPLEMENTED (2026-11-14)
Existing code to reuse or wrap (do not copy):
    src/quant/blue_engine/sectors/sector_screener_map.py (INDEX_TO_SCREENER_SECTORS
        - the sector -> constituent mapping, so sectors are never invented here)
    src/quant/blue_engine/economics/providers/fii_provider.py (the ``allIndices``
        endpoint and the browser-UA header rule already used for FII)

WHY A SEPARATE WEEKLY STORE
---------------------------
Sector relative strength needs a HISTORY of index levels. NSE's ``allIndices``
returns one snapshot (today's level per index), not a series, so a weekly cron
that saves each snapshot is what turns it into history. Weekly is enough because
Mansfield RS is computed on weekly bars.

DESIGN
------
- Read: database first, always. Analysis never waits on the network.
- Write: ``scripts/ops/fetch_sectors_weekly.py``, Saturday after the close.
- One row per (trade_date, index_name). Re-fetching the same day is idempotent;
  a changed level for a date already stored is kept as the ORIGINAL, with the
  disagreement recorded - the same rule as the FII store, for the same reason.

FAIL-CLOSED RULES
-----------------
- A missing index is absent, never 0.0. A zero index level would silently rank
  that sector worst in every relative-strength calculation.
- Too few weeks of history returns ``insufficient_history`` rather than a
  made-up score.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "NSE_ALL_INDICES_URL",
    "SectorStore",
    "default_db_path",
    "parse_all_indices",
    "fetch_sector_levels",
    "save_levels",
    "levels",
    "weekly_series",
    "MIN_WEEKS_FOR_RS",
]

#: Every NSE index with its current level, in one call.
NSE_ALL_INDICES_URL = "https://www.nseindia.com/api/allIndices"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

#: Mansfield RS needs enough weekly points to mean anything. Four weeks is a
#: month: below that a "trend" is one lucky week.
MIN_WEEKS_FOR_RS = 4

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS index_levels (
    trade_date TEXT NOT NULL,
    index_name TEXT NOT NULL,
    last       REAL,
    open       REAL,
    high       REAL,
    low        REAL,
    change_pct REAL,
    fetched_at TEXT NOT NULL,
    source     TEXT NOT NULL,
    PRIMARY KEY (trade_date, index_name)
);
CREATE TABLE IF NOT EXISTS index_revisions (
    trade_date TEXT NOT NULL,
    index_name TEXT NOT NULL,
    old_last   REAL,
    new_last   REAL,
    seen_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_levels_name ON index_levels(index_name);
"""


def default_db_path() -> Path:
    """Repo-relative default: data/sectors/sector_levels.sqlite.

    Overridable with ``QUANT_SECTOR_DB`` so a test never touches the working tree.
    """
    override = os.getenv("QUANT_SECTOR_DB")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "data" / "sectors" / "sector_levels.sqlite"


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def _num(value: Any) -> Optional[float]:
    """'12,345.60' -> 12345.6. None on anything unparseable, NEVER 0."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_all_indices(payload: Any, *, trade_date: Optional[date] = None
                      ) -> List[Dict[str, Any]]:
    """Normalise the NSE ``allIndices`` payload.

    The shape is ``{"data": [{"index": "NIFTY 50", "last": "23,456.70",
    "open": ..., "high": ..., "low": ..., "percentChange": "0.45", ...}]}``.

    Returns ``[{index_name, last, open, high, low, change_pct, trade_date}]``.
    An index with no usable ``last`` is SKIPPED: a missing level must stay
    missing rather than become a 0 that ranks last in every RS calculation.
    """
    rows: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("records") or []
    if not isinstance(payload, (list, tuple)):
        return rows

    day = (trade_date or date.today()).isoformat()
    for rec in payload:
        if not isinstance(rec, dict):
            continue
        name = str(rec.get("index") or rec.get("indexName") or "").strip()
        last = _num(rec.get("last") or rec.get("lastPrice") or rec.get("close"))
        if not name or last is None:
            continue
        rows.append({
            "trade_date": day,
            "index_name": name.upper(),
            "last": last,
            "open": _num(rec.get("open")),
            "high": _num(rec.get("high")),
            "low": _num(rec.get("low")),
            "change_pct": _num(rec.get("percentChange") or rec.get("change")),
        })
    return rows


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def fetch_sector_levels(url: str = NSE_ALL_INDICES_URL, *, timeout: float = 15.0,
                        http_get=None, trade_date: Optional[date] = None
                        ) -> Dict[str, Any]:
    """Fetch every NSE index level. Never raises; a failure is a reported result."""
    now = datetime.now().isoformat(timespec="seconds")
    getter = http_get
    if getter is None:
        try:
            import requests
            getter = requests.get
        except ImportError:
            return {"ok": False, "rows": [], "status": None,
                    "detail": "requests is not installed", "fetched_at": now}

    headers = {"User-Agent": _UA, "Accept": "application/json, text/plain, */*",
               "Accept-Language": "en-US,en;q=0.9",
               "Referer": "https://www.nseindia.com/"}
    try:
        resp = getter(url, timeout=timeout, headers=headers)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "rows": [], "status": None,
                "detail": f"request failed: {exc!r}", "fetched_at": now}

    status = getattr(resp, "status_code", None)
    if status is not None and status != 200:
        return {"ok": False, "rows": [], "status": status,
                "detail": f"HTTP {status}. NSE needs a warm session cookie for "
                          f"allIndices; use the vendor parquet or a browser session",
                "fetched_at": now}

    try:
        payload = resp.json() if hasattr(resp, "json") else resp
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "rows": [], "status": status,
                "detail": f"response is not JSON: {exc!r}", "fetched_at": now}

    rows = parse_all_indices(payload, trade_date=trade_date)
    if not rows:
        return {"ok": False, "rows": [], "status": status,
                "detail": "payload parsed to zero index rows", "fetched_at": now}
    return {"ok": True, "rows": rows, "status": status, "detail": "",
            "fetched_at": now}


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
class SectorStore:
    """SQLite store for index levels. Append-only; revisions are recorded."""

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

    def save(self, rows: Sequence[Dict[str, Any]], *, source: str = "nse",
             fetched_at: Optional[str] = None) -> Dict[str, int]:
        """Insert levels; record a revision on disagreement. Idempotent per day."""
        ts = fetched_at or datetime.now().isoformat(timespec="seconds")
        counts = {"inserted": 0, "unchanged": 0, "revised": 0}
        with closing(self._conn()) as conn:
            for row in rows:
                day, name = row.get("trade_date"), row.get("index_name")
                if not day or not name:
                    continue
                existing = conn.execute(
                    "SELECT last FROM index_levels WHERE trade_date=? AND index_name=?",
                    (day, name)).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO index_levels (trade_date, index_name, last, open, "
                        "high, low, change_pct, fetched_at, source) VALUES (?,?,?,?,?,?,?,?,?)",
                        (day, name, row.get("last"), row.get("open"), row.get("high"),
                         row.get("low"), row.get("change_pct"), ts, source))
                    counts["inserted"] += 1
                    continue
                old_last, new_last = existing["last"], row.get("last")
                if old_last is None or new_last is None or float(old_last) == float(new_last):
                    counts["unchanged"] += 1
                    continue
                conn.execute(
                    "INSERT INTO index_revisions (trade_date, index_name, old_last, "
                    "new_last, seen_at) VALUES (?,?,?,?,?)",
                    (day, name, old_last, new_last, ts))
                counts["revised"] += 1
            conn.commit()
        return counts

    def levels(self, trade_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """Levels for one date (default: the newest date stored)."""
        with closing(self._conn()) as conn:
            if trade_date is None:
                head = conn.execute(
                    "SELECT MAX(trade_date) AS d FROM index_levels").fetchone()
                trade_date = head["d"] if head else None
            if trade_date is None:
                return []
            rows = conn.execute(
                "SELECT * FROM index_levels WHERE trade_date=? ORDER BY index_name",
                (trade_date,)).fetchall()
        return [dict(r) for r in rows]

    def weekly_series(self, index_name: str) -> List[Dict[str, Any]]:
        """Every stored level for one index, oldest first.

        The saved snapshots ARE the series; one row per weekly fetch.
        """
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT trade_date, last, change_pct FROM index_levels "
                "WHERE index_name=? ORDER BY trade_date", (index_name.upper(),)).fetchall()
        return [dict(r) for r in rows]

    def revisions(self) -> List[Dict[str, Any]]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT * FROM index_revisions ORDER BY seen_at").fetchall()
        return [dict(r) for r in rows]

    def index_names(self) -> List[str]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT DISTINCT index_name FROM index_levels ORDER BY index_name").fetchall()
        return [r["index_name"] for r in rows]


# --------------------------------------------------------------------------- #
# module helpers
# --------------------------------------------------------------------------- #
def save_levels(rows: Sequence[Dict[str, Any]], *, source: str = "nse",
                path=None) -> Dict[str, int]:
    """Persist levels into the default (or given) store."""
    return SectorStore(path).save(rows, source=source)


def levels(trade_date: Optional[str] = None, *, path=None) -> List[Dict[str, Any]]:
    """Index levels for a date, newest stored date by default."""
    return SectorStore(path).levels(trade_date)


def weekly_series(index_name: str, *, path=None) -> Dict[str, Any]:
    """Relative-strength input for one index, with an explicit readiness verdict.

    Returns ``{"index": name, "weeks": int, "ready": bool, "levels": [...],
    "detail": str}``. ``ready`` is False below :data:`MIN_WEEKS_FOR_RS`, so a
    caller cannot compute a trend from two points by accident.
    """
    series = SectorStore(path).weekly_series(index_name)
    n = len(series)
    ready = n >= MIN_WEEKS_FOR_RS
    return {
        "index": str(index_name).upper(),
        "weeks": n,
        "ready": ready,
        "levels": series,
        "detail": ("ready" if ready
                   else f"insufficient_history: {n} week(s), need {MIN_WEEKS_FOR_RS}"),
    }
