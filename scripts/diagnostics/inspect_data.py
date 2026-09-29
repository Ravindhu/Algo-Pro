"""Inspect local SQLite stores for real OHLCV data usable by the simulators."""
import os
import sqlite3
import sys

CANDIDATES = [
    "data/price_cache/prices.db",
    "openalgo/db/historify.db",
    "openalgo/db/openalgo.db",
]

for path in CANDIDATES:
    print("=" * 70)
    print(path, "exists:", os.path.exists(path))
    if not os.path.exists(path):
        continue
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        cur = con.cursor()
        tables = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        print("tables:", [t[0] for t in tables])
        for (name,) in tables:
            try:
                cols = cur.execute(f"PRAGMA table_info({name})").fetchall()
                colnames = [c[1] for c in cols]
                n = cur.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                print(f"  {name}: {n} rows")
                print(f"    cols: {colnames}")
                # Does it look like OHLCV?
                lower = {c.lower() for c in colnames}
                if {"open", "high", "low", "close"} <= lower:
                    print("    >> OHLCV-LIKE TABLE <<")
                    sample = cur.execute(
                        f"SELECT * FROM {name} LIMIT 2"
                    ).fetchall()
                    for s in sample:
                        print("      ", dict(zip(colnames, s)))
            except Exception as exc:
                print(f"  {name}: error {exc}")
        con.close()
    except Exception as exc:
        print("  open failed:", exc)