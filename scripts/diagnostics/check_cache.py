"""Check which symbols/intervals the local price cache holds."""
import sqlite3

con = sqlite3.connect("file:data/price_cache/prices.db?mode=ro", uri=True)
print("=== coverage by symbol/exchange/interval ===")
q = ("SELECT symbol, exchange, interval, COUNT(*) AS n, "
     "MIN(ts) AS first, MAX(ts) AS last "
     "FROM ohlcv GROUP BY 1,2,3 ORDER BY n DESC LIMIT 20")
for row in con.execute(q):
    print(f"  {row[0]:<12} {row[1]:<10} {row[2]:<5} n={row[3]:<7} "
          f"{row[4]} -> {row[5]}")
con.close()