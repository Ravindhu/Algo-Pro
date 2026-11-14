"""NSE FII/DII flow data: fetch, persist and serve for analysis.

Flow stage: India flow data
Status: PHASE 2 IMPLEMENTED (2026-11-14)
Existing code to reuse or wrap (do not copy):
    src/quant/blue_engine/economics/providers/fii_provider.py
    (FiiFlowProvider._parse_fii_dii - the payload normaliser, wrapped below;
     its vendor-parquet reader stays the primary offline source).
    Bulk/block deals and the semantic search layer are out of scope here.

WHAT THIS MODULE IS FOR
-----------------------
The Stage 2 audit found India flow data was documented as implemented but had
NO data path: `market_inputs.py` claimed to wrap `fii_provider.py` and never
imported it, and the engine's primary source (a vendor parquet clone) is
gitignored and absent. So the flow stage produced nothing.

This module gives it a real path, in this order:

  1. **Database**  - `data/fii/fii_dii.sqlite`. Read first, always.
  2. **NSE fetch** - `https://www.nseindia.com/api/fiidiiTradeReact`, run by
     `scripts/ops/fetch_fii_daily.py` (evening, or on demand).
  3. **Nothing**   - an honest empty result with a reason.

The database first is deliberate. Analysis must never block on the network, and
a backtest must see the same numbers twice. A fetch happens on a schedule, not
inside a research call.

DATA CAVEATS, STATED
--------------------
- NSE's `fiidiiTradeReact` returns the CURRENT session's provisional figures,
  usually published after the close. It is not a history: you cannot ask it for
  a past date. That is exactly why this module persists what it fetches.
- The figures are provisional and revised. This module keeps the first value it
  saw for a date and records a `revision` row when a later fetch disagrees, so a
  revision is visible rather than silently overwriting history.
- NSE requires a browser-like `User-Agent` and a warm cookie for this endpoint.
  A cold request often returns 401/403. The fetcher reports that plainly instead
  of retrying in a loop.

FAIL-CLOSED RULES
-----------------
- No data for a date means `None`, never 0. A zero FII flow is a real number and
  must not be confused with "we do not know".
- A read failure raises in `latest()` only when `strict=True`; otherwise it
  returns an empty, labelled result so a panel can render.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from quant.core.blue_bridge import BlueEngineUnavailable, load

logger = logging.getLogger(__name__)

__all__ = [
    "NSE_FII_URL",
    "FiiStore",
    "default_db_path",
    "parse_fii_dii",
    "fetch_fii_dii",
    "save_flows",
    "latest",
    "history",
    "staleness",
]

#: The NSE endpoint. Returns the current session's provisional FII/DII figures.
NSE_FII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"

#: NSE rejects a default python-requests User-Agent. This is a normal browser UA.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

#: Beyond this age the flow data is reported stale. Two sessions covers a
#: weekend plus one holiday.
STALE_AFTER_DAYS = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fii_dii (
    trade_date TEXT NOT NULL,
    category   TEXT NOT NULL,          -- FII | DII
    buy_value  REAL,
    sell_value REAL,
    net_value  REAL,
    fetched_at TEXT NOT NULL,
    source     TEXT NOT NULL,          -- nse | vendor
    raw        TEXT,
    PRIMARY KEY (trade_date, category)
);
CREATE TABLE IF NOT EXISTS revisions (
    trade_date TEXT NOT NULL,
    category   TEXT NOT NULL,
    old_net    REAL,
    new_net    REAL,
    seen_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fii_date ON fii_dii(trade_date);
"""


def default_db_path() -> Path:
    """Repo-relative default: data/fii/fii_dii.sqlite.

    Overridable with ``QUANT_FII_DB`` so a test or a replay never touches the
    working tree.
    """
    override = os.getenv("QUANT_FII_DB")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "data" / "fii" / "fii_dii.sqlite"


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def _engine_parser():
    """The engine's payload normaliser, so the two never disagree."""
    try:
        return load("economics.providers.fii_provider").FiiFlowProvider._parse_fii_dii
    except BlueEngineUnavailable:
        return None


