"""database/engine.py - the Data Management Engine (DME) for Blue_engine.

WHAT THIS IS
============
The DME is the foundational data layer for the multi-engine trading system. It
sits between raw sources (market APIs, local SQLite/PostgreSQL stores, PDF
filings) and the consumers (math indicators, ML layers, agentic frontline
agents), and guarantees four properties:

1. ZERO HALT INTEGRITY. Every public method returns a status-bearing dict with
   ``ok`` and human-readable ``reason``/``warnings``. A broker timeout, a
   corrupted PDF page, a locked SQLite file or a missing optional package is
   logged and reported, never raised into a trading cycle.

2. TRANSACTIONAL TIMESTAMPS. Every data object gets a verified ISO-8601 UTC
   sync stamp (``YYYY-MM-DDTHH:MM:SSZ``) so downstream math never combines
   mismatched time intervals unknowingly. Source timestamps (IST by default)
   are converted to UTC on ingest.

3. NO LOOK-AHEAD. ``align()`` joins text/sentiment features to prices with a
   backward as-of merge only: a sentiment row published at time T is visible to
   prices at T and later, never to prices before T.

4. REUSE, DO NOT DUPLICATE. OHLCV storage is the existing
   ``portfolio.providers.price_cache.PriceCache`` (same SQLite file the rest of
   the system reads). This engine does not invent a second price store.

Storage routing
---------------
    data/price_cache/prices.db          OHLCV bars (existing PriceCache)
    data/current_cycle_payload.json     the Tier-4 cycle payload, atomic writes

Runtime safety
--------------
* SQLite reads open with ``mode=ro`` + ``PRAGMA query_only=ON`` and a short
  timeout so a locked writer database cannot stall a trading cycle.
* PostgreSQL support is optional (``psycopg`` imported only when a postgres DSN
  is requested) inside a ``READ ONLY`` transaction.
* The lag fallback is an INJECTED callable, never a scraper pointed at an
  arbitrary website. The caller supplies a trusted source with its own timeout
  and rate-limit policy; unverified scraping is deliberately not attempted.
* PDF extraction is keyword-scoped and byte-capped (``max_pdf_chars``) so a
  300-page annual report cannot exhaust memory.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import tempfile
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from zoneinfo import ZoneInfo

from blue_engine.portfolio.providers.price_cache import PriceCache, normalise_rows

logger = logging.getLogger(__name__)

__all__ = ["DatabaseEngine", "utc_now_iso", "normalize_timestamp"]

_UTC = timezone.utc
_SQL_START = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_PAYLOAD_LOCK = threading.RLock()


def utc_now_iso() -> str:
    """Verified UTC sync stamp: second precision, ``Z`` suffix."""
    return datetime.now(_UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_timestamp(value: Any, source_timezone: str = "Asia/Kolkata"
                        ) -> Optional[str]:
    """Normalize date/datetime/ISO/epoch input to an ISO-8601 UTC ``Z`` string.

    Accepts ``datetime``/``date``, ISO strings (with or without ``Z``), and epoch
    seconds/milliseconds/microseconds/nanoseconds (disambiguated by magnitude,
    same rule as the existing price cache). Naive datetimes are interpreted in
    ``source_timezone`` (IST for NSE bars by default) and converted to UTC.
    Returns ``None`` for anything uninterpretable - never raises.
    """
    try:
        if isinstance(value, datetime):
            stamp = value
        elif isinstance(value, date):
            stamp = datetime(value.year, value.month, value.day)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if not math.isfinite(number):
                return None
            while abs(number) > 1e11:      # ms -> s, us -> s, ns -> s
                number /= 1000.0
            stamp = datetime.fromtimestamp(number, tz=_UTC)
        else:
            raw = str(value).strip()
            if not raw:
                return None
            if raw.replace(".", "", 1).isdigit():
                return normalize_timestamp(float(raw), source_timezone)
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo(source_timezone))
        return stamp.astimezone(_UTC).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError, KeyError):
        return None


def _json_safe(value: Any) -> Any:
    """Recursively coerce a payload to JSON-safe values. NaN/inf become None."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return normalize_timestamp(value) or str(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):          # numpy scalars
        try:
            return _json_safe(value.item())
        except Exception:  # noqa: BLE001
            pass
    return str(value)


