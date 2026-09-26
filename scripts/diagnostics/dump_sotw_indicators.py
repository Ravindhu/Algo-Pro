"""Dump Statistics of the World's India indicators and judge each one.

The provider design question is NOT "can we fetch it" (we can) but "is this
the right series for a macro-regime signal". SOTW aggregates IMF WEO + World
Bank WDI, which are authoritative but ANNUAL and lag 6-18 months. RBI/MoSPI
publish monthly/quarterly.

So this script maps every SOTW indicator we care about against the
primary-source value already verified, and prints the year so the lag is
visible. A 2024 annual GDP-growth figure is not wrong -- it is simply not the
Q1 FY27 print, and the engine must not present it as current.

Usage:
    openalgo\\Scripts\\python.exe Scripts\\dump_sotw_indicators.py <key>
"""

from __future__ import annotations

import json
import sys
import urllib.request

KEY = sys.argv[1] if len(sys.argv) > 1 else ""
BASE = "https://statisticsoftheworld.com/api/v1"

# Primary-source values verified 2026-09-26 (source in brackets).
VERIFIED = {
    "gdp_growth": (8.2, "MoSPI Q1 FY27 quarterly"),
    "inflation": (2.5, "MoSPI CPI Aug 2026 monthly"),
    "employment": (5.0, "MoSPI PLFS Aug 2026 monthly"),
    "debt": (81.92, "RBI/IMF general govt debt/GDP"),
    "currency": (95.8918, "FBIL reference rate 25-Sep-2026"),
    "forex_reserves": (765.9, "RBI WSS Sep 2026"),
    "bond_return": (6.5, "India 10Y G-Sec Sep 2026"),
}

# Which SOTW indicators might fill each engine signal.
WANTED = {
    "gdp_growth": ["NY.GDP.MKTP.KD.ZG", "NY.GDP.MKTP.KD", "NY.GDP.PCAP.KD"],
    "inflation": ["FP.CPI.TOTL.ZG", "NY.GDP.DEFL.KD.ZG"],
    "employment": ["SL.UEM.TOTL.ZS", "SL.UEM.1524.ZS", "SL.TLF.CACT.ZS"],
    "debt": ["GC.DOD.TOTL.GD.ZS", "GG.GOV.DEBT.GD.ZS"],
    "currency": ["PA.NUS.FCRF", "PX.REX.REER"],
    "forex_reserves": ["FI.RES.TOTL.CD", "FI.RES.TOTL.MO"],
    "bond_return": ["FR.INR.LEND", "FR.INR.RINR", "FR.INR.DPST"],
}


def get(path: str, timeout: int = 40):
    req = urllib.request.Request(
        BASE + path,
        headers={"Authorization": f"Bearer {KEY}", "X-API-Key": KEY,
                 "Accept": "application/json", "User-Agent": "blue-engine/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    if not KEY:
        print("no key")
        return 1

    payload = get("/countries/IND")
    inds = payload.get("indicators") or []
    print("=" * 96)
    print(f"Statistics of the World - India: {len(inds)} indicators returned")
    print("=" * 96)

    by_id = {i.get("id"): i for i in inds}

    print(f"\n{'engine signal':<17} {'SOTW indicator':<22} {'value':>12} "
          f"{'yr':>5}  {'primary source':>10}  verdict")
    print("-" * 96)
    for signal, candidates in WANTED.items():
        truth, tsrc = VERIFIED.get(signal, (None, "-"))
        found = False
        for cid in candidates:
            ind = by_id.get(cid)
            if not ind:
                continue
            found = True
            val = ind.get("value")
            yr = str(ind.get("year", "-"))
            if truth is not None and isinstance(val, (int, float)):
                pct = abs(val - truth) / abs(truth) * 100 if truth else 0
                verdict = (f"differs {pct:.1f}% from primary" if pct > 2
                           else "close to primary")
            else:
                verdict = "no primary comparable"
            print(f"{signal:<17} {cid:<22} {str(val):>12} {yr:>5}  "
                  f"{truth!s:>10}  {verdict}")
        if not found:
            print(f"{signal:<17} {'(none of the candidates present)':<22} "
                  f"{'-':>12} {'-':>5}  {truth!s:>10}  NOT AVAILABLE from SOTW")

    # Anything else that looks macro-relevant and might be a better signal.
    print()
    print("=" * 96)
    print("Other Economy / Fiscal & Monetary indicators available")
    print("=" * 96)
    for i in inds:
        cat = i.get("category") or ""
        if cat in ("Economy", "Fiscal & Monetary", "Currencies",
                   "Commodities", "Financial Markets"):
            print(f"  {i.get('id'):<26} {str(i.get('value')):>14} "
                  f"{str(i.get('year')):>6}  {i.get('label','')[:44]}")

    print()
    print("=" * 96)
    print("KEY POINT: check the 'yr' column. IMF/WB annual series that lag by")
    print("a year or more are fine for STRUCTURAL weight but must not be")
    print("presented as the current reading for a signal the engine scores.")
    return 0


if __name__ == "__main__":
    sys.exit(main())