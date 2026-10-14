"""Download and validate a bounded Indian index/equity historical-data sample.

The generated Parquet files are local research data, intentionally excluded
from Git. The source repository's license restricts use to personal,
non-commercial, educational research absent written permission.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import requests

SOURCE_REPOSITORY = "aswinv90/indian-market-historical-data"
SOURCE_API = f"https://api.github.com/repos/{SOURCE_REPOSITORY}"
RAW_BASE = f"https://raw.githubusercontent.com/{SOURCE_REPOSITORY}"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "sample_data"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_STALE_WEEKDAYS = 5
REQUEST_TIMEOUT = (15, 90)
RETRY_LIMIT = 3
LOG = logging.getLogger(__name__)

INDEX_FILES = (
    "INDIA_VIX", "NIFTY_50", "NIFTY_100", "NIFTY_200", "NIFTY_500",
    "NIFTY_BANK", "NIFTY_IT", "NIFTY_MIDCAP_50", "NIFTY_PHARMA", "SENSEX",
)
STOCK_FILES = ("INFY", "RELIANCE", "TCS", "ADANIENT", "M&M")
SAMPLE_FILES = {
    **{f"indices/{name}.parquet": f"data/INDICES/{name}.parquet"
       for name in INDEX_FILES},
    **{f"stocks/{symbol}_NS.parquet": f"data/NSE/{symbol}_NS.parquet"
       for symbol in STOCK_FILES},
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _business_days_between(start: date, end: date) -> int:
    if end <= start:
        return 0
    elapsed = (end - start).days
    weeks, remaining = divmod(elapsed, 7)
    days = weeks * 5
    for offset in range(1, remaining + 1):
        if (start.weekday() + offset) % 7 < 5:
            days += 1
    return days


def _as_date(value: Any, *, path: Path) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise ValueError(f"{path}: invalid date {value!r}") from exc
    raise ValueError(f"{path}: unsupported date value {value!r}")


def validate_parquet(
    path: Path,
    *,
    today: date,
    max_stale_weekdays: int = MAX_STALE_WEEKDAYS,
) -> Dict[str, Any]:
    """Validate a Parquet OHLCV dataset, schema, date range and freshness."""
    if path.stat().st_size < 8 or path.read_bytes()[:4] != b"PAR1":
        raise ValueError(f"{path}: not a Parquet file")
    parquet = pq.ParquetFile(path)
    schema_names = parquet.schema_arrow.names
    row_count = parquet.metadata.num_rows
    parquet.close()
    columns_by_lower = {name.casefold(): name for name in schema_names}
    date_column = next(
        (columns_by_lower[key] for key in ("date", "timestamp", "datetime")
         if key in columns_by_lower),
        None,
    )
    close_column = columns_by_lower.get("close")
    if date_column is None or close_column is None:
        raise ValueError(
            f"{path}: requires a date/timestamp and Close column; "
            f"found {parquet.schema_arrow.names}")
    if row_count == 0:
        raise ValueError(f"{path}: dataset is empty")

    columns = pq.read_table(path, columns=[date_column, close_column])
    valid = columns.drop_null()
    if valid.num_rows == 0:
        raise ValueError(f"{path}: has no rows with both date and Close")
    min_date = _as_date(pa.compute.min(valid.column(date_column)).as_py(), path=path)
    max_date = _as_date(pa.compute.max(valid.column(date_column)).as_py(), path=path)
    if max_date > today:
        raise ValueError(f"{path}: latest date {max_date} is in the future")
    stale_weekdays = _business_days_between(max_date, today)
    if stale_weekdays > max_stale_weekdays:
        raise ValueError(
            f"{path}: latest date {max_date} is {stale_weekdays} weekdays old; "
            f"limit is {max_stale_weekdays}")
    close_values = valid.column(close_column).to_pylist()
    if any(not isinstance(value, (int, float)) for value in close_values):
        raise ValueError(f"{path}: Close column contains non-numeric values")
    if any(value <= 0 for value in close_values):
        raise ValueError(f"{path}: Close column contains non-positive values")
    return {
        "records": row_count,
        "first_date": min_date.isoformat(),
        "latest_date": max_date.isoformat(),
        "stale_weekdays": stale_weekdays,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _request_bytes(session: requests.Session, url: str, *, limit: int) -> bytes:
    last_error: Optional[Exception] = None
    for attempt in range(RETRY_LIMIT):
        try:
            with session.get(url, stream=True, timeout=REQUEST_TIMEOUT) as response:
                response.raise_for_status()
                length = response.headers.get("Content-Length")
                if length and int(length) > limit:
                    raise ValueError(f"response exceeds {limit} bytes")
                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > limit:
                        raise ValueError(f"response exceeds {limit} bytes")
                    chunks.append(chunk)
                if not chunks:
                    raise ValueError(f"empty response from {url}")
                return b"".join(chunks)
        except (requests.RequestException, OSError, ValueError) as exc:
            last_error = exc
            if isinstance(exc, ValueError) and "exceeds" in str(exc):
                break
            if attempt + 1 < RETRY_LIMIT:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"failed to download {url}: {last_error}") from last_error


def _get_latest_revision(session: requests.Session) -> str:
    response = session.get(
        SOURCE_API,
        headers={"Accept": "application/vnd.github+json"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    default_branch = response.json()["default_branch"]
    branch_url = f"{SOURCE_API}/commits/{default_branch}"
    commit_response = session.get(branch_url, timeout=REQUEST_TIMEOUT)
    commit_response.raise_for_status()
    revision = commit_response.json()["sha"]
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise ValueError("upstream API did not return a full commit SHA")
    return revision.lower()


def refresh_sample_data(
    output_dir: Path = DEFAULT_OUTPUT,
    *,
    revision: Optional[str] = None,
    today: Optional[date] = None,
    max_stale_weekdays: int = MAX_STALE_WEEKDAYS,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Refresh all index and sample-equity files, then publish a manifest.

    Every source file is downloaded and validated into a temporary directory
    before the existing local dataset is replaced. Any download or validation
    error leaves the prior published dataset intact.
    """
    if max_stale_weekdays < 0:
        raise ValueError("max_stale_weekdays must be non-negative")
    if revision is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise ValueError("revision must be a full 40-character commit SHA")

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_date = today or datetime.now(timezone.utc).date()
    http = session or requests.Session()
    owns_session = session is None
    if owns_session:
        http.headers.update({"User-Agent": "BlueEngine-SampleData/1.0"})
    try:
        source_revision = revision.lower() if revision else _get_latest_revision(http)
        base_url = f"{RAW_BASE}/{source_revision}"
        with tempfile.TemporaryDirectory(
                prefix=".refresh-", dir=str(output_dir)) as temp_name:
            stage = Path(temp_name)
            file_manifest: Dict[str, Any] = {}
            for local_name, upstream_name in SAMPLE_FILES.items():
                staged_file = stage / local_name
                staged_file.parent.mkdir(parents=True, exist_ok=True)
                raw = _request_bytes(
                    http, f"{base_url}/{upstream_name}", limit=MAX_FILE_BYTES)
                staged_file.write_bytes(raw)
                details = validate_parquet(
                    staged_file, today=run_date,
                    max_stale_weekdays=max_stale_weekdays)
                file_manifest[local_name] = {
                    **details,
                    "source_path": upstream_name,
                }
                LOG.info(
                    "%s: %s rows through %s",
                    local_name, details["records"], details["latest_date"])

            license_bytes = _request_bytes(
                http, f"{base_url}/LICENSE", limit=1024 * 1024)
            license_text = license_bytes.decode("utf-8")
            if ("NON-COMMERCIAL" not in license_text
                    or "Copyright" not in license_text):
                raise ValueError("upstream license changed; review before use")
            (stage / "LICENSE").write_bytes(license_bytes)

            manifest: Dict[str, Any] = {
                "data_class": "historical",
                "updated_at_utc": datetime.now(timezone.utc).isoformat(
                    timespec="seconds").replace("+00:00", "Z"),
                "source_repository": SOURCE_REPOSITORY,
                "source_revision": source_revision,
                "license": (
                    "Personal, non-commercial, educational research only. "
                    "Commercial use requires prior written permission. "
                    "See LICENSE for full terms."
                ),
                "symbols": {
                    "indices": list(INDEX_FILES),
                    "nse_stocks": list(STOCK_FILES),
                },
                "max_stale_weekdays": max_stale_weekdays,
                "files": file_manifest,
            }
            (stage / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            for local_name in SAMPLE_FILES:
                destination = output_dir / local_name
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage / local_name, destination)
            os.replace(stage / "LICENSE", output_dir / "LICENSE")
            os.replace(stage / "manifest.json", output_dir / "manifest.json")
            return manifest
    finally:
        if owns_session:
            http.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh all upstream indices plus five NSE sample stocks.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--revision", help="Full upstream commit SHA (default: latest)")
    parser.add_argument("--max-stale-weekdays", type=int,
                        default=MAX_STALE_WEEKDAYS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        manifest = refresh_sample_data(
            args.output_dir,
            revision=args.revision,
            max_stale_weekdays=args.max_stale_weekdays,
        )
    except (OSError, requests.RequestException, RuntimeError, ValueError,
            KeyError) as exc:
        LOG.error("sample database refresh failed: %s", exc)
        return 1
    print(json.dumps({
        "updated_at_utc": manifest["updated_at_utc"],
        "source_revision": manifest["source_revision"],
        "files": {
            name: {
                "records": metadata["records"],
                "latest_date": metadata["latest_date"],
            }
            for name, metadata in manifest["files"].items()
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
