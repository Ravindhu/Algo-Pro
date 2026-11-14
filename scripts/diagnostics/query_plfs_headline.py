"""Reach the PLFS headline row by narrowing the query, not by paging blindly.

The unfiltered PLFS pull returns only 20 rows and `gender=person` never reaches
`sector='rural + urban'` before the page ends. Rather than paging through
everything, pass the dimension filters the server accepts so the response is
already narrowed to the slice we want.

Candidate filter keys (from the earlier rejection message and the row schema):
    indicator_code, frequency_code, year, month_code, gender,
    sector, state, age_group, weekly_status
Key names are uncertain for some dimensions, so each is tried and the ones the
server accepts are reported.

Run:
    openalgo\\Scripts\\python.exe Scripts\\query_plfs_headline.py
"""

from __future__ import annotations

import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "quant", "blue_engine"))

from blue_engine.economics.providers.mospi_provider import (  # noqa: E402
    MospiProvider,
)


def main() -> int:
    p = MospiProvider({}, log=lambda m: print(f"  log: {m}"))

    print("=" * 100)
    print("Trying PLFS dimension filters (key names verified by the server)")
    print("=" * 100)

    # Each entry: (label, extra filters)
    trials = [
        ("gender=person", {"gender": "person"}),
        ("gender=Person", {"gender": "Person"}),
        ("sector=rural + urban", {"sector": "rural + urban"}),
        ("age_group=All ages", {"age_group": "All ages"}),
        ("AgeGroup=All ages", {"AgeGroup": "All ages"}),
        ("weekly_status=PS+SS", {"weekly_status": "PS+SS"}),
        ("month_code=12", {"month_code": "12"}),
        ("year=2025", {"year": "2025"}),
        ("page=2", {"page": "2"}),
        ("offset=20", {"offset": "20"}),
    ]

    base = {"indicator_code": "3", "frequency_code": "3"}
    accepted = {}
    for label, extra in trials:
        out = p._call_tool("get_data", {"dataset": "PLFS",
                                        "filters": {**base, **extra}})
        if isinstance(out, dict) and out.get("valid") is False:
            msg = str(out.get("error") or out)[:110]
            print(f"  [REJECTED] {label:<24} {msg}")
            continue
        rows = []
        if isinstance(out, dict):
            for k in ("records", "data", "rows", "results"):
                if isinstance(out.get(k), list):
                    rows = out[k]
                    break
        print(f"  [ACCEPTED] {label:<24} -> {len(rows)} rows")
        accepted[label] = rows
        for r in rows[:3]:
            print(f"        {json.dumps(r)[:200]}")

    # If a narrowing worked, look for the headline combination.
    print()
    print("=" * 100)
    print("Searching all accepted results for the all-India person headline")
    print("=" * 100)
    pool = [r for rows in accepted.values() for r in rows]
    hits = []
    for r in pool:
        g = str(r.get("gender", "")).lower()
        s = str(r.get("sector", "")).lower()
        a = str(r.get("AgeGroup", "")).lower()
        if g == "person" and s in ("rural + urban", "combined") and a == "all ages":
            hits.append(r)
    if hits:
        for h in hits:
            print(f"  HEADLINE: {h.get('year')} {h.get('month')} -> "
                  f"UR = {h.get('value')}%")
    else:
        print("  not found in this page.")
        print()
        print("  FALLBACK OPTIONS (in order of preference):")
        print("   1. gender=person, sector=rural, AgeGroup='All ages' "
              "-> 3.9% (rural-person, current)")
        print("   2. gender=male,   sector='rural + urban' -> 4.7%")
        print("   3. gender=female, sector='rural + urban' -> 4.9%")
        print("   Averaging male+female rural+urban gives ~4.8%, which is a")
        print("   defensible proxy for the all-person rate and is derived from")
        print("   published rows rather than invented.")

    return 0


if __name__ == "__main__":
    sys.exit(main())