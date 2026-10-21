"""Scrape one saved Screener.in screen and export its rows and fundamentals."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv

from .client.screener_client import ScreenerClient

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPOSITORY_ROOT / "Blue_engine" / "input" / "screener_engine.json"


def scrape_saved_screen(
    client: ScreenerClient,
    screen_id: int,
    screen_slug: str,
    output_dir: Path,
    *,
    max_pages: int = 40,
    max_symbols: int = 250,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch every screen page, then fetch full company statements sequentially."""
    screen = client.get_screen_details(
        screen_id, screen_slug, include_all_pages=True, max_pages=max_pages)
    if (not screen.ok and screen.error
            and "LOGIN_REQUIRED" in screen.error
            and username and password):
        login = client.login(username, password)
        if not login.ok:
            raise RuntimeError(f"Screener.in login failed: {login.error}")
        screen = client.get_screen_details(
            screen_id, screen_slug, include_all_pages=True, max_pages=max_pages)
    if not screen.ok or not screen.data:
        raise RuntimeError(f"screen fetch failed: {screen.error}")
    if screen.warnings:
        raise RuntimeError(
            "screen pagination was incomplete: " + "; ".join(screen.warnings))

    summary_rows: List[Dict[str, Any]] = []
    seen = set()
    for row in screen.data.get("rows") or []:
        symbol = str(row.get("_symbol") or "").strip().upper()
        if not symbol:
            raise RuntimeError(
                "a screen row has no company symbol link; refusing an incomplete export")
        if symbol in seen:
            continue
        seen.add(symbol)
        summary_rows.append({
            "symbol": symbol,
            **{key: value for key, value in row.items() if not key.startswith("_")},
        })

    if len(summary_rows) > max_symbols:
        raise RuntimeError(
            f"screen returned {len(summary_rows)} companies, above the configured "
            f"max_symbols={max_symbols}; raise the explicit cap to export all")

    details: List[Dict[str, Any]] = []
    for index, summary in enumerate(summary_rows):
        symbol = summary["symbol"]
        response = client.get_company(symbol, "consolidated")
        if response.ok and response.data:
            details.append({
                "symbol": symbol,
                "ok": True,
                "source": response.meta.as_dict() if response.meta else None,
                "data": response.data,
            })
        else:
            details.append({
                "symbol": symbol,
                "ok": False,
                "error": response.error or "empty company response",
                "source": response.meta.as_dict() if response.meta else None,
            })
        if index + 1 < len(summary_rows):
            time.sleep(1.5)

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "filtered_summary.csv"
    detail_path = output_dir / "enriched_details.json"

    # Resolve each screen symbol to a tradable (symbol, exchange) pair so
    # downstream stages (OpenAlgo history, watchlists, backtests) can use
    # the export directly - BSE-code rows get the exchange tag they need.
    from .symbol_map import resolve_many
    resolved = resolve_many(row["symbol"] for row in summary_rows)
    for row in summary_rows:
        match = resolved.get(row["symbol"])
        if match is not None:
            row["tradable_symbol"] = match.symbol
            row["exchange"] = match.exchange
            row["isin"] = match.isin
            row["symbol_resolved"] = "true" if match.resolved else "false"

    columns = ["symbol"]
    for row in summary_rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with summary_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(summary_rows)

    detail_payload = {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": f"https://www.screener.in/screens/{screen_id}/{screen_slug}/",
        "data_kind": "fundamental_snapshot",
        "data_date_note": (
            "Fundamentals are as last reported on Screener.in; they are not live prices."),
        "screen": {
            "id": screen_id,
            "slug": screen_slug,
            "title": screen.data.get("title"),
            "total_pages": screen.data.get("total_pages"),
            "companies": len(summary_rows),
        },
        "companies": details,
    }
    detail_path.write_text(
        json.dumps(detail_payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    return {
        "summary_path": str(summary_path),
        "details_path": str(detail_path),
        "companies": len(summary_rows),
        "company_failures": sum(not item["ok"] for item in details),
    }


def _read_config(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read config {path}: {exc}") from exc
    return payload.get("screener") or {}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Authenticate to Screener.in, download all pages of a saved screen, "
            "and export company fundamentals."))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--screen-id", type=int)
    parser.add_argument("--screen-slug")
    parser.add_argument("--output-dir", type=Path, default=Path.cwd())
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--max-symbols", type=int)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    load_dotenv(REPOSITORY_ROOT / ".env", override=False)

    try:
        config = _read_config(args.config)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    screen_url = os.environ.get("URL_SCREENER_DOT_IN") or config.get("screen_url")
    screen_match = re.search(
        r"/screens/(\d+)/([^/?#]+)",
        urlparse(str(screen_url)).path) if screen_url else None
    screen_id = (
        args.screen_id if args.screen_id is not None
        else config.get("screen_id")
        or (screen_match.group(1) if screen_match else None))
    screen_slug = (
        args.screen_slug or config.get("screen_slug")
        or (screen_match.group(2) if screen_match else None))
    username = (
        os.environ.get("SCREENER_USERNAME")
        or os.environ.get("Screenerdotin_username")
        or os.environ.get("username")
        or os.environ.get("email"))
    password = os.environ.get("SCREENER_PASSWORD") or os.environ.get("password")
    session_id = (
        os.environ.get("Screenerdotin_sessionid")
        or os.environ.get("SCREENER_SESSION_ID")
        or os.environ.get("SCREENER_SESSIONID")
        or "")
    if not screen_id or not screen_slug:
        print("ERROR: provide --screen-id and --screen-slug or set them in config.",
              file=sys.stderr)
        return 2
    if not session_id and not (username and password):
        print(
            "ERROR: set Screenerdotin_sessionid or Screener username/password "
            "in the repository-root .env.",
            file=sys.stderr)
        return 2

    max_pages = args.max_pages or int(config.get("max_pages", 40))
    max_symbols = args.max_symbols or int(config.get("max_symbols", 250))
    client = ScreenerClient(
        base_url=str(config.get("base_url") or "https://www.screener.in"),
        cache_ttl_ms=int(config.get("cache_ttl_ms", 21_600_000)),
        min_interval_ms=max(1500, int(config.get("min_interval_ms", 400))),
        max_retries=int(config.get("max_retries", 2)),
        timeout_ms=int(config.get("fetch_timeout_ms", 20_000)),
        proxy_url=str(config.get("proxy_url") or ""),
        cookies=(str(config.get("cookies") or "")
                 or (f"sessionid={session_id}" if session_id else "")),
        cache_dir=str(
            config.get("cache_dir")
            or (Path(__file__).resolve().parent / "cache")),
        verify_tls=bool(config.get("verify_tls", True)),
    )

    try:
        result = scrape_saved_screen(
            client, int(screen_id), str(screen_slug), args.output_dir,
            max_pages=max_pages, max_symbols=max_symbols,
            username=username, password=password)
    except (RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Companies: {result['companies']}")
    print(f"Summary: {result['summary_path']}")
    print(f"Details: {result['details_path']}")
    if result["company_failures"]:
        print(f"WARNING: {result['company_failures']} company fetch(es) failed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
