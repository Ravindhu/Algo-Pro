"""
data_cache.py - download -> cache -> load bars for the optimizer engine.

The optimizer engine today drives a strategy against the synthetic fixture in
``Blue_engine/optimizer/spaces.py``. The new optimizer page header (Phase 4 of
``Temp/OPTIMIZER_HEADER_PLAN.md``) lets the operator pick a real symbol,
strategy, and time period; the engine must then run against the operator's
data, not synthetic. This module is the single boundary between the
operator's data and the engine's ``bars`` argument.

DESIGN PRINCIPLES
=================

1. **Reuse, do not duplicate.** The persistent OHLCV store is
   ``blue_engine.portfolio.providers.price_cache.PriceCache`` (the SQLite
   file the rest of the system already reads from). The OpenAlgo client
   resolution goes through
   ``blue_engine.portfolio.providers.factory.get_client()``. The history
   normaliser is ``price_cache.normalise_rows``. New code calls those; it
   does not re-implement them.

2. **Read-only contract is inherited.** This module writes to the local
   SQLite cache (``data/optimizer/ohlcv.sqlite`` by default) but never to
   the broker, the live system, or any live config. The Optimizer page
   header uses this module to populate the cache; the live trading system
   never reads from it.

3. **Failure is informative.** Every public method returns a small typed
   result object (``DownloadResult``, ``LoadResult``, ``StatusResult``)
   with ``ok: bool`` and a ``reason: Optional[str]``. The dashboard reads
   these to render honest "broker offline" / "no data" panels; the engine
   reads them to decide whether to run a study or short-circuit with a
   clear reason.

4. **The 30-day cap is honest, not silent.** OpenAlgo's ``history`` endpoint
   has rolling caps documented in ``Blue_engine/ml/data_sources/openalgo_source.py``
   (lines 73-83). If the operator asks for a 5-year window on a contract
   whose cap is 30 days, the cache is pruned to the most recent 30 days and
   a warning is appended to ``DownloadResult.warnings``. The engine still
   runs on the pruned window; the dashboard surfaces the warning so the
   operator can extend the cache one day at a time.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Default cache location. The portfolio provider uses
# ``data/optimizer/ohlcv.sqlite``; we mirror that to keep the dashboard and
# the portfolio engine reading from the same file.
DEFAULT_CACHE_DIR = Path("data/optimizer")
DEFAULT_CACHE_DB = DEFAULT_CACHE_DIR / "ohlcv.sqlite"

# NSE cash only (per the locked answer #6 in OPTIMIZER_HEADER_PLAN.md).
DEFAULT_EXCHANGE = "NSE"

# The 30-day rolling cap is a documented OpenAlgo constraint; we do not
# pretend it doesn't exist. When the requested window exceeds the cap, the
# cache is pruned to the most recent ``MAX_WINDOW_DAYS`` days.
MAX_WINDOW_DAYS = 365

# The contract-list cache lives next to the bars cache; refresh once a day.
CONTRACT_LIST_TTL_S = 24 * 60 * 60


# --------------------------------------------------------------------------- #
# Result types (the dashboard's contract; do not change field names)
# --------------------------------------------------------------------------- #


@dataclass
class DownloadResult:
    """Outcome of a ``OptimizerDataCache.download(...)`` call."""
    ok: bool
    symbol: str
    exchange: str
    interval: str
    n_written: int = 0
    n_existing: int = 0
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    reason: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


@dataclass
class LoadResult:
    """Outcome of a ``OptimizerDataCache.load(...)`` call."""
    ok: bool
    symbol: str
    exchange: str
    interval: str
    start: str
    end: str
    n_bars: int = 0
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    source: str = "missing"             # "cache" | "downloaded" | "missing"
    data_class: str = "historical:openalgo"
    reason: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    # The bars themselves. ``None`` when ``ok=False``; a list of dicts
    # ordered oldest-first when ``ok=True``. We keep them as dicts (not a
    # pandas DataFrame) so this module does not require pandas at import
    # time; the engine / dashboard convert as needed.
    bars: Optional[List[Dict[str, Any]]] = None


@dataclass
class StatusResult:
    """The cache's view of one instrument."""
    cached: bool
    symbol: str
    exchange: str
    interval: str
    n_bars: int = 0
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    source: str = "missing"


# --------------------------------------------------------------------------- #
# The cache itself
# --------------------------------------------------------------------------- #


