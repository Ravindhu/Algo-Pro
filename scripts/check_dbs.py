"""Health check for the OpenAlgo SQLite databases.

Runs ``PRAGMA integrity_check`` plus a quick table count on every database the
app opens. A corrupt SQLite file is the one failure mode that looks like
"everything works" right up until an order is placed, so it is worth checking
explicitly rather than assuming.

Usage (from the openalgo/ folder):
    .venv\\Scripts\\python.exe ..\\Scripts\\check_dbs.py
"""

from __future__ import annotations

import os
import sqlite3
import sys

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
    print(f"{'database':<18} {'size':>10}  {'integrity':<12} tables")
    print("-" * 55)
    for path in DATABASES:
        if not os.path.exists(path):
            print(f"{path:<18} {'-':>10}  {'MISSING':<12} -")
            failures += 1
            continue
        size = os.path.getsize(path)
        try:
            conn = sqlite3.connect(path)
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check")
            integrity = cur.fetchone()[0]
            tables = count_tables(cur)
            conn.close()
            if integrity != "ok":
                failures += 1
            print(f"{path:<18} {size:>10,}  {integrity:<12} {tables}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"{path:<18} {size:>10,}  ERROR: {exc}")
    print("-" * 55)
    print("RESULT:", "ALL OK" if failures == 0 else f"{failures} PROBLEM(S)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())