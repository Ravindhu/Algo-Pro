"""Temp probe: what is actually inside the price cache, and can ML train on it?

Throwaway diagnostic (Temp/ is the staging workspace). Read-only.
"""
import sqlite3

from blue_engine.ml.data_sources.price_cache import PriceCacheSource

src = PriceCacheSource()
db = src._cache().db_path
print("cache db =", db)

con = sqlite3.connect(str(db))
cur = con.cursor()
tables = [r[0] for r in cur.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("tables:", tables)
for t in tables:
    try:
        n = cur.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        print("  %-24s %8d rows" % (t, n))
    except Exception as exc:
        print("  %-24s error: %r" % (t, exc))

# Which instruments and how much history?
for t in tables:
    cols = [r[1] for r in cur.execute("PRAGMA table_info(%s)" % t).fetchall()]
    if "symbol" in cols:
        print("\n-- %s sample --" % t)
        q = ("SELECT symbol, exchange, interval, COUNT(*), MIN(ts), MAX(ts) "
             "FROM %s GROUP BY symbol, exchange, interval "
             "ORDER BY COUNT(*) DESC LIMIT 15" % t)
        for row in cur.execute(q).fetchall():
            print("  %s/%s/%s  n=%d  %s .. %s" % row)
        break
con.close()