"""Health check for the OpenAlgo SQLite databases.

Runs ``PRAGMA integrity_check`` plus a quick table count on every database the
app opens. A corrupt SQLite file is the one failure mode that looks like
"everything works" right up until an order is placed, so it is worth checking
explicitly rather than assuming.

Databases are resolved against the OpenAlgo clone root (``../openalgo/``), not
against the current working directory. The first version used bare relative
paths (``db/openalgo.db``) and therefore reported all five databases MISSING
when invoked from anywhere except inside the clone -- a false alarm that reads
exactly like real corruption. Paths are now anchored, so the script works from
anywhere.

Usage (from my-trading-system/):
    openalgo\\Scripts\\python.exe Scripts\\check_dbs.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

#: OpenAlgo keeps its SQLite files here, relative to the clone root.
CLONE_ROOT = Path(__file__).resolve().parent.parent.parent / "openalgo"

DATABASES = [
    "db/openalgo.db",
    "db/logs.db",
    "db/latency.db",
    "db/health.db",
    "db/sandbox.db",
]


def count_tables(cur: sqlite3.Cursor) -> int:
    cur.execute("SELECT count(1) FROM sqlite_master WHERE type = 'table'")
    return cur.fetchone()[0]


def main() -> int:
    failures = 0
    print(f"{'database':<24} {'size':>12}  {'integrity':<12} tables")
    print("-" * 62)
    for rel in DATABASES:
        path = CLONE_ROOT / rel
        if not path.exists():
            print(f"{rel:<24} {'-':>12}  {'MISSING':<12} -")
            failures += 1
            continue
        size = path.stat().st_size
        try:
            conn = sqlite3.connect(str(path))
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check")
            integrity = cur.fetchone()[0]
            tables = count_tables(cur)
            conn.close()
            if integrity != "ok":
                failures += 1
            print(f"{rel:<24} {size:>12,}  {integrity:<12} {tables}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"{rel:<24} {size:>12,}  ERROR: {exc}")
    print("-" * 62)
    print("RESULT:", "ALL OK" if failures == 0 else f"{failures} PROBLEM(S)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())