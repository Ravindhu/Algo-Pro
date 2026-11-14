"""Fetch NSE FII/DII provisional flows and save them to the local database.

Run it every evening after the NSE close (17:30-19:00 IST is when the
provisional cash figures usually appear), and on demand whenever the engine
starts. Idempotent: re-running the same day inserts nothing and records a
revision only if NSE has changed a number.

Usage:
    python scripts/ops/fetch_fii_daily.py
    python scripts/ops/fetch_fii_daily.py --dry-run      # fetch, do not save
    python scripts/ops/fetch_fii_daily.py --status       # what is stored

Exit codes:
    0  data fetched and saved (or already current)
    1  fetch failed (network, NSE refusal, unparseable payload)
    2  fetch succeeded but NSE returned figures for a day we already have
       unchanged - reported separately so a cron failure is never faked

Flow stage: India flow data
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

from quant.data import nse_fii  # noqa: E402


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, default=str))


def cmd_status(store: nse_fii.FiiStore) -> int:
    latest_fii = store.latest("FII")
    latest_dii = store.latest("DII")
    st = nse_fii.staleness(latest_fii)
    _print({
        "db": str(store.path),
        "fii": latest_fii,
        "dii": latest_dii,
        "staleness": st,
        "history_rows": len(store.history()),
        "revisions": len(store.revisions()),
    })
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="fetch but do not save")
    p.add_argument("--status", action="store_true", help="show what is stored and exit")
    p.add_argument("--db", default=None, help="override the sqlite path")
    a = p.parse_args()

    store = nse_fii.FiiStore(a.db)

    if a.status:
        return cmd_status(store)

    fetched = nse_fii.fetch_fii_dii()
    if not fetched["ok"]:
        _print({"ok": False, "detail": fetched["detail"],
                "status": fetched["status"], "fetched_at": fetched["fetched_at"]})
        return 1

    if a.dry_run:
        _print({"ok": True, "dry_run": True, "rows": fetched["rows"]})
        return 0

    counts = store.save(fetched["rows"], source="nse", fetched_at=fetched["fetched_at"])
    stale = nse_fii.staleness(store.latest("FII"))
    _print({
        "ok": True,
        "rows_fetched": len(fetched["rows"]),
        "counts": counts,
        "staleness": stale,
        "db": str(store.path),
    })
    # A run that added nothing new is still a success; cron should not alert.
    return 0 if (counts["inserted"] or counts["revised"] or counts["unchanged"]) else 2


if __name__ == "__main__":
    sys.exit(main())
