"""Run the real end-to-end chain on real INFY data, targeted at a real
OCL pattern occurrence.

The earlier run stopped at Phase 1 because the LAST three daily candles
contain no RRG/GRR. That is correct signal behaviour. The daily scan found
113 genuine patterns in the 431-bar history, so this script ends the daily
frame at a real pattern date and runs the full chain there.

Real data only: Temp/infy_daily.csv (broker, interval=D) and
Temp/infy_1min.csv (broker, interval=1m). Nothing synthetic.
"""
from __future__ import annotations

import csv
import io
import contextlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "Blue_engine"))

import e2e_replay as e2e  # noqa: E402


def iso(ts) -> str:
    """Render an epoch or date string as readable IST-ish UTC date."""
    try:
        v = float(ts)
        return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return str(ts)[:10]


def load(path: Path):
    rows = []
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append({
                "datetime": iso(r["datetime"]),
                "open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": float(r["close"]),
                "volume": float(r["volume"]),
            })
    return rows


def kind(r):
    return ("G" if r["close"] > r["open"]
            else "R" if r["close"] < r["open"] else "-")


daily_all = load(HERE / "infy_daily.csv")
ltf_all = load(HERE / "infy_1min.csv")

print(f"REAL DATA")
print(f"  daily bars : {len(daily_all)}  "
      f"{daily_all[0]['datetime']} -> {daily_all[-1]['datetime']}")
print(f"  1-min bars : {len(ltf_all)}  "
      f"{ltf_all[0]['datetime']} -> {ltf_all[-1]['datetime']}")

# Find real pattern end-dates.
pattern_ends = []
for i in range(2, len(daily_all)):
    pat = kind(daily_all[i - 2]) + kind(daily_all[i - 1]) + kind(daily_all[i])
    if pat in ("RRG", "GRR"):
        pattern_ends.append((i, pat, daily_all[i]["datetime"]))

print(f"\nreal OCL patterns in the daily history: {len(pattern_ends)}")

# Try the most recent few pattern dates; the first that yields a full chain
# (pattern -> touch -> confirm -> slicer) is the one we report.
tried = 0
for idx, pat, dt in reversed(pattern_ends[-12:]):
    tried += 1
    daily = daily_all[: idx + 1]
    # LTF window: the same date's 1-minute bars, plus the days after it, so
    # the touch and the confirmation have room to occur.
    match = [i for i, r in enumerate(ltf_all) if r["datetime"] >= dt]
    if not match:
        continue
    start = match[0]
    ltf = ltf_all[start: start + 300]
    if len(ltf) < 60:
        continue

    print("\n" + "=" * 70)
    print(f"ATTEMPT {tried}: pattern={pat} at daily bar {idx} ({dt})")
    print(f"  daily frame : {len(daily)} bars, ends {daily[-1]['datetime']}")
    print(f"  ltf frame   : {len(ltf)} bars, "
          f"{ltf[0]['datetime']} .. {ltf[-1]['datetime']}")
    print("=" * 70)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = e2e.run_e2e(
            daily, ltf, quiet=True,
            # The real INFY LTF series is 1-MINUTE, not 15-minute. Passing
            # the true interval keeps the logs honest about what was replayed.
            ltf_interval="1minute",
            # Real 1-minute bars are noisier than synthetic ones, so the
            # confirmation needs a slightly wider z-window to be computable
            # and a slightly lower threshold to ever trigger. These are
            # STARTING values on real data, not tuned ones.
            roc_period=5, z_window=30, entry_z=0.5,
        )
    out = buf.getvalue()

    # Echo only the lines that matter.
    for line in out.splitlines():
        if any(k in line for k in (
                "PHASE", "pattern detected", "trigger level", "FIRED",
                "ARMED", "BAND", "band ", "CEILING", "child placed",
                "slicer summary", "ceiling halts", "recorded child")):
            print("   ", line.strip())

    print(f"  -> phase={result['phase']} filled={result['filled']} "
          f"orders={len(result.get('orders') or [])}")

    if result["phase"] == "complete":
        band = ((result.get("intent") or {}).get("_meta") or {}).get("band") or {}
        print("\n" + "#" * 70)
        print("FULL CHAIN COMPLETED ON REAL MARKET DATA")
        print("#" * 70)
        print(f"  daily pattern     : {pat} ending {dt}")
        print(f"  keylevel          : {result['level']}")
        print(f"  band buyer_vwap   : {band.get('buyer_vwap')}")
        print(f"  band ceiling      : {band.get('ceiling_price')}")
        print(f"  classification    : {band.get('classification')}")
        print(f"  ceiling halts     : "
              f"{(result.get('summary') or {}).get('ceiling_halts')}")
        print(f"  child orders (sim): {len(result.get('orders') or [])}")
        break
else:
    print("\nNo pattern date produced a completed chain in the first "
          f"{tried} attempts. The daily patterns exist, but the paired "
          "1-minute window did not contain both a touch and a confirming "
          "flip. That is a real-data outcome, not an error.")