class OptimizerDataCache:
    """Download + cache + load bars for the optimizer engine.

    This class is the single boundary between the operator's data and the
    engine's ``bars`` argument. It is read-only against the broker and the
    live trading system; it writes only to a local SQLite file.

    Thread-safety: the underlying ``PriceCache`` opens a fresh sqlite3
    connection per call. Two concurrent downloads of the same instrument
    may write the same rows; ``upsert`` is idempotent so the second write
    is a no-op.
    """

    def __init__(self, root: Optional[Path] = None,
                 log: Optional[Any] = None) -> None:
        self.root = Path(root) if root else DEFAULT_CACHE_DIR
        self.db_path = self.root / "ohlcv.sqlite"
        self.contracts_path = self.root / "contracts_nse_cash.json"
        self._log = log
        self.root.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ---- logging helper ------------------------------------------------- #

    def _log_msg(self, msg: str) -> None:
        if callable(self._log):
            self._log(f"[optimizer_data_cache] {msg}")
        else:
            logger.info("[optimizer_data_cache] %s", msg)

    # ---- schema (mirrors PriceCache; kept local so the cache is self-contained)

    def _init_schema(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            self._log_msg(f"schema init failed: {exc!r}")

    # ---- public API ----------------------------------------------------- #

    def list_contracts(self, *, exchange: str = DEFAULT_EXCHANGE,
                       force_refresh: bool = False) -> List[Dict[str, str]]:
        """Return NSE cash contracts from OpenAlgo, cached for 24h.

        Returns a list of ``{symbol, name, exchange, lot_size, token}``
        dicts. On any failure, returns the previous cached list (or an
        empty list if the cache is empty). The cache file lives at
        ``<root>/contracts_nse_cash.json``.
        """
        # Read-through cache: serve the on-disk list if it's fresh enough.
        if not force_refresh and self.contracts_path.is_file():
            try:
                age = time.time() - self.contracts_path.stat().st_mtime
                if age < CONTRACT_LIST_TTL_S:
                    with self.contracts_path.open("r", encoding="utf-8") as fh:
                        cached = json.load(fh)
                    if isinstance(cached, list) and cached:
                        return cached
            except (OSError, json.JSONDecodeError):
                pass     # fall through to refresh

        # Refresh from OpenAlgo.
        try:
            from blue_engine.portfolio.providers.factory import get_client
            client = get_client()
        except Exception as exc:  # noqa: BLE001
            self._log_msg(f"factory.get_client failed: {exc!r}")
            return self._read_contracts_fallback()

        if client is None:
            return self._read_contracts_fallback()

        try:
            resp = client.instruments(exchange=exchange)
        except Exception as exc:  # noqa: BLE001
            self._log_msg(f"instruments call failed: {exc!r}")
            return self._read_contracts_fallback()

        rows = self._normalise_instruments(resp, exchange=exchange)
        if not rows:
            return self._read_contracts_fallback()

        try:
            with self.contracts_path.open("w", encoding="utf-8") as fh:
                json.dump(rows, fh, indent=2)
        except OSError as exc:
            self._log_msg(f"contracts cache write failed: {exc!r}")

        return rows

    def download(self, *, exchange: str, symbol: str, interval: str,
                 start: str, end: str,
                 api_key: Optional[str] = None) -> DownloadResult:
        """Download bars for ``(exchange, symbol, interval)`` over ``[start..end]``.

        Writes rows to the local SQLite cache via ``PriceCache.upsert``,
        which is idempotent. Honours the 30-day cap by pruning the window
        and appending a warning to the result. Never raises; returns a
        ``DownloadResult`` with ``ok=False`` on any failure.
        """
        symbol_u = (symbol or "").upper().strip()
        exchange_u = (exchange or DEFAULT_EXCHANGE).upper().strip()
        interval_u = (interval or "1d").strip()

        if not symbol_u or not exchange_u:
            return DownloadResult(ok=False, symbol=symbol_u,
                                   exchange=exchange_u, interval=interval_u,
                                   reason="missing symbol or exchange")

        # ---- Resolve the SDK client (env-var hand-off) ----------------- #
        try:
            from blue_engine.portfolio.providers.factory import get_client
            client = get_client()
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(ok=False, symbol=symbol_u,
                                   exchange=exchange_u, interval=interval_u,
                                   reason=f"factory.get_client failed: {exc!r}")

        if client is None:
            return DownloadResult(ok=False, symbol=symbol_u,
                                   exchange=exchange_u, interval=interval_u,
                                   reason="no_client",
                                   warnings=["no OPENALGO_API_KEY or platform down"])

        # ---- Resolve the window ---------------------------------------- #
        try:
            start_dt = datetime.strptime(start, "%Y-%m-%d")
            end_dt = datetime.strptime(end, "%Y-%m-%d")
        except ValueError:
            return DownloadResult(ok=False, symbol=symbol_u,
                                   exchange=exchange_u, interval=interval_u,
                                   reason="bad_date_format",
                                   warnings=["start/end must be YYYY-MM-DD"])

        if end_dt < start_dt:
            return DownloadResult(ok=False, symbol=symbol_u,
                                   exchange=exchange_u, interval=interval_u,
                                   reason="end_before_start")

        warnings: List[str] = []
        requested_days = (end_dt - start_dt).days
        if requested_days > MAX_WINDOW_DAYS:
            start_dt = end_dt - timedelta(days=MAX_WINDOW_DAYS)
            warnings.append(
                f"requested window {requested_days}d exceeds the "
                f"{MAX_WINDOW_DAYS}d hard prune; cache now covers the most "
                f"recent {MAX_WINDOW_DAYS} days")

        # ---- Call OpenAlgo's history endpoint --------------------------- #
        try:
            resp = client.history(
                symbol=symbol_u, exchange=exchange_u,
                interval=interval_u,
                start_date=start_dt.strftime("%Y-%m-%d"),
                end_date=end_dt.strftime("%Y-%m-%d"),
            )
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(ok=False, symbol=symbol_u,
                                   exchange=exchange_u, interval=interval_u,
                                   reason=f"history_call_failed: {exc!r}",
                                   warnings=warnings)

        if not isinstance(resp, dict) or resp.get("status") != "success":
            msg = (resp.get("message") if isinstance(resp, dict) else None) \
                  or "history returned non-success"
            return DownloadResult(ok=False, symbol=symbol_u,
                                   exchange=exchange_u, interval=interval_u,
                                   reason=f"history_error: {msg}",
                                   warnings=warnings)

        rows = resp.get("data") or []
        if not rows:
            return DownloadResult(ok=False, symbol=symbol_u,
                                   exchange=exchange_u, interval=interval_u,
                                   reason="empty_history",
                                   warnings=warnings)

        # ---- Persist to SQLite (idempotent) ---------------------------- #
        from blue_engine.portfolio.providers import price_cache as pc
        normalised = pc.normalise_rows(rows)
        n_existing = self._count_rows(symbol_u, exchange_u, interval_u)
        n_written = pc.PriceCache(db_path=self.db_path).upsert(
            symbol_u, exchange_u, interval_u, normalised)

        first = normalised[0]["ts"] if normalised else None
        last = normalised[-1]["ts"] if normalised else None
        return DownloadResult(
            ok=True, symbol=symbol_u, exchange=exchange_u, interval=interval_u,
            n_written=n_written, n_existing=n_existing,
            first_ts=first, last_ts=last, warnings=warnings)

    def load(self, *, exchange: str, symbol: str, interval: str,
             start: str, end: str) -> LoadResult:
        """Load bars for the requested window; download if missing.

        Prefers the local cache. If the cache is empty for the requested
        window, calls ``download(...)`` first. Returns a ``LoadResult``
        whose ``bars`` field is a list of dicts ordered oldest-first when
        ``ok=True``.
        """
        symbol_u = (symbol or "").upper().strip()
        exchange_u = (exchange or DEFAULT_EXCHANGE).upper().strip()
        interval_u = (interval or "1d").strip()
        warnings: List[str] = []

        # First, the in-cache count for the requested instrument.
        n_cached = self._count_rows(symbol_u, exchange_u, interval_u)
        if n_cached == 0:
            # Try a one-shot download.
            dl = self.download(exchange=exchange_u, symbol=symbol_u,
                                interval=interval_u, start=start, end=end)
            if not dl.ok:
                return LoadResult(ok=False, symbol=symbol_u,
                                   exchange=exchange_u, interval=interval_u,
                                   start=start, end=end, reason=dl.reason,
                                   warnings=dl.warnings)
            warnings.extend(dl.warnings)
            source = "downloaded"
        else:
            source = "cache"

        # Read the rows for the requested window.
        bars = self._read_window(symbol_u, exchange_u, interval_u,
                                  start, end)
        if not bars:
            return LoadResult(ok=False, symbol=symbol_u,
                               exchange=exchange_u, interval=interval_u,
                               start=start, end=end,
                               reason="no_rows_in_window",
                               warnings=warnings)

        return LoadResult(
            ok=True, symbol=symbol_u, exchange=exchange_u, interval=interval_u,
            start=start, end=end, n_bars=len(bars),
            first_ts=bars[0]["ts"], last_ts=bars[-1]["ts"],
            source=source, data_class="historical:openalgo",
            warnings=warnings, bars=bars)

    def status(self, *, exchange: str, symbol: str,
               interval: str) -> StatusResult:
        """Return a one-line health report for the instrument."""
        symbol_u = (symbol or "").upper().strip()
        exchange_u = (exchange or DEFAULT_EXCHANGE).upper().strip()
        interval_u = (interval or "1d").strip()
        n = self._count_rows(symbol_u, exchange_u, interval_u)
        if n == 0:
            return StatusResult(cached=False, symbol=symbol_u,
                                 exchange=exchange_u, interval=interval_u,
                                 source="missing")
        first, last = self._first_last(symbol_u, exchange_u, interval_u)
        return StatusResult(cached=True, symbol=symbol_u,
                             exchange=exchange_u, interval=interval_u,
                             n_bars=n, first_ts=first, last_ts=last,
                             source="cache")

    def cached_keys(self) -> List[Dict[str, str]]:
        """All (exchange, symbol, interval) tuples currently in the cache."""
        try:
            with sqlite3.connect(str(self.db_path), timeout=5.0) as conn:
                cur = conn.execute(
                    "SELECT DISTINCT exchange, symbol, interval "
                    "FROM ohlcv ORDER BY exchange, symbol, interval")
                return [{"exchange": r[0], "symbol": r[1], "interval": r[2]}
                        for r in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            self._log_msg(f"cached_keys failed: {exc!r}")
            return []

    # ---- internal helpers --------------------------------------------- #

    def _count_rows(self, symbol: str, exchange: str,
                    interval: str) -> int:
        try:
            with sqlite3.connect(str(self.db_path), timeout=5.0) as conn:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM ohlcv "
                    "WHERE symbol=? AND exchange=? AND interval=?",
                    (symbol, exchange, interval))
                return int(cur.fetchone()[0])
        except Exception:  # noqa: BLE001
            return 0

    def _first_last(self, symbol: str, exchange: str,
                    interval: str) -> Tuple[Optional[str], Optional[str]]:
        try:
            with sqlite3.connect(str(self.db_path), timeout=5.0) as conn:
                cur = conn.execute(
                    "SELECT MIN(ts), MAX(ts) FROM ohlcv "
                    "WHERE symbol=? AND exchange=? AND interval=?",
                    (symbol, exchange, interval))
                row = cur.fetchone()
                return (row[0], row[1]) if row else (None, None)
        except Exception:  # noqa: BLE001
            return (None, None)

    def _read_window(self, symbol: str, exchange: str, interval: str,
                     start: str, end: str) -> List[Dict[str, Any]]:
        """Read rows for ``[start..end]`` ordered oldest-first."""
        try:
            with sqlite3.connect(str(self.db_path), timeout=5.0) as conn:
                cur = conn.execute(
                    "SELECT ts, open, high, low, close, volume FROM ohlcv "
                    "WHERE symbol=? AND exchange=? AND interval=? "
                    "  AND ts>=? AND ts<=? "
                    "ORDER BY ts ASC",
                    (symbol, exchange, interval, start, end))
                out = []
                for r in cur.fetchall():
                    out.append({"ts": r[0], "open": r[1], "high": r[2],
                                "low": r[3], "close": r[4], "volume": r[5]})
                return out
        except Exception as exc:  # noqa: BLE001
            self._log_msg(f"_read_window failed: {exc!r}")
            return []

    def _normalise_instruments(self, resp: Any, *,
                                exchange: str) -> List[Dict[str, str]]:
        """Turn the OpenAlgo ``instruments`` payload into a clean list."""
        if isinstance(resp, dict):
            data = resp.get("data") or resp.get("result") or []
        else:
            data = resp
        if not isinstance(data, list):
            return []
        out: List[Dict[str, str]] = []
        for r in data:
            if not isinstance(r, dict):
                continue
            sym = (r.get("symbol") or r.get("tradingsymbol")
                   or r.get("tsym") or r.get("name"))
            if not sym:
                continue
            entry = {
                "symbol": str(sym).upper(),
                "name": str(r.get("name") or sym),
                "exchange": str(r.get("exchange") or exchange),
                "lot_size": str(r.get("lot_size") or r.get("lotsize") or 1),
                "token": str(r.get("token") or r.get("instrument_token") or ""),
            }
            out.append(entry)
        return out

    def _read_contracts_fallback(self) -> List[Dict[str, str]]:
        """Return the on-disk contract list even if it's stale."""
        if not self.contracts_path.is_file():
            return []
        try:
            with self.contracts_path.open("r", encoding="utf-8") as fh:
                cached = json.load(fh)
            return cached if isinstance(cached, list) else []
        except (OSError, json.JSONDecodeError):
            return []


# --------------------------------------------------------------------------- #
# Schema (mirrors PriceCache._SCHEMA; kept local so the module is self-contained
# and tests can use a tmpfile without depending on PriceCache's default path).
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv (
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    interval TEXT NOT NULL,
    ts TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    fetched_at REAL,
    PRIMARY KEY (symbol, exchange, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_lookup
    ON ohlcv (symbol, exchange, interval, ts);
"""


# --------------------------------------------------------------------------- #
# Module-level helpers used by the dashboard adapter (read-only)
# --------------------------------------------------------------------------- #


def watchlist_for_ui(*, preset: Optional[str] = None,
                      extra_symbols: Optional[Sequence[Dict[str, str]]] = None,
                      exchange: str = DEFAULT_EXCHANGE
                      ) -> List[Dict[str, str]]:
    """The operator's watchlist, filtered to ``exchange`` (NSE cash by default).

    Thin adapter over
    ``blue_engine.dashboard.data_source.resolve_watchlist``. Returns the
    list of ``{symbol, exchange, name, lot_size}`` dicts the dashboard
    dropdown renders. Exists in this module so the header has a single
    import point for both the bars cache and the watchlist.
    """
    try:
        from blue_engine.dashboard.data_source import resolve_watchlist
        return list(resolve_watchlist(preset=preset,
                                       extra_symbols=extra_symbols,
                                       exchange_filter=exchange) or [])
    except Exception as exc:  # noqa: BLE001
        logger.info("watchlist_for_ui failed: %r", exc)
        return []


def list_strategies_for_ui() -> List[Dict[str, str]]:
    """Every ``BaseSignal`` subclass registered in the alpha engine.

    The registry is the union of
    ``blue_engine.alpha.signals._REGISTRY`` (the auto-walked flat folder)
    and the ``blue_engine.alpha.Statergy`` folder (added by the Phase 2
    walker). Both feed into the same registry; ``available()`` returns
    the sorted union.

    Returns a list of ``{name, label, module, description, params_space}``
    dicts the dashboard dropdown + AI Insight header render. Read-only.
    """
    out: List[Dict[str, Any]] = []
    try:
        # Importing the package triggers both walkers (signals + Statergy).
        from blue_engine.alpha import signals as signals_pkg
        for name in signals_pkg.available():
            try:
                cls = signals_pkg.get(name)
            except KeyError:
                continue
            # `params_space` may be a class attribute (preferred) or
            # derived from the strategy's ``__init__`` docstring. We read
            # the class attribute when present; otherwise leave it empty
            # so the AI Insight header renders an honest "no params"
            # panel instead of inventing rows.
            params_space = getattr(cls, "params_space", None) or {}
            entry = {
                "name": name,
                "label": getattr(cls, "label", name),
                "module": getattr(cls, "__module__", "?"),
                "class_name": cls.__name__,
                "description": ((getattr(cls, "__doc__", "") or "")
                                .strip().splitlines()[0]
                                if getattr(cls, "__doc__", None) else ""),
                "params_space": params_space,
            }
            out.append(entry)
    except Exception as exc:  # noqa: BLE001
        logger.info("list_strategies_for_ui failed: %r", exc)
    return sorted(out, key=lambda d: d["name"])


def live_active_state(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Read the live trading system's current strategy from disk.

    The live runner writes its active state to
    ``Blue_engine/input/strategy.active.json`` on every config change.
    The Auto-Live mode of the optimizer header reads this file to know
    which symbol/strategy/interval to study. Returns ``None`` when the
    file is missing or unreadable; never raises.
    """
    from pathlib import Path as _P
    target = _P(path) if path else _P("Blue_engine/input/strategy.active.json")
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.info("live_active_state read failed: %r", exc)
        return {"error": str(exc)}
