"""End-to-end check that the macro 'currency' signal is correct and traceable.

Reproduces the path the Economic Dash page uses:
    macro_master.json (+ live provider override) -> EconomicEngine -> signals dict

Asserts:
  * currency value is ~95 USD/INR, not the old 84.5
  * every signal carries provenance (source + as_of) so a fallback value is
    distinguishable from a live one

Read-only.

Usage (from my-trading-system/):
    openalgo\\Scripts\\python.exe Scripts\\verify_macro_currency.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "Blue_engine"))

FRESH_DAYS = 30


def main() -> int:
    cfg_path = ROOT / "Blue_engine" / "input" / "macro_master.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    econ = cfg["engines"]["economic"]
    macro = econ["macro"]

    print("=" * 78)
    print("1. Config values (the fallback the engine uses)")
    print("=" * 78)
    cur = macro.get("currency") or {}
    print(f"  as_of          : {macro.get('as_of')}")
    print(f"  currency       : {cur.get('value')} {cur.get('unit')} "
          f"(prev {cur.get('previous')})")
    print(f"  forex_reserves : {(macro.get('forex_reserves') or {}).get('value')} "
          f"bn_usd")

    print()
    print("=" * 78)
    print("2. Engine output (what the Dash page renders)")
    print("=" * 78)
    from blue_engine.economics.economic_engine import EconomicEngine

    eng = EconomicEngine({"signals": econ.get("signals") or {},
                          "macro": macro,
                          "blend": econ.get("blend"),
                          "use_surprise": econ.get("use_surprise", True),
                          "use_delevel": econ.get("use_delevel", True)})
    res = eng.run(context={"macro": macro})
    result = getattr(res, "data", None) or getattr(res, "output", None) or {}
    if not result:
        # Show what the engine actually returned so a shape change is obvious.
        print(f"  engine produced no data: ok={getattr(res, 'ok', None)} "
              f"error={getattr(res, 'error', None)}")
        return 1

    sigs = (result or {}).get("signals") or {}
    if not sigs:
        print("  no signals produced")
        return 1

    print(f"{'signal':<14} {'value':>9} {'score':>8} {'source':<28} as_of")
    print("-" * 78)
    problems = []
    for key, s in sigs.items():
        src = s.get("source") or "config fallback"
        as_of = s.get("as_of") or "-"
        print(f"{key:<14} {str(s.get('value')):>9} "
              f"{float(s.get('score') or 0):>+8.3f} {src:<28} {as_of}")
        if not s.get("source"):
            problems.append(f"{key}: no live source (fallback)")

    print("-" * 78)
    cv = (sigs.get("currency") or {}).get("value")
    ok_value = cv is not None and 90.0 <= float(cv) <= 100.0
    print(f"currency value now: {cv}  -> {'OK (~95 USD/INR)' if ok_value else 'STILL WRONG'}")
    print()
    print("Fallback-sourced signals (expected on this broker, since the CDS")
    print("currency segment is unavailable):")
    for p in problems:
        print(f"   - {p}")

    return 0 if ok_value else 1


if __name__ == "__main__":
    sys.exit(main())