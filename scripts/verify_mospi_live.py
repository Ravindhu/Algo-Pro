"""Verify MospiProvider against the LIVE MoSPI server.

Complements test_mospi_provider.py (which is offline/synthetic). This one makes
real network calls and asserts the returned values are plausible for India,
so a schema change that still returns HTTP 200 is caught.

Run:
    openalgo\\Scripts\\python.exe Scripts\\verify_mospi_live.py
"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "Blue_engine"))

# Plausible bands for India. A value outside these means the payload shape
# changed or the wrong series was selected -- not a market move.
BANDS = {
    "inflation": (0.0, 15.0, "CPI YoY %"),
    "gdp_growth": (2.0, 12.0, "real GDP YoY %"),
    "employment": (2.0, 12.0, "unemployment rate %"),
}


def main() -> int:
    from blue_engine.economics.providers.mospi_provider import MospiProvider

    print("=" * 84)
    print("MospiProvider - LIVE verification")
    print("=" * 84)

    prov = MospiProvider({}, log=lambda m: print(f"  log: {m}"))
    out = prov.fetch()

    failures = []
    for key, (lo, hi, label) in BANDS.items():
        block = out.get(key)
        print(f"\n{key}  ({label})")
        if not block:
            print("   NOT RETURNED")
            failures.append(f"{key} missing")
            continue
        val = block.get("value")
        print(f"   value  : {val}")
        print(f"   as_of  : {block.get('as_of')}")
        print(f"   source : {block.get('source')}")
        if val is None:
            failures.append(f"{key} value is None")
        elif not (lo <= float(val) <= hi):
            failures.append(f"{key}={val} outside plausible {lo}-{hi}")
            print(f"   !! OUTSIDE plausible band {lo}-{hi}")
        else:
            print(f"   OK (within {lo}-{hi})")

    meta = out.get("meta") or {}
    print(f"\nmeta.ok       : {meta.get('ok')}")
    print(f"meta.warnings : {meta.get('warnings')}")

    print()
    print("=" * 84)
    if failures:
        print(f"FAILURES ({len(failures)}): {failures}")
        return 1
    print("ALL LIVE VALUES VERIFIED")
    print()
    print("Cross-check against the primary release before trusting:")
    print("  inflation   MoSPI CPI press release, All-India Combined")
    print("  gdp_growth  MoSPI NAS press note (Current series)")
    print("  employment  MoSPI PLFS bulletin")
    return 0


if __name__ == "__main__":
    sys.exit(main())