"""Verify USD/INR now comes from RBI/FBIL through the full provider stack.

Reproduces the production path:
    build_default_stack(client) -> CompositeProvider -> macro["currency"]

Assertions:
  * `currency` is produced with a value near 95.9 (not the old 84.5)
  * its provenance says RB, not "config fallback"
  * the value carries onto the EconomicEngine's `currency` signal

Usage (from my-trading-system/):
    openalgo\\Scripts\\python.exe Scripts\\verify_rbi_in_stack.py <apikey>
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "Blue_engine"))

KEY = sys.argv[1] if len(sys.argv) > 1 else ""

FAILURES = []


def main() -> int:
    from blue_engine.economics.providers import build_default_stack

    client = None
    if KEY:
        from blue_engine.core.client import init_client
        client = init_client(api_key=KEY)

    print("=" * 78)
    print("1. build_default_stack() -> CompositeProvider")
    print("=" * 78)
    logs = []
    stack = build_default_stack(client=client, config={}, log=logs.append)
    if stack is None:
        print("  stack is None (no providers constructed)")
        return 1

    macro = stack.fetch()
    meta = macro.get("meta") or {}
    print(f"  providers : {list((meta.get('providers') or {}).keys())}")
    print(f"  provenance: {meta.get('provenance')}")
    print(f"  ok={meta.get('ok')} stale={meta.get('stale')}")
    for w in (meta.get("warnings") or [])[:6]:
        print(f"  warn: {str(w)[:95]}")

    print()
    print("=" * 78)
    print("2. The currency block")
    print("=" * 78)
    cur = macro.get("currency")
    if not cur:
        print("  currency: MISSING")
        FAILURES.append("currency missing from composite")
    else:
        print(f"  value  : {cur.get('value')}")
        print(f"  unit   : {cur.get('unit')}")
        print(f"  as_of  : {cur.get('as_of')}")
        print(f"  source : {cur.get('source')}")
        v = float(cur.get("value") or 0)
        if not (90.0 <= v <= 100.0):
            FAILURES.append(f"currency value {v} not near 95")
        if "FBIL" not in str(cur.get("source", "")):
            FAILURES.append("currency not attributed to FBIL")

    print()
    print("=" * 78)
    print("3. Engine consumes it (provenance must survive to the signal)")
    print("=" * 78)
    from blue_engine.economics.economic_engine import EconomicEngine

    eng = EconomicEngine({
        "signals": {"bond_return": {"direction": -1, "weight": 1.0},
                    "currency": {"direction": -1, "weight": 0.5}},
        "use_surprise": True, "use_delevel": True,
    })
    res = eng.run(context={"macro": macro})
    data = getattr(res, "data", None) or {}
    sig = (data.get("signals") or {}).get("currency")
    if not sig:
        print("  currency signal not produced")
        FAILURES.append("engine produced no currency signal")
    else:
        print(f"  value  : {sig.get('value')}")
        print(f"  score  : {sig.get('score')}")
        print(f"  source : {sig.get('source')}")
        print(f"  as_of  : {sig.get('as_of')}")
        if not sig.get("source"):
            FAILURES.append("signal lost its source (renders as 'config fallback')")

    print()
    print("=" * 78)
    if FAILURES:
        print("RESULT: FAILURES:")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("RESULT: ALL PASS - USD/INR now comes from the official FBIL fix")
    return 0


if __name__ == "__main__":
    sys.exit(main())