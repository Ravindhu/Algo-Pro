"""Derive real keylevels from real daily INFY OCL patterns.

The backtest harness needs a list of levels. Using arbitrary round numbers
would test nothing meaningful, so the levels come from the SAME logic the
production signal uses: _pattern.validate_pattern + calculate_trigger_level
applied to the real daily bars. That keeps the backtest honest about what it
is measuring - the confirmation filter's effect on real signal levels.

Writes Temp/real_levels.txt (one level per line).
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "Blue_engine"))

from blue_engine.alpha.signals._pattern import (  # noqa: E402
    calculate_trigger_level,
    validate_pattern,
)

OUT = HERE / "real_levels.json"
BARS_PER_DAY = 375          # a 1-minute NSE session
VALID_DAYS = 10             # a level stays actionable for this many days


def iso(ts):
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
            "%Y-%m-%d")
    except (TypeError, ValueError):
        return str(ts)[:10]


rows = []
with (HERE / "infy_daily.csv").open(encoding="utf-8") as fh:
    for r in csv.DictReader(fh):
        rows.append({
            "datetime": iso(r["datetime"]),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": float(r["volume"]),
        })

#
# Each level carries the bar at which its pattern COMPLETED, mapped into the
# 1-minute series. A level is only tested while active: from its formation
# bar, for VALID_DAYS trading days. Treating levels as timeless tested a
# February level (1365) against August bars (~1000), so 11 of 12 setups were
# never touched and the comparison had almost no trades to measure.
#
# Series start: the first bar of the 1-minute data. Formation bars are
# computed as the index of the first 1-minute bar on/after the pattern date.
first_ltf_date = None
with (HERE / "infy_1min.csv").open(encoding="utf-8") as fh:
    for r in csv.DictReader(fh):
        first_ltf_date = iso(r["datetime"])
        break

# Bar index of each distinct trading date in the 1-minute series.
day_start_bar: dict = {}
with (HERE / "infy_1min.csv").open(encoding="utf-8") as fh:
    for i, r in enumerate(csv.DictReader(fh)):
        d = iso(r["datetime"])
        day_start_bar.setdefault(d, i)

levels = []
for pat_type in ("RRG", "GRR"):
    for i in range(2, len(rows)):
        c1, c2, c3 = rows[i - 2], rows[i - 1], rows[i]
        if not validate_pattern(c1, c2, c3, pat_type, min_body_pct=0.3):
            continue
        lvl = calculate_trigger_level(c2, c3, pat_type)
        if lvl is None or lvl <= 0:
            continue
        levels.append((rows[i]["datetime"], pat_type, round(float(lvl), 2)))

print(f"\nvalidated patterns (min_body_pct=0.3): {len(levels)}")
for dt, pat, lvl in levels[-8:]:
    print(f"  {dt}  {pat}  level={lvl}")

if not levels:
    print("\nno validated patterns; nothing written")
    sys.exit(1)

# Only RRG (demand) levels: the BUY rule needs demand levels. GRR levels are
# supply levels requiring the SELL rule, which this pass does not run. That
# limitation is stated rather than hidden.
specs = []
for dt, pat, lvl in levels:
    if pat != "RRG":
        continue
    formed = day_start_bar.get(dt)
    if formed is None:
        continue                      # no 1-minute data on/after this date
    specs.append({
        "price": lvl,
        "formed_idx": formed,
        "valid_days": VALID_DAYS,
        "bars_per_day": BARS_PER_DAY,
        "pattern": pat,
    })

specs.sort(key=lambda s: (s["formed_idx"], s["price"]))
print(f"\nRRG levels with a live 1-minute window: {len(specs)}")
for s in specs:
    end_bar = s["formed_idx"] + s["valid_days"] * BARS_PER_DAY
    print(f"  level={s['price']:>8.2f}  formed_bar={s['formed_idx']:>6}  "
          f"active until bar {end_bar}")

if not specs:
    print("\nno RRG level had a usable 1-minute window; nothing written")
    sys.exit(1)

OUT.write_text(json.dumps(specs, indent=2), encoding="utf-8")
print(f"\nwrote {len(specs)} level specs to {OUT}")

lo = min(r["low"] for r in rows)
hi = max(r["high"] for r in rows)
print(f"daily range over the whole history: {lo:.2f} .. {hi:.2f}")