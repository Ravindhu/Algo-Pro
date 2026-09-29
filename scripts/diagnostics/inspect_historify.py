"""Inspect the historify DuckDB store for the INFY 1-minute download."""
import os
import sys

try:
    import duckdb
except ImportError:
    print("duckdb module NOT installed in this venv")
    sys.exit(1)

path = "openalgo/db/historify.duckdb"
print("exists:", os.path.exists(path))
if not os.path.exists(path):
    sys.exit(1)
print("size:", os.path.getsize(path), "bytes")

con = duckdb.connect(path, read_only=True)
print("\n=== tables ===")
tables = con.execute("SHOW TABLES").fetchall()
for (t,) in tables:
    print(" ", t)

for (t,) in tables:
    try:
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        cols = [c[0] for c in con.execute(f'DESCRIBE "{t}"').fetchall()]
        print(f"\n=== {t}: {n} rows ===")
        print("  cols:", cols)
    except Exception as exc:
        print(f"\n=== {t}: error {exc}")

# Look for anything resembling symbol/interval coverage.
for (t,) in tables:
    cols = [c[0] for c in con.execute(f'DESCRIBE "{t}"').fetchall()]
    lower = {c.lower() for c in cols}
    if {"symbol", "interval"} <= lower or "symbol" in lower:
        print(f"\n=== coverage in {t} ===")
        try:
            sym_col = next(c for c in cols if c.lower() == "symbol")
            iv_col = next((c for c in cols if c.lower() == "interval"), None)
            if iv_col:
                q = (f'SELECT "{sym_col}", "{iv_col}", COUNT(*) '
                     f'FROM "{t}" GROUP BY 1,2 ORDER BY 3 DESC LIMIT 25')
            else:
                q = (f'SELECT "{sym_col}", COUNT(*) '
                     f'FROM "{t}" GROUP BY 1 ORDER BY 2 DESC LIMIT 25')
            for row in con.execute(q).fetchall():
                print("  ", row)
        except Exception as exc:
            print("  coverage query failed:", exc)

con.close()