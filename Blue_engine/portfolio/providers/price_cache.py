"""
providers/price_cache.py - persistent OHLCV store (SQLite, stdlib only).

Why this exists
---------------
``client.history()`` gives you history *on request*. If the engine only ever asks
for a rolling 400-day window, you never accumulate a dataset you can walk-forward
test on. This module fixes that: **every fetch is persisted**, so the dataset
grows by one day per day automatically.

That matters because the backtest must run on the *same prices you trade on*.
A backtest on scraped or vendor data that disagrees with your broker's feed is
worse than no backtest.

Design
------
* **SQLite** (stdlib ``sqlite3``) - zero dependency, crash-safe, single file.
* **Idempotent upsert** - re-fetching overlapping windows never duplicates rows;
  ``PRIMARY KEY (symbol, exchange, interval, ts)`` + ``ON CONFLICT DO UPDATE``.
* **Best-effort writes** - a disk error is logged and swallowed. Bookkeeping must
  never abort a trading run (same rule as ``core/ledger.py`` and ``run_history``).
* **Read-back helpers** - ``closes()`` / ``panel()`` return exactly the series the
  covariance/backtest code wants, so the cache is a drop-in price source.

Schema::

    CREATE TABLE ohlcv (
        symbol    TEXT NOT NULL,
        exchange  TEXT NOT NULL,
        interval  TEXT NOT NULL,
        ts        TEXT NOT NULL,      -- ISO date/datetime
        open      REAL, high REAL, low REAL, close REAL, volume REAL,
        fetched_at REAL,
        PRIMARY KEY (symbol, exchange, interval, ts)
    )

The table is intentionally narrow: everything the risk model, the volatility
estimators and the backtest need is here, and nothing else.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# <repo>/data/price_cache/prices.db
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = _REPO_ROOT / "data" / "price_cache" / "prices.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv (
    symbol    TEXT NOT NULL,
    exchange  TEXT NOT NULL,
    interval  TEXT NOT NULL,
    ts        TEXT NOT NULL,
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL,
    volume    REAL,
    fetched_at REAL,
    PRIMARY KEY (symbol, exchange, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_lookup
    ON ohlcv (symbol, exchange, interval, ts);
"""

__all__ = ["PriceCache", "DEFAULT_DB", "normalise_rows"]


def normalise_rows(rows: Sequence[Any]) -> List[Dict[str, Any]]:
    """Normalise SDK history rows into dicts with a ``ts`` and ``close``.

    Accepts the shapes the OpenAlgo SDK (and most brokers) return:

    * ``{"timestamp"/"date"/"time": ..., "open": ..., "close": ...}``
    * ``[timestamp, open, high, low, close, volume]`` (the OHLCV list form)

    Rows with no usable close are dropped. When a row carries **no timestamp**
    at all we synthesise a positional index (``row-0001``) rather than dropping
    it: some brokers omit the field, and the risk model only needs an ordered
    close series. Rows *with* a timestamp keep it, so real bars win on upsert.

    Never raises.
    """
    out: List[Dict[str, Any]] = []
    for i, r in enumerate(rows or []):
        try:
            if isinstance(r, dict):
                ts = (r.get("timestamp") or r.get("date") or r.get("time")
                      or r.get("ts"))
                close = r.get("close")
                rec = {
                    "ts": str(ts) if ts is not None else f"row-{i:06d}",
                    "open": _f(r.get("open")), "high": _f(r.get("high")),
                    "low": _f(r.get("low")), "close": _f(close),
                    "volume": _f(r.get("volume")),
                }
            elif isinstance(r, (list, tuple)) and len(r) >= 5:
                ts = r[0]
                rec = {"ts": str(ts) if ts is not None else f"row-{i:06d}",
                       "open": _f(r[1]), "high": _f(r[2]),
                       "low": _f(r[3]), "close": _f(r[4]),
                       "volume": _f(r[5]) if len(r) > 5 else None}
            else:
                continue
            if rec["ts"] in ("", "None") or rec["close"] is None:
                continue
            out.append(rec)
        except Exception:  # noqa: BLE001 - one bad row must not stop the batch
            continue
    return out


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


