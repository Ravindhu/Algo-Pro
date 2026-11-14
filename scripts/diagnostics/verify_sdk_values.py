"""Verify the two SDK values that look wrong before trusting them.

Two red flags from the resolution test:

  1. `bond_return` -> NIFTYGS10YR quotes 2647.02. That is a bond INDEX level,
     not a yield. The macro signal needs a percentage (~6.5%), so quoting the
     index and scoring it as a yield would be a category error.

  2. `india_vix` -> ltp=38.88 against prev=12.16. A 3x move in one session is
     not credible for India VIX; 12 is the normal range. Either the field is
     mismatched or the quote is stale.

This pulls the FULL quote payload for both so the correct field can be chosen
from evidence.

Run:
    openalgo\\Scripts\\python.exe Scripts\\verify_sdk_values.py <apikey>
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

KEY = sys.argv[1] if len(sys.argv) > 1 else ""


def main() -> int:
    from blue_engine.core.client import init_client

    c = init_client(api_key=KEY)

    for label, sym, exch in [
        ("10Y G-Sec index", "NIFTYGS10YR", "NSE_INDEX"),
        ("India VIX", "INDIAVIX", "NSE_INDEX"),
        ("NIFTY", "NIFTY", "NSE_INDEX"),
    ]:
        print("=" * 96)
        print(f"{label}  ({sym}/{exch})")
        print("=" * 96)
        try:
            q = c.quotes(symbol=sym, exchange=exch)
        except Exception as exc:  # noqa: BLE001
            print(f"  EXC {type(exc).__name__}: {exc}")
            continue
        print(json.dumps(q, indent=2, default=str)[:1400])
        print()

    print("=" * 96)
    print("Search the contract master for a real 10Y YIELD instrument")
    print("=" * 96)
    for term in ("NIFTYGS10YR", "GS10", "10YR", "INDIAVIX", "VIX"):
        try:
            s = c.search(query=term)
            rows = s.get("data") or []
            print(f"\nsearch {term!r}: {len(rows)} hits")
            for r in rows[:8]:
                print(f"   exch={r.get('exchange'):<10} "
                      f"sym={str(r.get('symbol'))[:34]:<36} "
                      f"name={str(r.get('name'))[:40]}")
        except Exception as exc:  # noqa: BLE001
            print(f"search {term!r} EXC {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())