"""Fetch bulk and block deals for the screened universe and store them.

Run every evening after the NSE close. Deals are kept while their symbol stays in
the screener; when a symbol leaves, its deals are MARKED removed, never deleted.

Prerequisites: data/screened/latest.json must exist (the screener writer produces
it). An empty screen means nothing is fetched - that is deliberate.

Usage:
    python scripts/ops/fetch_deals_daily.py
    python scripts/ops/fetch_deals_daily.py --dry-run
    python scripts/ops/fetch_deals_daily.py --status
    python scripts/ops/fetch_deals_daily.py --symbols SBIN,TCS --trade-date 2026-11-14
    python scripts/ops/fetch_deals_daily.py --prefer nse_mcp

Exit codes:
    0  fetched (deals may still be zero for the session)
    1  both sources failed
    2  screened universe empty - nothing attempted

Flow stage: Bulk and block deals
Status: PHASE 2 IMPLEMENTED (2026-11-14)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.data import nse_deals  # noqa: E402
from quant.data import nse_mcp_client  # noqa: E402


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, default=str))


def cmd_status(store: "nse_deals.DealsStore") -> int:
    _print({
        "db": str(store.path),
        "sources_available": nse_mcp_client.available_sources(),
        "preferred_source": nse_mcp_client.best_source(),
        "summary": store.summary(),
        "members": store.membership()[:10],
        "recent_deals": store.all_deals()[:10],
    })
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="fetch but do not save")
    p.add_argument("--status", action="store_true", help="show stored state and exit")
    p.add_argument("--db", default=None)
    p.add_argument("--screen", default=None, help="path to screened/latest.json")
    p.add_argument("--symbols", default=None, help="comma list, overrides the screen")
    p.add_argument("--trade-date", default=None, help="ISO date to tag the session")
    p.add_argument("--prefer", default=None, choices=["nse_mcp", "jugaad_data", "nse_json"])
    a = p.parse_args()

    store = nse_deals.DealsStore(a.db)

    if a.status:
        return cmd_status(store)

    day = date.fromisoformat(a.trade_date) if a.trade_date else None
    symbols = ([s.strip().upper() for s in a.symbols.split(",") if s.strip()]
               if a.symbols else None)

    if a.dry_run:
        resolved = symbols if symbols is not None else nse_mcp_client._screened_symbols(a.screen)
        _print({"dry_run": True, "symbols": resolved,
                "sources": nse_mcp_client.available_sources()})
        return 0 if resolved else 2

    report = nse_deals.fetch_and_store(
        store, symbols=symbols, trade_date=day,
        prefer=a.prefer, screen_path=a.screen)
    _print(report)

    if not report["symbols"]:
        return 2
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