def parse_fii_dii(payload: Any, *, trade_date: Optional[date] = None
                  ) -> List[Dict[str, Any]]:
    """Normalise an NSE ``fiidiiTradeReact`` payload into rows this module stores.

    The NSE shape is a list of ``{"category": "FII/FPI *", "date": "14-Nov-2026",
    "buyValue": "1,234.56", "sellValue": "...", "netValue": "..."}``. Values
    carry thousands separators, so they are cleaned here.

    Returns ``[{trade_date, category, buy_value, sell_value, net_value}]``.
    Unparseable rows are SKIPPED and counted, never guessed.
    """
    rows: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("records") or [payload]
    if not isinstance(payload, (list, tuple)):
        return rows

    for rec in payload:
        if not isinstance(rec, dict):
            continue
        category = _normalise_category(rec.get("category"))
        if category is None:
            continue
        day = _parse_day(rec.get("date")) or trade_date
        if day is None:
            continue
        rows.append({
            "trade_date": day.isoformat(),
            "category": category,
            "buy_value": _num(rec.get("buyValue")),
            "sell_value": _num(rec.get("sellValue")),
            "net_value": _num(rec.get("netValue")),
        })
    return rows


def _normalise_category(value: Any) -> Optional[str]:
    """'FII/FPI *' -> 'FII', 'DII **' -> 'DII'. Anything else -> None."""
    text = str(value or "").strip().upper()
    if text.startswith("FII"):
        return "FII"
    if text.startswith("DII"):
        return "DII"
    return None