class DatabaseEngine:
    """Orchestrates ingestion, freshness diagnostics, alignment and caching.

    Args:
        client: an injected market-data client exposing keyword-only
            ``history(...)`` and ``quotes(...)`` (the OpenAlgo SDK contract).
        price_cache: the OHLCV store to persist into. Defaults to the shared
            ``PriceCache``; tests inject a temp-path instance.
        payload_path: where ``current_cycle_payload.json`` is published.
        lag_fallback: an optional trusted patch source with the contract
            ``fallback(symbol=..., exchange=..., interval=..., after=...,
            before=...) -> rows``. Never auto-scraped.
        source_timezone: timezone of naive source timestamps (IST default).
        sqlite_timeout_seconds: bounded wait for locked SQLite reads/writes.
        max_pdf_chars: hard cap on extracted PDF context characters.
    """

    def __init__(self, *, client: Any = None,
                 price_cache: Optional[PriceCache] = None,
                 payload_path: Optional[Path] = None,
                 lag_fallback: Any = None,
                 source_timezone: str = "Asia/Kolkata",
                 sqlite_timeout_seconds: float = 0.25,
                 max_pdf_chars: int = 200_000) -> None:
        self.client = client
        self.price_cache = price_cache or PriceCache()
        self.payload_path = (Path(payload_path) if payload_path else
                             Path(__file__).resolve().parents[2] / "data"
                             / "current_cycle_payload.json")
        self.lag_fallback = lag_fallback
        self.source_timezone = source_timezone
        self.sqlite_timeout_seconds = max(0.01, float(sqlite_timeout_seconds))
        self.max_pdf_chars = max(1024, int(max_pdf_chars))
        self._payload: Dict[str, Any] = {
            "synced_at": utc_now_iso(),
            "data_class": "historical_or_derived",
            "market": {},
            "text_context": {},
            "aligned": [],
            "diagnostics": [],
        }

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _result(ok: bool, **fields: Any) -> Dict[str, Any]:
        return {"ok": bool(ok), "synced_at": utc_now_iso(), **fields}

    def _normalize_market_rows(self, raw_rows: Iterable[Any]) -> List[Dict[str, Any]]:
        """SDK rows -> validated UTC-stamped rows, oldest first. Never raises."""
        normalized = normalise_rows(list(raw_rows or []))
        sync_stamp = utc_now_iso()
        out: List[Dict[str, Any]] = []
        for row in normalized:
            ts = normalize_timestamp(row.get("ts"), self.source_timezone)
            close = row.get("close")
            if ts is None or close is None:
                continue
            out.append({**row, "ts": ts, "synced_at": sync_stamp})
        out.sort(key=lambda r: r["ts"])
        return out

    def _record_diagnostic(self, kind: str, component: str, message: str,
                           **context: Any) -> None:
        self._payload.setdefault("diagnostics", []).append({
            "type": kind, "component": component, "message": str(message),
            "timestamp": utc_now_iso(), **context})

    # ------------------------------------------------------------------ #
    # Tier 1a - market ingestion (index/equity/VIX history + snapshots)
    # ------------------------------------------------------------------ #

    def ingest_history(self, *, symbol: str, exchange: str = "NSE",
                       interval: str = "1d", start: Optional[str] = None,
                       end: Optional[str] = None,
                       fallback_on_lag: bool = False,
                       max_age_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Fetch history via the injected client, persist it, diagnose lag.

        Never raises. On lag (when ``max_age_seconds`` is given) and
        ``fallback_on_lag=True``, the injected trusted fallback patches the
        delta; its output is normalized and re-checked like any other source.
        """
        warnings: List[str] = []
        try:
            if self.client is None:
                raise RuntimeError("no market-data client configured")
            response = self.client.history(
                symbol=symbol, exchange=exchange, interval=interval,
                start_date=start, end_date=end)
            if isinstance(response, dict):
                if response.get("status") not in (None, "success"):
                    raise RuntimeError(
                        str(response.get("message", "history request failed")))
                raw_rows = response.get("data", [])
            elif hasattr(response, "to_dict") and hasattr(response, "index"):
                frame = response.reset_index()
                raw_rows = frame.to_dict("records")
            else:
                raw_rows = response or []
            rows = self._normalize_market_rows(raw_rows)
            if not rows:
                raise ValueError("history returned no valid timestamped close rows")
            written = self.price_cache.upsert(symbol, exchange, interval, rows)
            freshness = self.diagnose_freshness(
                rows, max_age_seconds=max_age_seconds)
            if not freshness["fresh"]:
                warnings.append(str(freshness["reason"]))
                if fallback_on_lag:
                    rows, fallback_warning = self._apply_fallback(
                        rows, symbol=symbol, exchange=exchange, interval=interval)
                    if fallback_warning:
                        warnings.append(fallback_warning)
                    if rows:
                        written += self.price_cache.upsert(
                            symbol, exchange, interval, rows)
                        freshness = self.diagnose_freshness(
                            rows, max_age_seconds=max_age_seconds)
                        if not freshness["fresh"]:
                            warnings.append(str(freshness["reason"]))
            self._payload.setdefault("market", {})[symbol] = rows
            self._payload["synced_at"] = utc_now_iso()
            return self._result(True, symbol=symbol, exchange=exchange,
                                interval=interval, rows=rows, written=written,
                                freshness=freshness, warnings=warnings,
                                source="injected_market_client")
        except Exception as exc:  # noqa: BLE001 - trading cycle must not halt
            logger.warning("market history ingestion failed for %s: %s", symbol, exc)
            self._record_diagnostic("INGESTION_ERROR", "market_history",
                                    f"{type(exc).__name__}: {exc}", symbol=symbol)
            return self._result(False, symbol=symbol, exchange=exchange,
                                interval=interval, rows=[], written=0,
                                warnings=warnings,
                                reason=f"{type(exc).__name__}: {exc}")

    def ingest_local_history(self, *, symbol: str, exchange: str,
                             interval: str, rows: Sequence[Any],
                             source: str = "local_vendor",
                             data_class: str = "historical_vendor") -> Dict[str, Any]:
        """Persist trusted local history into the shared OHLCV cache.

        This is the local-source counterpart to :meth:`ingest_history`: it
        performs no network I/O, preserves daily index dates as session dates
        (does not reinterpret date-only vendor rows as midnight IST), and writes
        through the existing ``PriceCache`` rather than creating a second DB.
        The source must use a distinct cache symbol/exchange namespace if its
        values must not overwrite broker history.

        Returns the standard status-bearing dict and never raises.
        """
        try:
            if not symbol or not exchange or not interval:
                raise ValueError("symbol, exchange and interval are required")
            normalized = normalise_rows(list(rows or []))
            clean = []
            for row in normalized:
                stamp = str(row.get("ts") or "")
                # The local daily vendor path intentionally stores YYYY-MM-DD.
                # Intraday timestamps are kept as the cache normalizer supplied.
                if not stamp:
                    continue
                close = row.get("close")
                if close is None or not math.isfinite(float(close)) or float(close) <= 0:
                    continue
                clean.append(row)
            clean.sort(key=lambda row: row["ts"])
            if not clean:
                raise ValueError("local history contains no valid close rows")
            written = self.price_cache.upsert(symbol, exchange, interval, clean)
            if written <= 0:
                raise OSError("shared OHLCV cache wrote no rows")
            self._payload.setdefault("market", {})[symbol] = clean
            self._payload["synced_at"] = utc_now_iso()
            return self._result(True, symbol=symbol, exchange=exchange,
                                 interval=interval, rows=clean, written=written,
                                 source=source, data_class=data_class,
                                 freshness={"fresh": None,
                                            "latest_ts": clean[-1]["ts"],
                                            "reason": "local historical source; "
                                                     "freshness is reported by caller"},
                                 warnings=[])
        except Exception as exc:  # noqa: BLE001 - DME never halts a cycle
            logger.warning("local history ingestion failed for %s: %s", symbol, exc)
            self._record_diagnostic(
                "INGESTION_ERROR", "local_history",
                f"{type(exc).__name__}: {exc}", symbol=symbol, source=source)
            return self._result(False, symbol=symbol, exchange=exchange,
                                interval=interval, rows=[], written=0,
                                source=source, data_class=data_class,
                                warnings=[f"{type(exc).__name__}: {exc}"])

    def _apply_fallback(self, rows: List[Dict[str, Any]], *, symbol: str,

                        exchange: str,
                        interval: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Patch rows after the latest cached bar using the injected fallback."""
        if not callable(self.lag_fallback):
            return rows, "lag fallback unavailable; no unverified web scraping attempted"
        try:
            after = rows[-1]["ts"] if rows else None
            raw = self.lag_fallback(symbol=symbol, exchange=exchange,
                                    interval=interval, after=after,
                                    before=utc_now_iso())
            patch = self._normalize_market_rows(raw or [])
            combined = {r["ts"]: r for r in rows}
            combined.update({r["ts"]: r for r in patch})
            return [combined[k] for k in sorted(combined)], None
        except Exception as exc:  # noqa: BLE001
            logger.warning("trusted lag fallback failed for %s: %s", symbol, exc)
            return rows, f"trusted fallback failed: {type(exc).__name__}: {exc}"

    def fetch_quotes(self, instruments: Sequence[Tuple[str, str]]) -> Dict[str, Any]:
        """Snapshot quotes (index/equity/VIX) through the same injected adapter."""
        snapshots: Dict[str, Any] = {}
        warnings: List[str] = []
        for symbol, exchange in instruments or ():
            try:
                if self.client is None:
                    raise RuntimeError("no market-data client configured")
                response = self.client.quotes(symbol=symbol, exchange=exchange)
                if not isinstance(response, dict) or response.get("status") != "success":
                    raise ValueError("quote response was not successful")
                data = dict(response.get("data") or {})
                data["synced_at"] = utc_now_iso()
                data["data_class"] = "live_snapshot_from_injected_client"
                snapshots[f"{exchange}:{symbol}"] = data
            except Exception as exc:  # noqa: BLE001
                warning = f"{exchange}:{symbol}: {type(exc).__name__}: {exc}"
                warnings.append(warning)
                logger.warning("quote ingest failed: %s", warning)
        self._payload.setdefault("market", {}).update(snapshots)
        self._payload["synced_at"] = utc_now_iso()
        return self._result(not warnings, snapshots=snapshots, warnings=warnings)

    # ------------------------------------------------------------------ #
    # Tier 2 - freshness diagnostics
    # ------------------------------------------------------------------ #

    def diagnose_freshness(self, rows: Sequence[Mapping[str, Any]], *,
                           max_age_seconds: Optional[float] = None,
                           now: Optional[datetime] = None) -> Dict[str, Any]:
        """Flag data lagging: latest event timestamp vs a caller-chosen SLA.

        With ``max_age_seconds=None`` the check reports the latest stamp without
        judging freshness (the caller decides). With an SLA, ``fresh`` is False
        when no valid timestamp exists or the data is older than the SLA.
        """
        valid = [ts for row in (rows or ())
                 if isinstance(row, Mapping)
                 for ts in (normalize_timestamp(row.get("ts"), self.source_timezone),)
                 if ts is not None]
        latest = max(valid) if valid else None
        if max_age_seconds is None:
            return {"fresh": bool(latest), "latest_ts": latest,
                    "age_seconds": None, "sla_seconds": None,
                    "reason": None if latest else "dataset has no valid timestamps"}
        try:
            sla = float(max_age_seconds)
            if not math.isfinite(sla) or sla < 0:
                raise ValueError("max_age_seconds must be finite and non-negative")
            current = now or datetime.now(_UTC)
            if current.tzinfo is None:
                current = current.replace(tzinfo=_UTC)
            if latest is None:
                age = None
                fresh = False
                reason = "dataset has no valid timestamps"
            else:
                latest_dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
                age = max(0.0, (current.astimezone(_UTC) - latest_dt).total_seconds())
                fresh = age <= sla
                reason = None if fresh else (
                    f"latest data is {age:.1f}s old; SLA is {sla:.1f}s")
            return {"fresh": fresh, "latest_ts": latest, "age_seconds": age,
                    "sla_seconds": sla, "reason": reason}
        except Exception as exc:  # noqa: BLE001
            return {"fresh": False, "latest_ts": latest, "age_seconds": None,
                    "sla_seconds": max_age_seconds,
                    "reason": f"freshness diagnostic failed: {exc}"}

    # ------------------------------------------------------------------ #
    # Tier 1b - local document parsing (keyword-scoped PDF context)
    # ------------------------------------------------------------------ #

    def extract_pdf_context(self, pdf_path: Path, *, keywords: Sequence[str],
                            context_chars: int = 600,
                            max_pages: Optional[int] = None) -> Dict[str, Any]:
        """Extract keyword-centred snippets from a PDF, chunked and capped.

        ``pypdf`` is optional and imported lazily. A corrupted page is isolated
        and skipped; a missing package or corrupt file returns ``ok=False`` with
        the reason instead of raising. Total extracted characters are capped at
        ``max_pdf_chars`` so a 300-page filing cannot exhaust memory.
        """
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            return self._result(False, snippets=[], chars_extracted=0,
                                warnings=[f"pypdf not installed: {exc}"])
        try:
            reader = PdfReader(str(pdf_path), strict=False)
            terms = [str(k).casefold() for k in keywords if str(k).strip()]
            pages = list(reader.pages)
            if max_pages is not None:
                pages = pages[:max(0, int(max_pages))]
            snippets: List[Dict[str, Any]] = []
            total_chars = 0
            warnings_note: Optional[str] = None
            for page_no, page in enumerate(pages, start=1):
                try:
                    text = page.extract_text() or ""
                except Exception as exc:  # noqa: BLE001 - isolate a bad page
                    logger.warning("PDF page %d extraction failed: %s", page_no, exc)
                    continue
                lower = text.casefold()
                for term in terms:
                    if not term:
                        continue
                    pos = lower.find(term)
                    while pos >= 0 and total_chars < self.max_pdf_chars:
                        start = max(0, pos - context_chars // 2)
                        end = min(len(text), pos + len(term) + context_chars // 2)
                        snippet = text[start:end].strip()
                        if snippet:
                            snippets.append({"page": page_no, "keyword": term,
                                             "text": snippet,
                                             "synced_at": utc_now_iso()})
                            total_chars += len(snippet)
                        pos = lower.find(term, pos + len(term))
                    if total_chars >= self.max_pdf_chars:
                        break
                if total_chars >= self.max_pdf_chars:
                    warnings_note = "extraction cap reached; remaining pages skipped"
                    break
            self._payload.setdefault("text_context", {})[str(pdf_path)] = snippets
            return self._result(True, snippets=snippets,
                                pages_read=len(pages),
                                pages_total=len(reader.pages),
                                chars_extracted=total_chars,
                                warnings=[warnings_note] if warnings_note else [])
        except Exception as exc:  # noqa: BLE001 - corrupt PDF is non-fatal
            logger.warning("PDF context extraction failed for %s: %s", pdf_path, exc)
            self._record_diagnostic("PARSE_ERROR", "pdf_context",
                                    f"{type(exc).__name__}: {exc}",
                                    path=str(pdf_path))
            return self._result(False, snippets=[], chars_extracted=0,
                                warnings=[f"{type(exc).__name__}: {exc}"])

    # ------------------------------------------------------------------ #
    # Tier 1c - read-only relational queries (SQLite / optional PostgreSQL)
    # ------------------------------------------------------------------ #

    def query_readonly(self, dsn: str, sql: str, params: Sequence[Any] = (), *,
                       max_rows: int = 5000) -> Dict[str, Any]:
        """Execute a single read-only SELECT/WITH, row-capped. Never raises.

        SQLite opens the file with ``mode=ro`` and ``PRAGMA query_only=ON``;
        PostgreSQL runs inside a ``READ ONLY`` transaction with a bounded
        connect timeout. Multiple statements and non-SELECT text are rejected.
        """
        try:
            if not isinstance(sql, str) or not _SQL_START.match(sql):
                raise ValueError("only SELECT/WITH queries are allowed")
            if ";" in sql.rstrip().rstrip(";"):
                raise ValueError("multiple SQL statements are not allowed")
            limit = int(max_rows)
            if not 1 <= limit <= 100_000:
                raise ValueError("max_rows must be between 1 and 100000")
            if dsn.startswith("sqlite:"):
                db_path = dsn[len("sqlite:"):]
                uri = Path(db_path).resolve().as_uri() + "?mode=ro"
                conn = sqlite3.connect(uri, uri=True,
                                       timeout=self.sqlite_timeout_seconds)
                try:
                    conn.execute("PRAGMA query_only=ON")
                    cur = conn.execute(sql, tuple(params))
                    columns = [d[0] for d in (cur.description or [])]
                    rows = [dict(zip(columns, row))
                            for row in cur.fetchmany(limit)]
                    return self._result(True, rows=_json_safe(rows),
                                        row_count=len(rows),
                                        truncated=len(rows) == limit,
                                        backend="sqlite")
                finally:
                    conn.close()
            if dsn.startswith(("postgresql://", "postgres://")):
                import psycopg  # optional; imported only when requested
                timeout = max(1, int(self.sqlite_timeout_seconds))
                with psycopg.connect(dsn, connect_timeout=timeout) as conn:
                    with conn.transaction():
                        conn.execute("SET TRANSACTION READ ONLY")
                        cur = conn.execute(sql, tuple(params))
                        columns = [d.name for d in (cur.description or [])]
                        rows = [dict(zip(columns, row))
                                for row in cur.fetchmany(limit)]
                return self._result(True, rows=_json_safe(rows),
                                    row_count=len(rows),
                                    truncated=len(rows) == limit,
                                    backend="postgresql")
            raise ValueError("dsn must begin with sqlite: or postgresql://")
        except Exception as exc:  # noqa: BLE001
            logger.warning("read-only database query failed: %s", exc)
            return self._result(False, rows=[], row_count=0, truncated=False,
                                reason=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    # Tier 3 - chronological matrix synchronization (no look-ahead)
    # ------------------------------------------------------------------ #

    def align(self, prices: Mapping[str, Sequence[Mapping[str, Any]]],
              text_metrics: Optional[Mapping[str, Sequence[Mapping[str, Any]]]] = None
              ) -> Dict[str, Any]:
        """Merge multi-symbol price tables with text features, backward as-of.

        Uses pandas when installed (optional dependency, imported lazily).
        Timestamps are normalized to UTC first, so a 09:15 IST bar and a UTC
        sentiment stamp align on the same time axis. ``merge_asof(direction=
        'backward')`` guarantees a text row is joined only to prices at or after
        its publication - future information never leaks into past bars.
        """
        try:
            import pandas as pd
        except ImportError as exc:
            return self._result(False, rows=[],
                                warnings=[f"pandas not installed: {exc}"])
        try:
            price_rows: List[Dict[str, Any]] = []
            for symbol, rows in (prices or {}).items():
                for row in rows or ():
                    if not isinstance(row, Mapping):
                        continue
                    ts = normalize_timestamp(row.get("ts"), self.source_timezone)
                    if ts is None:
                        continue
                    price_rows.append({**dict(row), "ts": ts, "symbol": symbol})
            if not price_rows:
                return self._result(False, rows=[],
                                    warnings=["no valid price rows to align"])
            price_frame = pd.DataFrame(price_rows)
            price_frame["_dt"] = pd.to_datetime(price_frame["ts"], utc=True)
            price_frame = price_frame.sort_values(["_dt", "symbol"])
            if text_metrics:
                text_rows: List[Dict[str, Any]] = []
                for key, rows in text_metrics.items():
                    for row in rows or ():
                        if not isinstance(row, Mapping):
                            continue
                        ts = normalize_timestamp(
                            row.get("ts", row.get("published_at")),
                            self.source_timezone)
                        if ts is None:
                            continue
                        text_rows.append({**dict(row), "ts": ts,
                                          "text_key": str(key)})
                if text_rows:
                    text_frame = pd.DataFrame(text_rows)
                    text_frame["_dt"] = pd.to_datetime(text_frame["ts"], utc=True)
                    text_frame = text_frame.sort_values("_dt")
                    merged = pd.merge_asof(
                        price_frame, text_frame, on="_dt",
                        direction="backward", allow_exact_matches=True,
                        suffixes=("", "_text"))
                    price_frame = merged
            price_frame["synced_at"] = utc_now_iso()
            aligned = (price_frame.drop(columns=["_dt"])
                       .replace({float("nan"): None}).to_dict("records"))
            result = self._result(True, rows=_json_safe(aligned),
                                  row_count=len(aligned), backend="pandas",
                                  warnings=[])
            self._payload["aligned"] = result["rows"]
            self._payload["synced_at"] = utc_now_iso()
            return result
        except Exception as exc:  # noqa: BLE001 - bad input must not halt
            logger.warning("data alignment failed: %s", exc)
            return self._result(False, rows=[],
                                warnings=[f"{type(exc).__name__}: {exc}"])

    # ------------------------------------------------------------------ #
    # Tier 4 - memory interface caching (current_cycle_payload.json)
    # ------------------------------------------------------------------ #

    def write_cycle_payload(self, *,
                            data_class: str = "historical_or_derived"
                            ) -> Dict[str, Any]:
        """Atomically publish the cycle payload for all frontline agents.

        Writers build a temp file in the same directory, fsync it, and
        ``os.replace`` it over the target - readers never observe a partial
        file, and concurrent readers need no lock (atomic replacement is the
        synchronization primitive).
        """
        payload = _json_safe({**self._payload, "synced_at": utc_now_iso(),
                              "data_class": data_class})
        try:
            encoded = json.dumps(payload, ensure_ascii=False,
                                 separators=(", ", ":"), allow_nan=False)
            self.payload_path.parent.mkdir(parents=True, exist_ok=True)
            with _PAYLOAD_LOCK:
                fd, temp_name = tempfile.mkstemp(
                    prefix=f".{self.payload_path.name}.", suffix=".tmp",
                    dir=str(self.payload_path.parent))
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                        stream.write(encoded)
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp_name, self.payload_path)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
            return self._result(True, path=str(self.payload_path),
                                payload=payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cycle payload write failed: %s", exc)
            self._record_diagnostic("CACHE_WRITE_ERROR", "cycle_payload",
                                    f"{type(exc).__name__}: {exc}")
            return self._result(False, path=str(self.payload_path),
                                payload=payload,
                                warnings=[f"{type(exc).__name__}: {exc}"])

    def read_cycle_payload(self) -> Dict[str, Any]:
        """Read the last atomically published payload. Never blocks or raises."""
        try:
            with self.payload_path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
            if not isinstance(payload, dict) or not normalize_timestamp(
                    payload.get("synced_at")):
                raise ValueError("payload shape/timestamp invalid")
            return self._result(True, payload=payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cycle payload read failed: %s", exc)
            return self._result(False, payload=None,
                                warnings=[f"{type(exc).__name__}: {exc}"])
