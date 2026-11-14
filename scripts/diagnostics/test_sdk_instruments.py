"""Test which OpenAlgo SDK instruments resolve, BEFORE the provider is written.

Building a provider on assumed symbols is how you get a feed that 200s and
returns nothing. Every candidate below is checked against the live broker.

Targets:
    bond_return   India 10Y G-Sec  -> the 'bond_return' macro signal
    india_vix     India VIX        -> risk-appetite input
    nifty         NIFTY            -> breadth / regime context
    banknifty     BANKNIFTY

Run:
    openalgo\\Scripts\\python.exe Scripts\\test_sdk_instruments.py <apikey>
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
sys.path.insert(0, os.path.join(ROOT, "src", "quant", "blue_engine"))

KEY = sys.argv[1] if len(sys.argv) > 1 else ""

# (macro key, [candidate (symbol, exchange) pairs in preference order])
CANDIDATES = {
    "bond_return": [
        ("NIFTYGS10YR", "NSE_INDEX"),
        ("NIFTYGS10YR", "NSE"),
        ("NIFTYGS10YRI", "NSE_INDEX"),
        ("GS10YR", "NSE_INDEX"),
        ("10GS2033", "NSE"),
    ],
    "india_vix": [
        ("INDIAVIX", "NSE_INDEX"),
        ("INDIA VIX", "NSE_INDEX"),
        ("INDIAVIX", "NSE"),
    ],
    "nifty": [("NIFTY", "NSE_INDEX"), ("NIFTY50", "NSE_INDEX")],
    "banknifty": [("BANKNIFTY", "NSE_INDEX")],
    "gold": [("GOLD", "MCX"), ("GOLD", "MCX_FO")],
    "crude": [("CRUDEOIL", "MCX"), ("CRUDEOIL", "MCX_FO")],
}


def main() -> int:
    from blue_engine.core.client import init_client

    client = init_client(api_key=KEY)
    print("=" * 96)
    print("OpenAlgo SDK instrument resolution")
    print("=" * 96)

    resolved = {}
    for key, cands in CANDIDATES.items():
        print(f"\n{key}:")
        for sym, exch in cands:
            try:
                q = client.quotes(symbol=sym, exchange=exch)
            except Exception as exc:  # noqa: BLE001
                print(f"  {sym:<14} {exch:<10} EXC {type(exc).__name__}")
                continue
            if q.get("status") == "success":
                d = q.get("data") or {}
                ltp = d.get("ltp")
                print(f"  {sym:<14} {exch:<10} OK   ltp={ltp} "
                      f"prev={d.get('prev_close') or d.get('previous_close')}")
                resolved.setdefault(key, (sym, exch, ltp))
                break
            msg = str(q.get("message"))[:58]
            print(f"  {sym:<14} {exch:<10} no   {msg}")

    print()
    print("=" * 96)
    print("RESOLVED (usable as live macro inputs)")
    print("=" * 96)
    for k, (sym, exch, ltp) in resolved.items():
        print(f"  {k:<14} {sym}/{exch}  ltp={ltp}")
    missing = [k for k in CANDIDATES if k not in resolved]
    print(f"\n  NOT RESOLVED: {missing}")
    return 0 if resolved else 1


if __name__ == "__main__":
    sys.exit(main())