def _num(value: Any) -> Optional[float]:
    """'1,234.56' -> 1234.56. None on anything unparseable, never 0."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_day(value: Any) -> Optional[date]:
    """'14-Nov-2026' or ISO. None when unparseable."""
    if value is None:
        return None
    text = str(value).strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def fetch_fii_dii(url: str = NSE_FII_URL, *, timeout: float = 15.0,
                  http_get=None, prefer: Optional[str] = None) -> Dict[str, Any]:
    """Fetch the current provisional FII/DII figures from NSE.

    Tries the source adapter first (``data/nse_mcp_client``), which uses nse-mcp
    or jugaad-data when installed and falls back to the direct NSE call. nse-mcp
    has NO FII tool, so in practice this resolves to the direct endpoint - the
    adapter reports which source actually answered.

    Returns::

        {"ok": bool, "rows": [...], "status": int|None, "detail": str,
         "fetched_at": iso, "source": str}

    Never raises: a network or parse failure is a reported result, because the
    caller runs on a schedule and a raised exception would just be logged twice.
    """
    if url == NSE_FII_URL:
        try:
            from quant.data.nse_mcp_client import fetch_fii_dii as _via_adapter
            got = _via_adapter(timeout=timeout, http_get=http_get, prefer=prefer)
            return {"ok": bool(got.get("ok")), "rows": got.get("rows") or [],
                    "status": None, "detail": got.get("detail") or "",
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "source": got.get("source", "nse_json")}
        except Exception:  # noqa: BLE001 - fall through to the direct path below
            pass
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
    except Exception as exc:  # noqa: BLE001 - network failure is data, not a crash
        return {"ok": False, "rows": [], "status": None,
                "detail": f"request failed: {exc!r}", "fetched_at": now}

    status = getattr(resp, "status_code", None)
    if status is not None and status != 200:
        hint = ("NSE often returns 401/403 without a warm session cookie; "
                "retry from a browser session or use the vendor parquet")
        return {"ok": False, "rows": [], "status": status,
                "detail": f"HTTP {status}. {hint}", "fetched_at": now}

    try:
        payload = resp.json() if hasattr(resp, "json") else resp
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "rows": [], "status": status,
                "detail": f"response is not JSON: {exc!r}", "fetched_at": now}

    rows = parse_fii_dii(payload)
    if not rows:
        # Try the engine's normaliser before declaring the payload unusable.
        parser = _engine_parser()
        if parser is not None:
            try:
                parsed = parser(payload) or {}
                net = parsed.get("net_cash")
                if net is not None:
                    rows = [{"trade_date": date.today().isoformat(), "category": "FII",
                             "buy_value": None, "sell_value": None,
                             "net_value": float(net)}]
            except Exception:  # noqa: BLE001
                pass

    if not rows:
        return {"ok": False, "rows": [], "status": status,
                "detail": "payload parsed to zero rows", "fetched_at": now}
    return {"ok": True, "rows": rows, "status": status, "detail": "",
            "fetched_at": now}


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
class FiiStore:
    """SQLite store for FII/DII flows. Append-only in practice.

    A row is keyed by (trade_date, category). Re-fetching the same date does NOT
    overwrite: if the net value differs, the ORIGINAL is kept and a revision row
    records both. History that silently changes cannot be audited.
    """

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
        """Insert rows, recording a revision when a value disagrees.

        Returns ``{"inserted": n, "unchanged": n, "revised": n}``.
        """
        ts = fetched_at or datetime.now().isoformat(timespec="seconds")
        counts = {"inserted": 0, "unchanged": 0, "revised": 0}
        with closing(self._conn()) as conn:
            for row in rows:
                trade_date = row.get("trade_date")
                category = row.get("category")
                if not trade_date or not category:
                    continue
                existing = conn.execute(
                    "SELECT net_value FROM fii_dii WHERE trade_date=? AND category=?",
                    (trade_date, category)).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO fii_dii (trade_date, category, buy_value, "
                        "sell_value, net_value, fetched_at, source, raw) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (trade_date, category, row.get("buy_value"),
                         row.get("sell_value"), row.get("net_value"), ts, source,
                         json.dumps(row, default=str)))
                    counts["inserted"] += 1
                    continue
                old_net = existing["net_value"]
                new_net = row.get("net_value")
                if old_net is None or new_net is None or float(old_net) == float(new_net):
                    counts["unchanged"] += 1
                    continue
                # Keep the original; record the disagreement.
                conn.execute(
                    "INSERT INTO revisions (trade_date, category, old_net, new_net, seen_at) "
                    "VALUES (?,?,?,?,?)", (trade_date, category, old_net, new_net, ts))
                counts["revised"] += 1
            conn.commit()
        return counts

    def latest(self, category: str = "FII", *, strict: bool = False) -> Optional[Dict[str, Any]]:
        """Most recent stored row for a category. None when the store is empty."""
        try:
            with closing(self._conn()) as conn:
                row = conn.execute(
                    "SELECT * FROM fii_dii WHERE category=? ORDER BY trade_date DESC LIMIT 1",
                    (category.upper(),)).fetchone()
            return dict(row) if row else None
        except sqlite3.Error as exc:
            if strict:
                raise
            logger.warning("fii latest read failed: %r", exc)
            return None

    def history(self, start: Optional[str] = None,
                end: Optional[str] = None) -> List[Dict[str, Any]]:
        """Stored rows between two ISO dates, oldest first."""
        clauses, params = [], []
        if start:
            clauses.append("trade_date >= ?")
            params.append(start)
        if end:
            clauses.append("trade_date <= ?")
            params.append(end)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                f"SELECT * FROM fii_dii{where} ORDER BY trade_date, category",
                params).fetchall()
        return [dict(r) for r in rows]

    def revisions(self) -> List[Dict[str, Any]]:
        """Every recorded disagreement between an original and a later fetch."""
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT * FROM revisions ORDER BY seen_at").fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# module-level helpers used by analysis
# --------------------------------------------------------------------------- #
def save_flows(rows: Sequence[Dict[str, Any]], *, source: str = "nse",
               path=None) -> Dict[str, int]:
    """Persist rows into the default (or given) store."""
    return FiiStore(path).save(rows, source=source)


def latest(category: str = "FII", *, path=None, strict: bool = False
           ) -> Optional[Dict[str, Any]]:
    """Newest stored flow value. None when nothing has been saved yet."""
    return FiiStore(path).latest(category, strict=strict)


def history(start: Optional[str] = None, end: Optional[str] = None, *,
            path=None) -> List[Dict[str, Any]]:
    """Stored flows between two ISO dates, oldest first."""
    return FiiStore(path).history(start, end)


def staleness(row: Optional[Dict[str, Any]], *, today: Optional[date] = None,
              max_age_days: int = STALE_AFTER_DAYS) -> Dict[str, Any]:
    """Age of a stored row. ``{"known": bool, "age_days": int|None, "stale": bool}``.

    A missing row is NOT fresh: ``known=False`` and ``stale=True``, so a caller
    that gates on ``stale`` fails closed.
    """
    if not row:
        return {"known": False, "age_days": None, "stale": True,
                "note": "no flow data stored"}
    try:
        seen = date.fromisoformat(str(row.get("trade_date")))
    except (TypeError, ValueError):
        return {"known": False, "age_days": None, "stale": True,
                "note": "stored row has an unparseable date"}
    ref = today or date.today()
    age = (ref - seen).days
    stale = age > max_age_days
    return {"known": True, "age_days": age, "stale": stale,
            "note": (f"flows are {age} days old; limit {max_age_days}"
                     if stale else "fresh")}
