"""Extract India's real values from the Statistics of the World API.

CRITICAL trap this script exists to avoid
-----------------------------------------
``/indicators/<CODE>?country=IND`` returns HTTP 200 and a full payload -- but
the ``country`` parameter is IGNORED. The endpoint is a *world ranking*, and
``data[0]`` is the #1-ranked country, not the country you asked for.

Concretely: for ``IMF.PCPIPCH`` (inflation) that returned 387.4, which is
**Venezuela**. India was nowhere near the top. A provider written straight off
the docs would have fed Venezuela's hyperinflation into the India regime score
and shown "Inflation 387.4%" on the dashboard -- with a 200 OK and no warning.

The correct read is to fetch the full ranking and filter ``countryId == "IND"``
(or use a per-country route if one exists).

Usage:
    openalgo\\Scripts\\python.exe Scripts\\sotw_india_values.py <key>
"""

from __future__ import annotations

import json
import sys
import urllib.request

KEY = sys.argv[1] if len(sys.argv) > 1 else ""
BASE = "https://statisticsoftheworld.com/api/v1"

INDICATORS = [
    ("IMF.PCPIPCH", "inflation (annual)", 2.5, "MoSPI CPI Aug 2026 monthly"),
    ("IMF.NGDP_RPCH", "gdp growth (annual)", 8.2, "MoSPI Q1 FY27 quarterly"),
    ("IMF.GGXWDG_NGDP", "govt debt % GDP", 81.92, "RBI/IMF debt/GDP"),
    ("IMF.BCA_NGDPD", "current account % GDP", None, "no primary comparable"),
    ("IMF.GGXCNL_NGDP", "fiscal balance % GDP", None, "no primary comparable"),
    ("IMF.NGDPD", "GDP current USD bn", None, "no primary comparable"),
    ("IMF.NGDPDPC", "GDP per capita USD", None, "no primary comparable"),
    ("SL.UEM.TOTL.ZS", "unemployment % (WB)", 5.0, "MoSPI PLFS Aug 2026"),
    ("FI.RES.TOTL.CD", "FX reserves USD (WB)", 765.9, "RBI WSS Sep 2026 USD"),
    ("PA.NUS.FCRF", "official FX rate LCU/USD", 95.8918, "FBIL ref 25-Sep-2026"),
]


def get(path: str, timeout: int = 45):
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

    print("=" * 104)
    print("Statistics of the World - INDIA only (filtered from the world ranking)")
    print("=" * 104)
    print(f"{'indicator':<18} {'label':<26} {'value':>12} {'yr':>5} "
          f"{'rank':>5}  vs primary")
    print("-" * 104)

    results = {}
    for code, label, truth, tsrc in INDICATORS:
        try:
            d = get(f"/indicators/{code}")
        except Exception as exc:  # noqa: BLE001
            print(f"{code:<18} {label:<26} FETCH FAILED: {exc!r}"[:100])
            continue

        rows = d.get("data") or []
        meta = d.get("indicator") or {}
        india = next((r for r in rows if r.get("countryId") == "IND"), None)

        if india is None:
            print(f"{code:<18} {label:<26} {'NOT IN RANKING':>12}")
            continue

        val = india.get("value")
        results[code] = india

        cmp = ""
        if truth is not None and isinstance(val, (int, float)):
            if abs(val - truth) < 0.05:
                cmp = f"MATCHES primary ({truth})"
            else:
                cmp = f"primary={truth}  [{tsrc}]"
        print(f"{code:<18} {label:<26} {str(val):>12} "
              f"{str(india.get('year')):>5} {str(india.get('rank')):>5}  {cmp}")

    print("-" * 104)
    print()
    print("=" * 104)
    print(f"India resolved for {len(results)}/{len(INDICATORS)} indicators")
    print("=" * 104)
    print("Top-3 ranked countries per indicator (to show the ranking is global,")
    print("and that data[0] was NEVER India):")
    for code in ("IMF.PCPIPCH", "IMF.NGDP_RPCH"):
        try:
            d = get(f"/indicators/{code}")
            top = (d.get("data") or [])[:3]
            names = ", ".join(f"{r['countryId']}={r['value']}" for r in top)
            print(f"  {code:<18} top3: {names}")
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())