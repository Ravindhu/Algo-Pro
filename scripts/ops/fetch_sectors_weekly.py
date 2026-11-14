"""Fetch NSE sector and index levels and save them for relative strength.

Run weekly, Saturday morning after Friday's close (NSE is shut on Saturday).
Each run saves ONE snapshot; the saved snapshots become the weekly series that
Mansfield relative strength consumes. Idempotent: a second run on the same date
inserts nothing and records a revision only if NSE changed a level.

Usage:
    python scripts/ops/fetch_sectors_weekly.py
    python scripts/ops/fetch_sectors_weekly.py --dry-run
    python scripts/ops/fetch_sectors_weekly.py --status
    python scripts/ops/fetch_sectors_weekly.py --trade-date 2026-11-14

Exit codes:
    0  fetched and saved (or already current)
    1  fetch failed (network, NSE refusal, unparseable payload)
    2  fetched but every row was already stored unchanged

Flow stage: Relative strength
Status: PHASE 2 IMPLEMENTED (2026-11-14)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import date, datetime

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.data import nse_sectors  # noqa: E402


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, default=str))


def cmd_status(store: "nse_sectors.SectorStore") -> int:
    names = store.index_names()
    latest = store.levels()
    _print({
        "db": str(store.path),
        "indices_tracked": len(names),
        "latest_date": latest[0]["trade_date"] if latest else None,
        "rows_on_latest": len(latest),
        "revisions": len(store.revisions()),
        "sample": latest[:5],
    })
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="fetch but do not save")
    p.add_argument("--status", action="store_true", help="show what is stored and exit")
    p.add_argument("--db", default=None, help="override the sqlite path")
    p.add_argument("--trade-date", default=None,
                   help="ISO date to tag the snapshot with (default: today)")
    a = p.parse_args()

    store = nse_sectors.SectorStore(a.db)

    if a.status:
        return cmd_status(store)

    day = date.fromisoformat(a.trade_date) if a.trade_date else date.today()
    fetched = nse_sectors.fetch_sector_levels(trade_date=day)
    if not fetched["ok"]:
        _print({"ok": False, "detail": fetched["detail"],
                "status": fetched["status"], "fetched_at": fetched["fetched_at"]})
        return 1

    if a.dry_run:
        _print({"ok": True, "dry_run": True, "rows": len(fetched["rows"]),
                "sample": fetched["rows"][:5]})
        return 0

    counts = store.save(fetched["rows"], source="nse",
                        fetched_at=fetched["fetched_at"])
    ready = {}
    for name in ("NIFTY 50", "NIFTY BANK"):
        ready[name] = nse_sectors.weekly_series(name, path=store.path)["ready"]
    _print({
        "ok": True,
        "trade_date": day.isoformat(),
        "rows_fetched": len(fetched["rows"]),
        "counts": counts,
        "rs_ready": ready,
        "db": str(store.path),
    })
    return 0 if (counts["inserted"] or counts["revised"]) else 2


if __name__ == "__main__":
    sys.exit(main())
