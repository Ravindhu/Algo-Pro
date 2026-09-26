"""Audit every macro value the economic engine consumes against real sources.

Answers: "what does the algo actually get, and is it right?"

Prints a per-signal table with the ingested value, the verified real-world
value, the delta, and the source. Values verified 2026-09-26 against
primary/official releases (RBI, MoSPI, IMF).

Usage (from my-trading-system/):
    openalgo\\Scripts\\python.exe Scripts\\audit_macro_values.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "Blue_engine"))

# VERIFIED ground truth, re-checked 2026-09-26 against MoSPI's own endpoint.
# Note: the earlier `inflation 2.5` and `gdp 8.2` in this audit were WRONG --
# they came from secondary sources. MoSPI's primary data says 4.82 and 7.82.
# This table is the authority, so it was corrected rather than the config.
VERIFIED = {
    "gdp_growth": (7.82, "MoSPI NAS real GDP YoY (computed)", "Q1 FY27"),
    "bond_return": (6.68, "FBIL/CCIL 10Y G-Sec benchmark", "Sep 2026"),
    "debt": (81.92, "RBI/IMF general govt debt/GDP", "FY25 actual"),
    "employment": (4.8, "MoSPI PLFS, person, rural+urban", "Dec 2025"),
    "inflation": (4.82, "MoSPI CPI Combined (2024 base)", "Aug 2026"),
    "currency": (95.8918, "FBIL reference rate via RBI", "25-Sep-2026"),
    "forex_reserves": (765.9, "RBI Weekly Statistical Supplement", "Sep 2026"),
    "climate": (None, "UNSOURCED placeholder (weight 0)", "-"),
    "tax": (None, "UNSOURCED placeholder (weight 0)", "-"),
}

#: Signals served by a live provider on this host. Anything else is the
#: static fallback and will drift until re-verified.
LIVE_SIGNALS = {
    "currency": "RbiReferenceRateProvider (FBIL via rbi.org.in)",
    "inflation": "MospiProvider (official MoSPI MCP)",
    "gdp_growth": "MospiProvider (official MoSPI MCP)",
    "employment": "MospiProvider (official MoSPI MCP)",
}

# What the console showed the user BEFORE this audit, for the record.
WAS = {
    "gdp_growth": 6.8, "bond_return": 7.1, "debt": 82.0, "employment": 7.6,
    "inflation": 5.1, "currency": 84.5, "forex_reserves": 690.0,
    "climate": 42.0, "tax": 27.0,
}


def main() -> int:
    cfg = json.loads(
        (ROOT / "Blue_engine" / "input" / "macro_master.json")
        .read_text(encoding="utf-8"))
    macro = cfg["engines"]["economic"]["macro"]

    print("=" * 100)
    print("MACRO VALUE AUDIT - openalgo/blue_engine economic engine")
    print("audited 2026-09-26")
    print("=" * 100)
    print(f"{'key':<16} {'was':>9} {'now':>10} {'verified':>10} {'delta':>8}  status")
    print("-" * 100)

    problems = 0
    for key, (truth, src, period) in VERIFIED.items():
        block = macro.get(key) or {}
        got = block.get("value")
        was = WAS.get(key)
        if truth is None:
            print(f"{key:<16} {str(was):>9} {str(got):>10} {'n/a':>10} {'-':>8}"
                  f"  UNSOURCED ({src})")
            continue
        delta = (got - truth) if isinstance(got, (int, float)) else None
        if delta is None:
            status = "MISSING"
            problems += 1
        elif abs(delta) < 0.01:
            status = "OK"
        elif abs(delta) < 0.5:
            status = f"CLOSE (off {delta:+.2f})"
        else:
            status = f"WRONG (off {delta:+.2f})"
            problems += 1
        print(f"{key:<16} {str(was):>9} {str(got):>10} {truth:>10} "
              f"{('n/a' if delta is None else f'{delta:+.2f}'):>8}  {status}")
        print(f"{'':<16} {'':>9} source: {src}  [{period}]")

    print("-" * 100)
    print(f"mismatches: {problems}")
    print()
    print("=" * 100)
    print("LIVE vs STATIC")
    print("=" * 100)
    print("LIVE (fetched automatically, provenance shown on the dashboard):")
    for sig, src in LIVE_SIGNALS.items():
        print(f"   {sig:<16} {src}")
    print()
    static = [k for k in VERIFIED
              if k not in LIVE_SIGNALS and VERIFIED[k][0] is not None]
    print("STATIC (no scriptable source found; dated + labelled in the UI):")
    for sig in static:
        print(f"   {sig:<16} see 'src' in macro_master.json for why")
    print()
    print("NOT SCORED (weight 0 - no citable source exists):")
    print("   climate, tax")
    print()
    print("Ruled out as sources, with reasons:")
    print("   data.rbi.org.in  SAP token needs a browser fingerprint; refuses")
    print("                    scripts (errorCode 4302)")
    print("   RBI WSS pages    HTTP 200 but only a 3.4KB JS template")
    print("   FBIL / CCIL      render via JS; no data endpoint")
    print("   SOTW             `?country=IND` is silently ignored -- it returns")
    print("                    world-rank #1 (Venezuela 387% inflation), not India")
    print("   NSE, bond        allIndices exposes only price INDEX levels")
    print("                    (NIFTY GS 10YR = 2644, not a ~6.68% yield)")
    print("   FRED             unreachable from this host")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())