"""Probe data.rbi.org.in (DBIE) for India's official series.

data.rbi.org.in is an Angular SPA, so there is no HTML to scrape. The endpoint
was recovered from the compiled bundle (main.<hash>.js):

    base   : https://data.rbi.org.in/CIMS_Gateway_DBIE/GATEWAY/SERVICES
    method : POST, JSON body {"body": {...}}
    auth   : header `authorization` + `channelkey: key2`

Relevant endpoints found in the bundle:
    dbie_foreignExchangeReserves   <- FX reserves (weekly)
    dbie_getAllDBIEReports         <- report catalogue
    dbie_getElementsDataQuery      <- generic time-series query
    dbie_getEntityDataQuery
    dbie_getFrequencyDataQuery
    dbie_getDimensionDataQuery

This script tries to fetch without a session first (some DBIE endpoints are
open), and reports exactly what each one needs. It does NOT guess at bypassing
auth -- if a valid session token is required, that is reported plainly.

Usage:
    openalgo\\Scripts\\python.exe Scripts\\probe_dbie.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "https://data.rbi.org.in/CIMS_Gateway_DBIE/GATEWAY/SERVICES"
ORIGIN = "https://data.rbi.org.in"

HEADERS = {
    "Content-Type": "application/json",
    "datatype": "application/json",
    "channelkey": "key2",
    "Accept": "application/json, text/plain, */*",
    "Origin": ORIGIN,
    "Referer": ORIGIN + "/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept-Language": "en-IN,en;q=0.9",
}

PROBES = [
    ("FX reserves", "dbie_foreignExchangeReserves", {"body": {}}),
    ("report catalogue", "dbie_getAllDBIEReports", {"body": {"portalName": "DBIE"}}),
    ("main menu", "dbie_mainMenuList", {"body": {"portalName": "DBIE",
                                                 "langCode": "en"}}),
    ("elements query", "dbie_getElementsDataQuery", {"body": {}}),
    ("publication data", "dbie_getPublicationDataImpala",
     {"body": {"portalName": "DBIE"}}),
]


def post(endpoint: str, payload: dict, timeout: int = 40):
    url = f"{BASE}/{endpoint}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:500]
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def main() -> int:
    print("=" * 96)
    print("data.rbi.org.in (DBIE) gateway probe")
    print("=" * 96)
    print(f"base: {BASE}")
    print()

    working = []
    for label, endpoint, payload in PROBES:
        status, body = post(endpoint, payload)
        snippet = body[:200].replace("\n", " ")
        print(f"[{status}] {label:<20} {endpoint}")
        print(f"        {snippet}")
        if status == 200:
            try:
                parsed = json.loads(body)
                keys = list(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__
                print(f"        JSON keys: {keys}")
                working.append((label, endpoint, parsed))
            except Exception:  # noqa: BLE001
                print("        (not JSON)")
        print()

    print("=" * 96)
    if working:
        print(f"OPEN ENDPOINTS: {[w[1] for w in working]}")
        for label, endpoint, parsed in working:
            blob = json.dumps(parsed)[:600]
            print(f"\n--- {label} ({endpoint}) ---")
            print(blob)
    else:
        print("No endpoint returned 200.")
        print()
        print("DIAGNOSIS: DBIE most likely requires a session token.")
        print("The SPA obtains one via:")
        print("  /CIMS_Gateway_LOGIN/GATEWAY/SERVICES/login_getDetails")
        print("and then sends it as the `authorization` header alongside")
        print("`channelkey: key2`. A token comes from an authenticated browser")
        print("session, not from the public API key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())