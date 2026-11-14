"""Maintain an always-up-to-date 1-minute OHLCV sample store.

Unlike `sample_data.py` (which mirrors the daily-only upstream GitHub repo),
the upstream repository publishes NO intraday data. 1-minute bars therefore
come from the local OpenAlgo bridge (`/api/v1/history`, interval "1m"),
which serves real broker history. Credentials are read from .env and are
never printed, logged, or written to any output file.

The store is ACCUMULATING: every refresh downloads the newest missing
window per symbol, merges it with the existing Parquet store, de-duplicates
by timestamp, and republishes atomically (staged -> validated -> replaced).
A manifest.json records per-symbol coverage, row counts and SHA256s.

Data class: HISTORICAL (broker history, not live). Gitignored - the store
is local research data, not redistributable upstream content.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

LOG = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
OUTPUT_DIR = HERE / "sample_data" / "1min"
ROOT = HERE.parents[1]

# The 5 sample NIFTY-50 names, matching sample_data.STOCK_FILES.
SYMBOLS = ("INFY", "RELIANCE", "TCS", "ADANIENT", "M&M")
EXCHANGE = "NSE"
INTERVAL = "1m"
STAGE_DIR = OUTPUT_DIR.parent / ".staging-1min"
SCHEMA = pa.schema([
    ("datetime", pa.timestamp("us")),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.float64()),
])
LOOKBACK_DAYS = 7          # per refresh, how far back to re-request
MAX_WEEKDAYS_STALE = 5     # close + weekend tolerance, same as daily store
REQUEST_TIMEOUT = 60
RETRY_LIMIT = 3
MAX_WEEKS_PER_REQUEST = 4  # keep broker responses manageable

# OpenAlgo NSE symbol for M&M ("&" is unsafe in URLs/APIs).
SYMBOL_API_MAP = {"M&M": "M&M"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_env() -> Dict[str, str]:
    """Read OpenAlgo bridge credentials from the project .env files."""
    env: Dict[str, str] = {}
    for name in (ROOT / "openalgo" / ".env", ROOT / ".env"):
        if not name.is_file():
            continue
        for line in name.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _request_history(
    host: str, api_key: str, symbol: str, start: str, end: str,
) -> List[Dict[str, Any]]:
    """POST one /api/v1/history window and return the raw row dicts."""
    body = json.dumps({
        "apikey": api_key,
        "symbol": SYMBOL_API_MAP.get(symbol, symbol),
        "exchange": EXCHANGE,
        "interval": INTERVAL,
        "start_date": start,
        "end_date": end,
        "source": "api",
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{host}/api/v1/history", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    last_error: Optional[Exception] = None
    for attempt in range(RETRY_LIMIT):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8", "ignore"))
            if payload.get("status") != "success":
                raise RuntimeError(
                    f"{symbol} {start}..{end}: status={payload.get('status')} "
                    f"msg={payload.get('message')}")
            return list(payload.get("data") or [])
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                ValueError, KeyError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < RETRY_LIMIT:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"{symbol} {start}..{end}: {last_error}")


def _rows_to_table(rows: List[Mapping[str, Any]]) -> pa.Table:
    """Normalise raw broker rows into the canonical schema."""
    if not rows:
        return SCHEMA.empty_table()
    keys = set(rows[0].keys())
    ts_key = next((k for k in ("datetime", "timestamp", "date", "time")
                   if k in keys), None)
    if ts_key is None:
        raise RuntimeError(f"no timestamp column in source; columns={sorted(keys)}")
    datetimes, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    for row in rows:
        try:
            stamp = str(row[ts_key]).replace("T", " ")
            stamp = stamp[:19]  # drop tz/millis; broker history is IST-local
            datetimes.append(datetime.fromisoformat(stamp))
            o, h, l, c = (float(row["open"]), float(row["high"]),
                          float(row["low"]), float(row["close"]))
            if h < l or min(o, h, l, c) <= 0:
                continue  # drop malformed bars rather than fail the refresh
            datetimes[-1] = datetimes[-1]  # keep alignment
            opens.append(o); highs.append(h); lows.append(l); closes.append(c)
            volumes.append(float(row.get("volume") or 0))
        except (KeyError, TypeError, ValueError):
            continue
    return pa.table({
        "datetime": pa.array(datetimes, pa.timestamp("us")),
        "open": pa.array(opens, pa.float64()),
        "high": pa.array(highs, pa.float64()),
        "low": pa.array(lows, pa.float64()),
        "close": pa.array(closes, pa.float64()),
        "volume": pa.array(volumes, pa.float64()),
    })


def _merge(existing: Optional[pa.Table], fresh: pa.Table) -> pa.Table:
    """Concatenate, de-duplicate by timestamp, sort ascending."""
    tables = [t for t in (existing, fresh) if t is not None and t.num_rows]
    if not tables:
        return SCHEMA.empty_table()
    combined = pa.concat_tables(tables)
    combined = combined.take(
        pc.sort_indices(combined, sort_keys=[("datetime", "ascending")]))
    unique_mask = ~pc.equal(
        combined.column("datetime"), pc.shift(combined.column("datetime"), 1))
    # first row has no predecessor -> always keep
    first = pa.array([True] + [False] * (combined.num_rows - 1), pa.bool_())
    mask = pc.or_else(unique_mask, first)
    return combined.filter(pc.fill_null(mask, True))


def validate_store(path: Path, *, today: date) -> Dict[str, Any]:
    """Validate a merged 1-minute store: schema, freshness, sanity."""
    if not path.is_file() or path.read_bytes()[:4] != b"PAR1":
        raise ValueError(f"{path}: not a Parquet file")
    table = pq.read_table(path)
    if table.column_names != SCHEMA.names:
        raise ValueError(f"{path}: unexpected columns {table.column_names}")
    if table.num_rows == 0:
        raise ValueError(f"{path}: store is empty")
    stamps = table.column("datetime").cast(pa.timestamp("s")).to_pylist()
    latest = max(s.date() for s in stamps if s is not None)
    if latest > today:
        raise ValueError(f"{path}: latest date {latest} is in the future")
    stale = (today - latest).days
    return {
        "records": table.num_rows,
        "latest_bar": max(s.isoformat(sep=" ") for s in stamps if s is not None),
        "latest_date": latest.isoformat(),
        "calendar_days_stale": stale,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def refresh_1min_sample_data(
    output_dir: Path = OUTPUT_DIR,
    *,
    symbols: tuple[str, ...] = SYMBOLS,
    today: Optional[date] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Extend every symbol's 1-minute store with the newest missing bars.

    Per symbol: read the existing store, fetch the missing window(s) from
    the bridge, merge/de-duplicate, then atomically replace the Parquet file.
    A failed symbol leaves its prior store intact and is reported, not fatal.
    """
    env = dict(env if env is not None else _load_env())
    host = (env.get("HOST_SERVER") or "http://127.0.0.1:5000").rstrip("/")
    api_key = env.get("OPENALGO_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENALGO_API_KEY not found in .env; cannot query bridge")

    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_date = today or datetime.now(timezone.utc).date()
    file_manifest: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    for symbol in symbols:
        destination = output_dir / f"{symbol}_NS_1min.parquet"
        existing = None
        if destination.is_file():
            existing = pq.read_table(destination)
        # Fetch from the latest store edge (or LOOKBACK_DAYS back) to today.
        tail_days = LOOKBACK_DAYS
        if existing is not None and existing.num_rows:
            stamps = existing.column("datetime").cast(pa.timestamp("s")).to_pylist()
            last = max(s for s in stamps if s is not None)
            tail_days = min(max((run_date - last.date()).days + 2, 1), 60)
        start_day = run_date - timedelta(days=tail_days)

        fresh: pa.Table = SCHEMA.empty_table()
        try:
            # Fetch in <=MAX_WEEKS_PER_REQUEST windows so large responses
            # stay manageable on the bridge side.
            window_start = start_day
            while window_start <= run_date:
                window_end = min(window_start + timedelta(days=MAX_WEEKS_PER_REQUEST),
                                 run_date)
                rows = _request_history(
                    host, api_key, symbol,
                    window_start.isoformat(), window_end.isoformat())
                fresh = _merge(fresh, _rows_to_table(rows))
                LOG.info("%s %s..%s: %d bars", symbol, window_start,
                         window_end, len(rows))
                window_start = window_end + timedelta(days=1)
        except Exception as exc:  # noqa: BLE001 - report per symbol
            errors[symbol] = str(exc)
            LOG.error("%s: refresh failed: %s", symbol, exc)
            if existing is None:
                continue  # nothing to publish for this symbol

        merged = _merge(existing, fresh)
        # Publish atomically through the staging dir.
        STAGE_DIR.mkdir(parents=True, exist_ok=True)
        staged = STAGE_DIR / destination.name
        pq.write_table(merged, staged, compression="snappy")
        details = validate_store(staged, today=run_date)
        os.replace(staged, destination)
        file_manifest[f"{symbol}_NS_1min.parquet"] = details
        LOG.info("%s: %s bars through %s", symbol,
                 details["records"], details["latest_bar"])

    manifest: Dict[str, Any] = {
        "data_class": "historical",
        "interval": INTERVAL,
        "source": "openalgo-bridge",
        "bridge_host": host,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z"),
        "symbols": list(symbols),
        "exchange": EXCHANGE,
        "errors": errors,
        "files": file_manifest,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh the accumulating 1-minute NSE sample store "
                    "from the OpenAlgo bridge.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--symbols", nargs="*", default=list(SYMBOLS))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        manifest = refresh_1min_sample_data(
            args.output_dir, symbols=tuple(args.symbols))
    except Exception as exc:  # noqa: BLE001
        LOG.error("1-minute sample refresh failed: %s", exc)
        return 1
    print(json.dumps({
        "updated_at_utc": manifest["updated_at_utc"],
        "errors": manifest["errors"],
        "files": {
            name: {"records": meta["records"], "latest_bar": meta["latest_bar"]}
            for name, meta in manifest["files"].items()
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
