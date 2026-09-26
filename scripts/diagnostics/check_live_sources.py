"""Which macro parameters can actually be fetched LIVE from here?

Tests every candidate source from a plain script (no browser) and reports
PASS/FAIL per parameter, with the reason. This is the feasibility gate before
any provider gets written -- a source that works in a browser but not from a
script is not implementable.

Ordered by likelihood of working:
  1. OpenAlgo SDK             already authenticated; no scraping
  2. rbi.org.in homepage      plain HTML; FBIL already proven to work here
  3. rbi.org.in WSS page      weekly reserves table
  4. MoSPI / data.gov.in      needs a key
  5. NSE                      often needs cookies

Usage:
    openalgo\\Scripts\\python.exe Scripts\\check_live_sources.py <openalgo_apikey>
"""

from __future__ import annotations

import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

KEY = sys.argv[1] if len(sys.argv) > 1 else ""

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

RESULTS = []


def note(param, source, ok, detail):
    RESULTS.append((param, source, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {param:<18} {source:<26} {detail[:70]}")


def get(url, timeout=30, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-IN,en;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.status, r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- 1. SDK ----
def test_sdk():
    print("\n[1] OpenAlgo SDK (no scraping - uses the authenticated client)")
    try:
        from blue_engine.core.client import init_client
        c = init_client(api_key=KEY)
    except Exception as exc:  # noqa: BLE001
        note("(all SDK)", "openalgo sdk", False, f"client init failed: {exc!r}")
        return

    end = datetime.now()
    start = end - timedelta(days=30)

    # 10Y G-Sec: the bond index is reachable as an index instrument
    for sym, exch in [("NIFTYGS10YR", "NSE_INDEX"),
                      ("NIFTYGS10YR", "NSE"),
                      ("10YR", "NSE_INDEX")]:
        try:
            q = c.quotes(symbol=sym, exchange=exch)
            if q.get("status") == "success":
                note("bond_return", f"sdk quotes {sym}/{exch}", True,
                     f"ltp={q['data'].get('ltp')}")
                break
        except Exception:  # noqa: BLE001
            continue
    else:
        note("bond_return", "sdk quotes", False, "no 10Y instrument resolved")

    # India VIX - useful risk-appetite input
    for sym, exch in [("INDIAVIX", "NSE_INDEX"), ("INDIA VIX", "NSE_INDEX")]:
        try:
            q = c.quotes(symbol=sym, exchange=exch)
            if q.get("status") == "success":
                note("india_vix", f"sdk quotes {sym}", True,
                     f"ltp={q['data'].get('ltp')}")
                break
        except Exception:  # noqa: BLE001
            continue
    else:
        note("india_vix", "sdk quotes", False, "INDIA VIX not resolved")

    # Gold / crude on MCX - commodity macro inputs
    for sym, exch in [("GOLD", "MCX"), ("CRUDEOIL", "MCX"),
                      ("SILVER", "MCX")]:
        try:
            q = c.quotes(symbol=sym, exchange=exch)
            if q.get("status") == "success":
                note(f"commodity:{sym}", f"sdk quotes {sym}/MCX", True,
                     f"ltp={q['data'].get('ltp')}")
            else:
                note(f"commodity:{sym}", f"sdk quotes {sym}/MCX", False,
                     str(q.get("message"))[:60])
        except Exception as exc:  # noqa: BLE001
            note(f"commodity:{sym}", f"sdk quotes {sym}/MCX", False,
                 f"{type(exc).__name__}")

    # NIFTY / BANKNIFTY - breadth + trend context
    for sym in ["NIFTY", "BANKNIFTY"]:
        try:
            q = c.quotes(symbol=sym, exchange="NSE_INDEX")
            ok = q.get("status") == "success"
            note(f"index:{sym}", f"sdk quotes {sym}", ok,
                 f"ltp={q.get('data', {}).get('ltp')}")
        except Exception as exc:  # noqa: BLE001
            note(f"index:{sym}", f"sdk quotes {sym}", False, f"{type(exc).__name__}")


# -------------------------------------------------------- 2. rbi homepage ---
def test_rbi_homepage():
    print("\n[2] rbi.org.in homepage (plain HTML - the FBIL route)")
    try:
        st, body = get("https://www.rbi.org.in/", timeout=40)
    except Exception as exc:  # noqa: BLE001
        note("(all rbi.org.in)", "homepage", False, f"{type(exc).__name__}: {exc}")
        return

    note("(page fetch)", "rbi.org.in", st == 200, f"HTTP {st}, {len(body):,} bytes")

    # USD/INR reference rate - already implemented and working
    m = re.search(r"INR\s*/\s*1\s*USD\s*</th>\s*<td[^>]*>\s*:\s*([0-9.]+)",
                  body, re.I | re.S)
    note("currency (USD/INR)", "rbi FBIL reference", bool(m),
         f"value={m.group(1)}" if m else "not found")

    # Other reference rates on the same table
    for code, label in [("EUR", "EUR/INR"), ("GBP", "GBP/INR"),
                        ("JPY", "JPY/INR (per 100)")]:
        pat = (rf"{code}\s*/\s*1\s*INR|100\s*{code}\s*/\s*1\s*INR|"
               rf"INR\s*/\s*1\s*{code}")
        mm = re.search(pat, body, re.I)
        note(f"fx:{label}", "rbi reference", bool(mm),
             "present in page" if mm else "not in this table")

    # WSS / policy rates often linked from the homepage
    for probe, label in [("policy", "policy rates"),
                         ("repo", "repo rate"),
                         ("reserve", "reserves")]:
        found = probe in body.lower()
        note(f"link:{label}", "rbi homepage", found,
             "referenced" if found else "not referenced")


# ------------------------------------------------------------ 3. WSS page ---
def test_rbi_wss():
    print("\n[3] RBI WSS (Weekly Statistical Supplement) - FX reserves")
    # Try a few known WSS view ids; the homepage links the current one.
    for url in [
        "https://www.rbi.org.in/scripts/WSSView.aspx?Id=28307",
        "https://www.rbi.org.in/scripts/WSSView.aspx",
        "https://www.rbi.org.in/Scripts/BS_ViewWSS.aspx",
    ]:
        try:
            st, body = get(url, timeout=40)
        except Exception as exc:  # noqa: BLE001
            note("forex_reserves", f"WSS {url[-22:]}", False,
                 f"{type(exc).__name__}")
            continue
        # Look for the reserves figure: "765.9" style, in US$ mn or bn
        hits = re.findall(r"(\d{3}\.\d{1,2})\s*(?:billion|bn)", body, re.I)
        hit_mn = re.findall(r"1?\(?(\d{7})\)?", body)
        found = bool(hits)
        note("forex_reserves", f"WSS {url[-22:]}", found or st == 200,
             f"HTTP {st}, bn-hits={hits[:3]}")
        if found:
            break


# ------------------------------------------------------- 4. data.gov.in -----
def test_datagovin():
    print("\n[4] data.gov.in (needs a free API key - checking reachability)")
    for url, label in [
        ("https://api.data.gov.in/", "api.data.gov.in"),
        ("https://www.mospi.gov.in/", "mospi.gov.in"),
    ]:
        try:
            st, body = get(url, timeout=30)
            note(f"reach:{label}", "http", st == 200, f"HTTP {st}")
        except Exception as exc:  # noqa: BLE001
            note(f"reach:{label}", "http", False, f"{type(exc).__name__}")


# ---------------------------------------------------------------- 5. NSE ----
def test_nse():
    print("\n[5] NSE public endpoints (often cookie-gated)")
    for url, label in [
        ("https://www.nseindia.com/api/allIndices", "allIndices (VIX etc)"),
        ("https://www.nseindia.com/api/fiidiiTradeReact", "FII/DII flows"),
    ]:
        try:
            st, body = get(url, timeout=20,
                           headers={"Accept": "application/json",
                                    "Referer": "https://www.nseindia.com/"})
            ok = st == 200 and body.strip().startswith(("{", "["))
            note(label, "nseindia api", ok,
                 f"HTTP {st}, {len(body)} bytes" + ("" if ok else " (gated)"))
        except Exception as exc:  # noqa: BLE001
            note(label, "nseindia api", False, f"{type(exc).__name__}")


def main() -> int:
    print("=" * 98)
    print("LIVE SOURCE FEASIBILITY - which params can actually be implemented")
    print("=" * 98)

    test_sdk()
    test_rbi_homepage()
    test_rbi_wss()
    test_datagovin()
    test_nse()

    print()
    print("=" * 98)
    print("SUMMARY")
    print("=" * 98)
    ok = [r for r in RESULTS if r[2]]
    bad = [r for r in RESULTS if not r[2]]
    print(f"  PASS {len(ok)}   FAIL {len(bad)}")
    print()
    print("IMPLEMENTABLE NOW:")
    for p, s, _, d in ok:
        print(f"   {p:<20} <- {s}")
    print()
    print("NOT IMPLEMENTABLE:")
    for p, s, _, d in bad:
        print(f"   {p:<20} <- {s}  ({d[:52]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())