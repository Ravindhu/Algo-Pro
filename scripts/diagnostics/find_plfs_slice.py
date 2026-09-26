"""Find the exact PLFS headline slice, from the data rather than from a guess.

A filter of gender='person' + sector='rural + urban' + AgeGroup='All ages'
matched ZERO rows. The dimensional dump showed those values exist individually,
so the combination must differ -- most likely the MONTHLY series does not carry
a 'rural + urban' row at all (only rural and urban), while the ANNUAL series
does.

This prints every distinct (frequency, gender, sector, AgeGroup) combination
present, with a sample value, so the headline can be selected by evidence.

Run:
    openalgo\\Scripts\\python.exe Scripts\\find_plfs_slice.py
"""

from __future__ import annotations

import json
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "Blue_engine"))

from blue_engine.economics.providers.mospi_provider import (  # noqa: E402
    MospiProvider,
)


def main() -> int:
    p = MospiProvider({}, log=lambda m: print(f"  log: {m}"))

    rows = []
    for freq in ("1", "3"):
        got = p._get_data("PLFS", {"indicator_code": "3",
                                   "frequency_code": freq})
        print(f"frequency_code={freq} -> {len(got)} rows")
        rows.extend(got)
    print(f"total: {len(rows)}")

    print()
    print("=" * 100)
    print("Every distinct PLFS slice present")
    print("=" * 100)
    print(f"{'frequency':<10} {'gender':<8} {'sector':<14} {'AgeGroup':<22} "
          f"{'yr':<6} {'mo':<10} {'status':<7} value")
    print("-" * 100)

    seen = set()
    for r in rows:
        key = (str(r.get("frequency")), str(r.get("gender")),
               str(r.get("sector")), str(r.get("AgeGroup")),
               str(r.get("weekly_status")))
        if key in seen:
            continue
        seen.add(key)
        print(f"{str(r.get('frequency'))[:9]:<10} "
              f"{str(r.get('gender')):<8} {str(r.get('sector')):<14} "
              f"{str(r.get('AgeGroup'))[:21]:<22} {str(r.get('year')):<6} "
              f"{str(r.get('month'))[:9]:<10} {str(r.get('weekly_status')):<7} "
              f"{r.get('value')}")

    print()
    print("=" * 100)
    print("All India, gender=person rows (ordered newest first)")
    print("=" * 100)
    per = [r for r in rows if str(r.get("gender", "")).lower() == "person"]
    def sort_key(r):
        return (str(r.get("year") or ""), str(r.get("month") or ""))
    for r in sorted(per, key=sort_key, reverse=True)[:20]:
        print(f"  {r.get('frequency'):<9} {r.get('year')} "
              f"{str(r.get('month'))[:9]:<10} "
              f"AgeGroup={str(r.get('AgeGroup'))[:20]:<21} "
              f"sector={str(r.get('sector')):<14} "
              f"status={str(r.get('weekly_status')):<7} -> {r.get('value')}")

    print()
    print("=" * 100)
    print("CONCLUSION")
    print("=" * 100)
    print("Pick the row combination above whose value is closest to India's")
    print("published UR (~4-6% for the all-India person rate). Do not assume")
    print("a combination exists - verify it from this list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())