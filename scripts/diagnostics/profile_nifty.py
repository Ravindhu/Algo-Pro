"""Profile the real NIFTY OHLCV store before using it in a replay."""
import sqlite3

con = sqlite3.connect("file:data/price_cache/prices.db?mode=ro", uri=True)
cur = con.cursor()

print("=== coverage by symbol/exchange/interval ===")
for row in cur.execute("""
    SELECT symbol, exchange, interval, COUNT(*) AS n,
           MIN(ts) AS first, MAX(ts) AS last
    FROM ohlcv
    GROUP BY symbol, exchange, interval
    ORDER BY n DESC
    LIMIT 20
"""):
    print(f"  {row[0]:<12} {row[1]:<8} {row[2]:<5} n={row[3]:<7} "
          f"{row[4]} -> {row[5]}")

print("\n=== NIFTY daily row count and sanity ===")
n, lo, hi = cur.execute("""
    SELECT COUNT(*), MIN(close), MAX(close) FROM ohlcv
    WHERE symbol='NIFTY' AND exchange='YAHOO' AND interval='D'
""").fetchone()
print(f"  rows={n} close range {lo:.2f} .. {hi:.2f}")

print("\n=== are there any invalid rows (high<low, zero volume)? ===")
bad = cur.execute("""
    SELECT COUNT(*) FROM ohlcv
    WHERE symbol='NIFTY' AND interval='D'
      AND (high < low OR close <= 0 OR volume < 0)
""").fetchone()[0]
print(f"  invalid rows: {bad}")

print("\n=== last 5 daily rows ===")
cols = ["ts", "open", "high", "low", "close", "volume"]
for row in cur.execute("""
    SELECT ts, open, high, low, close, volume FROM ohlcv
    WHERE symbol='NIFTY' AND interval='D' ORDER BY ts DESC LIMIT 5
"""):
    print("  ", dict(zip(cols, row)))

con.close()