class PriceCache:
    """SQLite-backed OHLCV store. Every method is best-effort (never raises)."""

    def __init__(self, db_path: Optional[Path] = None,
                 log: Optional[Any] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self._log = log
        self._init_schema()

    # ---- lifecycle ------------------------------------------------------ #
    def _log_msg(self, msg: str) -> None:
        if callable(self._log):
            self._log(f"[price_cache] {msg}")
        else:
            logger.info("[price_cache] %s", msg)

    def _connect(self) -> Optional[sqlite3.Connection]:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            conn.execute("PRAGMA journal_mode=WAL")   # concurrent readers
            return conn
        except Exception as exc:  # noqa: BLE001
            logger.warning("price_cache connect failed: %s", exc)
            return None

    def _init_schema(self) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("price_cache schema init failed: %s", exc)
        finally:
            conn.close()

    # ---- write ---------------------------------------------------------- #
    def upsert(self, symbol: str, exchange: str, interval: str,
               rows: Sequence[Any]) -> int:
        """Insert/update OHLCV rows. Returns the number of rows written.

        Idempotent: re-fetching an overlapping window updates in place rather
        than duplicating, so the cache can be safely refreshed daily.
        """
        records = normalise_rows(rows)
        if not records:
            return 0
        conn = self._connect()
        if conn is None:
            return 0
        now = time.time()
        written = 0
        try:
            for rec in records:
                try:
                    conn.execute(
                        "INSERT INTO ohlcv "
                        "(symbol, exchange, interval, ts, open, high, low, "
                        " close, volume, fetched_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(symbol, exchange, interval, ts) DO UPDATE SET "
                        "open=excluded.open, high=excluded.high, "
                        "low=excluded.low, close=excluded.close, "
                        "volume=excluded.volume, fetched_at=excluded.fetched_at",
                        (symbol, exchange, interval, rec["ts"], rec["open"],
                         rec["high"], rec["low"], rec["close"], rec["volume"],
                         now))
                    written += 1
                except Exception:  # noqa: BLE001 - skip a bad row
                    continue
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("price_cache upsert failed: %s", exc)
        finally:
            conn.close()
        return written

    def upsert_many(self, panel: Dict[Tuple[str, str, str], Sequence[Any]]
                    ) -> Dict[str, int]:
        """Bulk upsert. Key is ``(symbol, exchange, interval)``."""
        out: Dict[str, int] = {}
        for (sym, exch, interval), rows in (panel or {}).items():
            out[f"{sym}/{exch}/{interval}"] = self.upsert(sym, exch, interval, rows)
        return out

    # ---- read ----------------------------------------------------------- #
    def closes(self, symbol: str, exchange: str, interval: str = "D",
               *, limit: Optional[int] = None) -> List[float]:
        """Ordered close series (oldest first) for one instrument."""
        rows = self.rows(symbol, exchange, interval, limit=limit)
        return [r["close"] for r in rows if r.get("close") is not None]

    def rows(self, symbol: str, exchange: str, interval: str = "D",
             *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Ordered OHLCV rows (oldest first) for one instrument."""
        conn = self._connect()
        if conn is None:
            return []
        sql = ("SELECT ts, open, high, low, close, volume FROM ohlcv "
               "WHERE symbol=? AND exchange=? AND interval=? ORDER BY ts ASC")
        try:
            cur = conn.execute(sql, (symbol, exchange, interval))
            out = [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3],
                    "close": r[4], "volume": r[5]} for r in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            logger.debug("price_cache read failed: %s", exc)
            return []
        finally:
            conn.close()
        return out[-limit:] if limit else out

    def panel(self, instruments: Sequence[Tuple[str, str]], interval: str = "D",
              *, limit: Optional[int] = None
              ) -> Dict[str, List[float]]:
        """``{symbol: closes}`` for many instruments.

        ``instruments`` is a sequence of ``(symbol, exchange)`` pairs. Keys are
        the bare symbols (the sleeve names the portfolio engine uses).
        """
        out: Dict[str, List[float]] = {}
        for sym, exch in instruments or []:
            cs = self.closes(sym, exch, interval, limit=limit)
            if cs:
                out[sym] = cs
        return out

    def trading_dates(self, symbol: str, exchange: str, interval: str = "D"
                      ) -> List[str]:
        """The timestamp series for one instrument (used to align panels)."""
        return [r["ts"] for r in self.rows(symbol, exchange, interval)]

    # ---- introspection --------------------------------------------------- #
    def coverage(self, symbol: str, exchange: str,
                 interval: str = "D") -> Dict[str, Any]:
        """``{n, first, last}`` for one instrument - the cache's health report."""
        rows = self.rows(symbol, exchange, interval)
        return {"n": len(rows),
                "first": rows[0]["ts"] if rows else None,
                "last": rows[-1]["ts"] if rows else None}

    def summary(self) -> List[Dict[str, Any]]:
        """Per-instrument coverage for every instrument in the cache."""
        conn = self._connect()
        if conn is None:
            return []
        try:
            cur = conn.execute(
                "SELECT symbol, exchange, interval, COUNT(*), MIN(ts), MAX(ts) "
                "FROM ohlcv GROUP BY symbol, exchange, interval "
                "ORDER BY symbol, exchange")
            return [{"symbol": r[0], "exchange": r[1], "interval": r[2],
                     "n": r[3], "first": r[4], "last": r[5]}
                    for r in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            logger.debug("price_cache summary failed: %s", exc)
            return []
        finally:
            conn.close()

    def available(self) -> bool:
        """True when the DB file exists and holds at least one row."""
        if not self.db_path.is_file():
            return False
        conn = self._connect()
        if conn is None:
            return False
        try:
            cur = conn.execute("SELECT COUNT(*) FROM ohlcv")
            return int(cur.fetchone()[0]) > 0
        except Exception:  # noqa: BLE001
            return False
        finally:
            conn.close()