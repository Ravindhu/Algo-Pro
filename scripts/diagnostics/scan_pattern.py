"""Check whether the real INFY daily bars contain an RRG/GRR pattern.

If no pattern exists in the last three candles, the signal correctly
reports nothing - and the e2e run stopping at Phase 1 is CORRECT behaviour,
not a bug. This script verifies which it is, and scans recent history for
the most recent genuine pattern so a run can be targeted at real data that
actually contains one.
"""
from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys_path_added = False

ROWS = []
with (Path(__file__).resolve().parent / "infy_daily.csv").open(
        encoding="utf-8") as fh:
    for r in csv.DictReader(fh):
        ROWS.append({
            "datetime": r["datetime"],
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": float(r["volume"]),
        })

print(f"daily bars: {len(ROWS)}")
print(f"range     : {ROWS[0]['datetime']} -> {ROWS[-1]['datetime']}")

print("\n=== last 5 daily candles ===")
for r in ROWS[-5:]:
    o, c = r["open"], r["close"]
    kind = "green" if c > o else ("red" if c < o else "flat")
    print(f"  {r['datetime']}  O={o:8.2f} H={r['high']:8.2f} "
          f"L={r['low']:8.2f} C={c:8.2f}  {kind}")


def kind(r):
    if r["close"] > r["open"]:
        return "G"
    if r["close"] < r["open"]:
        return "R"
    return "-"


print("\n=== scan for the most recent RRG (R,R,G) and GRR (G,R,R) ===")
found = []
for i in range(2, len(ROWS)):
    c1, c2, c3 = ROWS[i - 2], ROWS[i - 1], ROWS[i]
    pat = kind(c1) + kind(c2) + kind(c3)
    if pat in ("RRG", "GRR"):
        found.append((ROWS[i]["datetime"], pat, c3))

print(f"patterns found in history: {len(found)}")
for dt, pat, c3 in found[-6:]:
    print(f"  {dt}  {pat}  close={c3['close']:.2f}")

if not found:
    print("  NONE - the daily series contains no 3-candle OCL pattern")
else:
    last_dt = found[-1][0]
    print(f"\nmost recent pattern date: {last_dt}")
    print("A run aimed at real data should end its daily frame on or near "
          "that date so the pattern is present in the last 3 candles.")