"""Temp probe: which symbols/dates does the price cache hold? (cross-sectional input)

Cross-sectional learning needs MANY symbols on the SAME dates. This reports
how wide and how deep the cache actually is, because a panel that is wide but
shallow (each symbol a different period) cannot support it honestly.

Read-only.
"""
import os
import sqlite3
import sys
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.ml.data_sources.price_cache import PriceCacheSource

db = PriceCacheSource()._cache().db_path
print("cache db =", db)
con = sqlite3.connect(str(db))
tables = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

for t in tables:
    cols = [r[1] for r in con.execute("PRAGMA table_info(%s)" % t).fetchall()]
    if "symbol" not in cols:
        continue
    rows = con.execute(
        "SELECT symbol, exchange, COUNT(*), MIN(ts), MAX(ts) FROM %s "
        "GROUP BY symbol, exchange ORDER BY COUNT(*) DESC" % t).fetchall()
    print("\n-- %s : %d (symbol,exchange) groups --" % (t, len(rows)))
    for sym, exch, n, lo, hi in rows[:40]:
        print("  %-22s %-10s n=%-6d %s .. %s" % (sym, exch, n, lo, hi))

    # How many symbols share the same trading DAYS? That is what decides
    # whether a cross-sectional panel is possible at all.
    per_day = defaultdict(set)
    for sym, exch, ts in con.execute(
            "SELECT symbol, exchange, ts FROM %s" % t).fetchall():
        day = str(ts)[:10]
        per_day[day].add((sym, exch))
    if per_day:
        widths = sorted(len(v) for v in per_day.values())
        n_days = len(per_day)
        print("  days with data        : %d" % n_days)
        print("  median symbols/day    : %d" % widths[len(widths) // 2])
        print("  max symbols/day       : %d" % widths[-1])
        # days where >= 5 symbols overlap: the usable cross-section depth
        wide = sum(1 for w in widths if w >= 5)
        print("  days with >=5 symbols : %d" % wide)
    break
con.close()