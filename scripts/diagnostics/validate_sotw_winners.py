"""Validate the specific SOTW indicators worth wiring into the engine.

The first dump compared annual USD/percent series against monthly primary
values, which produced nonsense ratios (30673%) - that was my comparison being
wrong, not the data. This script compares LIKE with LIKE:

  * annual vs annual
  * the year is stated, so the lag is explicit
  * unit is taken from the API's own metadata, not assumed

Winner candidates (from the dump):
  IMF.GGXWDG_NGDP    Govt gross debt % of GDP      83.4  2026   <- fills 'debt'
  IMF.NGDP_RPCH      Real GDP growth %              6.5  2026   <- annual GDP
  IMF.PCPIPCH        Inflation, consumer prices %   4.7  2026   <- annual CPI

These are IMF WEO projections for calendar 2026, published in 2026. That is a
fundamentally different thing from the MoSPI Q1-FY27 print (8.2%) or the Aug
2026 CPI (2.5%), and the distinction has to survive into the UI.

Usage:
    openalgo\\Scripts\\python.exe Scripts\\validate_sotw_winners.py <key>
"""

from __future__ import annotations

import json
import sys
import urllib.request

KEY = sys.argv[1] if len(sys.argv) > 1 else ""
BASE = "https://statisticsoftheworld.com/api/v1"

WINNERS = [
    ("debt", "IMF.GGXWDG_NGDP", 81.92, "RBI/IMF general govt debt/GDP"),
    ("gdp_growth_annual", "IMF.NGDP_RPCH", 8.2, "MoSPI Q1 FY27 (quarterly)"),
    ("inflation_annual", "IMF.PCPIPCH", 2.5, "MoSPI CPI Aug 2026 (monthly)"),
    ("fiscal_balance", "IMF.GGXCNL_NGDP", None, "no primary comparable"),
    ("current_account", "IMF.BCA_NGDPD", None, "no primary comparable"),
]


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

    print("=" * 98)
    print("Validating SOTW indicators for engine use (like-for-like)")
    print("=" * 98)
    print(f"{'signal':<20} {'indicator':<18} {'value':>10} {'yr':>5} "
          f"{'unit':<14} {'source':<26}")
    print("-" * 98)

    ok = 0
    for signal, iid, truth, tsrc in WINNERS:
        try:
            d = get(f"/indicators/{iid}?country=IND")
        except Exception as exc:  # noqa: BLE001
            print(f"{signal:<20} {iid:<18} FETCH FAILED {exc!r}")
            continue

        ind = d.get("indicator") or {}
        # The per-country payload nests the value under data[0].
        rows = d.get("data") or []
        row = rows[0] if rows else {}
        val = row.get("value")
        yr = row.get("year", "-")
        # Prefer the API's declared unit over anything we assume.
        unit = ind.get("unit") or ind.get("format") or "?"
        src = ind.get("sourceName") or ind.get("source") or "?"

        print(f"{signal:<20} {iid:<18} {str(val):>10} {str(yr):>5} "
              f"{str(unit)[:13]:<14} {str(src)[:25]:<26}")
        if val is not None:
            ok += 1
            if truth is not None:
                print(f"{'':<20} label: {ind.get('label')}")
                print(f"{'':<20} vs primary: {truth}  [{tsrc}]")

    print("-" * 98)
    print(f"resolved {ok}/{len(WINNERS)} indicators")
    print()
    print("=" * 98)
    print("DECISION SUMMARY")
    print("=" * 98)
    print("  debt              -> USE SOTW (IMF.GGXWDG_NGDP). Primary source was")
    print("                       itself IMF-derived; this is the same series,")
    print("                       now live instead of hand-entered.")
    print("  gdp_growth        -> DO NOT replace. Keep MoSPI Q1 FY27 (8.2%) as")
    print("                       the signal; IMF's 6.5% is a calendar-year")
    print("                       projection. Optionally record BOTH and label.")
    print("  inflation         -> DO NOT replace. MoSPI Aug-2026 monthly (2.5%)")
    print("                       is current; IMF 4.7% is a 2026 annual forecast.")
    print("  fiscal_balance    -> NEW signal available (IMF.GGXCNL_NGDP).")
    print("  current_account   -> NEW signal available (IMF.BCA_NGDPD).")
    print()
    print("  Still UNSOURCED after this: 'climate' and 'tax'.")
    print("  'tax' has a candidate: GC.TAX.TOTL.GD.ZS (tax revenue % GDP),")
    print("  but it is FY2022 - too stale to score, and it measures tax")
    print("  REVENUE, not the effective corporate tax rate the comment implied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())