"""Read the historify download job record to see its real outcome."""
import duckdb

con = duckdb.connect("openalgo/db/historify.duckdb", read_only=True)

print("=== download_jobs ===")
cols = [c[0] for c in con.execute("DESCRIBE download_jobs").fetchall()]
for row in con.execute("SELECT * FROM download_jobs").fetchall():
    for k, v in zip(cols, row):
        print(f"  {k:<20}: {v}")
    print("  " + "-" * 50)

print("\n=== job_items ===")
cols = [c[0] for c in con.execute("DESCRIBE job_items").fetchall()]
for row in con.execute("SELECT * FROM job_items").fetchall():
    for k, v in zip(cols, row):
        print(f"  {k:<20}: {v}")

print("\n=== market_data row count ===")
print("  rows:", con.execute("SELECT COUNT(*) FROM market_data").fetchone()[0])

con.close()