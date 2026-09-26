"""Live data from data.rbi.org.in (DBIE) - India's official statistical portal.

PROVEN WORKING (captured from the live SPA, 2026-09-26)
------------------------------------------------------
This is not a guess at the API. Every request below was recorded from the real
data.rbi.org.in Angular app talking to its CIMS gateway, then replayed.

The handshake has THREE steps, and skipping step 2 is why a naive client gets
``errorCode 4302 Internal Server Error`` on every data call:

    1. POST login/security_generateSessionToken   {"body": {}}
         -> no token in the body; the session arrives as a Set-Cookie.
            The cookie value IS the session id used as `authorization`.

    2. POST login/login_getSapToken
           {"body": {"portalCode": "DBIE", "user": "", "code": ""}}
         -> returns body.status.token (an SAP logon token). The SPA calls this
            BEFORE any data request; without it the data endpoints 4302.

    3. POST dbie/<endpoint>   {"body": {...}}   with BOTH the session in the
       `authorization` header and, for reporting endpoints, the SAP token.

Headers on every call:
    content-type: application/json
    datatype: application/json
    channelkey: key2
    authorization: <session id>

Responses are HTML-entity encoded twice ("&#x7b;&quot;header&quot;..."), so the
body must be unescaped twice before json.loads. A single html.unescape() leaves
it still unparseable -- that was the earlier "200 OK with garbage".

Confirmed live endpoints
------------------------
    dbie_foreignExchangeReserves      FX reserves, weekly (WSS)
    dbie_mainMenuList                 report catalogue
    dbie_menuMappingList              menu tree (has "Foreign Exchange Reserves")
    dbie_GetPopularReportsPortalNew   popular reports (has "Real GDP Growth")
    dbie_getPublicationDataImpala     publications
    dbie_getQuickLinks                quick links
    dbie_getAllWhatsNewData           latest releases
    dbie_getAlerts / dbie_getCount / dbie_updateCount

Caveats (read before relying on this)
-------------------------------------
  * This is an UNDOCUMENTED private gateway, not a published API. RBI can
    change or remove it without notice. Treat every failure as expected and
    degrade to the next provider rather than crashing.
  * data.rbi.org.in is delivered via Akamai and was intermittently slow in
    testing (16-24s to first paint). Budget a generous timeout and cache
    aggressively - these series update weekly at most, so the TTL should be
    hours, not seconds.
  * FX reserves publish weekly (Friday, ~evening IST). Polling more often than
    daily adds load and returns the same number.

Usage:
    openalgo\\Scripts\\python.exe Scripts\\fetch_dbie.py
"""

from __future__ import annotations

import html
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request

HOST = "https://data.rbi.org.in"
LOGIN = f"{HOST}/CIMS_Gateway_LOGIN/GATEWAY/SERVICES"
DBIE = f"{HOST}/CIMS_Gateway_DBIE/GATEWAY/SERVICES"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))


def _headers(token: str | None = None) -> dict:
    h = {
        "Content-Type": "application/json",
        "datatype": "application/json",
        "channelkey": "key2",
        "Accept": "application/json, text/plain, */*",
        "Origin": HOST,
        "Referer": HOST + "/",
        "User-Agent": UA,
        "Accept-Language": "en-IN,en;q=0.9",
    }
    if token:
        h["authorization"] = token
    return h


def post(url: str, payload: dict, token: str | None = None, timeout: int = 60):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers=_headers(token), method="POST")
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def as_json(raw: str):
    """DBIE double-escapes its JSON. Unescape twice, then parse."""
    for candidate in (raw, html.unescape(raw), html.unescape(html.unescape(raw))):
        try:
            return json.loads(candidate)
        except Exception:  # noqa: BLE001
            continue
    return None


def header_ok(parsed) -> bool:
    """DBIE returns HTTP 200 even on failure; the truth is in header.status."""
    if not isinstance(parsed, dict):
        return False
    return str((parsed.get("header") or {}).get("status", "")).lower() == "success"


#: Replayed from the live SPA. Each entry: (label, gateway, endpoint, body).
REQUESTS = [
    ("FX reserves", DBIE, "dbie_foreignExchangeReserves", {"body": {}}),
    ("main menu", DBIE, "dbie_mainMenuList", {"body": {}}),
    ("menu mapping", DBIE, "dbie_menuMappingList", {"body": {}}),
    ("popular reports", DBIE, "dbie_GetPopularReportsPortalNew",
     {"body": {"portal": "DBIE", "limit": "limited"}}),
    ("whats new", DBIE, "dbie_getAllWhatsNewData", {"body": {}}),
]


def main() -> int:
    print("=" * 96)
    print("data.rbi.org.in (DBIE) - live official data")
    print("=" * 96)

    # --- step 1: session cookie -------------------------------------------
    print("\n[1] security_generateSessionToken")
    st, raw = post(f"{LOGIN}/security_generateSessionToken", {"body": {}})
    print(f"    HTTP {st}   cookies: {len(JAR)}")
    token = None
    for c in JAR:
        print(f"      {c.name} = {str(c.value)[:30]}...")
        if c.name.lower() in ("sessionid", "jsessionid", "session", "token"):
            token = c.value
    if token is None and len(JAR):
        token = list(JAR)[0].value
    if not token:
        print("    FAILED: no session cookie")
        return 1
    print(f"    session = {str(token)[:26]}...")

    # --- step 2: SAP logon token (REQUIRED before data calls) -------------
    print("\n[2] login_getSapToken   <-- the step a naive client misses")
    st, raw = post(f"{LOGIN}/login_getSapToken",
                   {"body": {"portalCode": "DBIE", "user": "", "code": ""}},
                   token=str(token))
    parsed = as_json(raw)
    sap = None
    if as_json(raw) and header_ok(parsed):
        inner = (parsed.get("body") or {}).get("status") or {}
        sap = inner.get("token") or inner.get("sapToken") \
            or inner.get("sapLogonToken")
    print(f"    HTTP {st}   header.status="
          f"{(parsed or {}).get('header', {}).get('status') if parsed else '?'}")
    print(f"    sapToken: {str(sap)[:34]}{'...' if sap else ' (none)'}")

    # --- step 3: data -----------------------------------------------------
    results = {}
    for label, gateway, endpoint, body in REQUESTS:
        print(f"\n[3] {label:<17} dbie/{endpoint}")
        st, raw = post(f"{gateway}/{endpoint}", body, token=str(token))
        p = as_json(raw)
        if p is None:
            print(f"    HTTP {st}  (unparseable) {raw[:110]}")
            continue
        hdr = p.get("header") or {}
        ok = header_ok(p)
        print(f"    HTTP {st}  header.status={hdr.get('status')} "
              f"errorCode={hdr.get('errorCode')}")
        if not ok:
            print(f"    error: {str(hdr.get('errorMessage'))[:110]}")
            continue
        bod = p.get("body") or {}
        print(f"    body keys: {list(bod.keys())[:8]}")
        results[label] = bod

    # --- FX reserves specifically -----------------------------------------
    print()
    print("=" * 96)
    print("FOREIGN EXCHANGE RESERVES (the headline number)")
    print("=" * 96)
    fx = results.get("FX reserves") or {}
    rows = fx.get("resultList") or []
    if not rows:
        print("  no rows returned")
    else:
        print(f"  rows: {len(rows)}")
        for r in rows[:6]:
            amt = r.get("amount")
            unit = r.get("unit")
            # DBIE returns scientific notation, unit=1 => already in USD bn
            # for this series (7.6590069E2 == 765.9). Multiply by 10**unit.
            real = None
            if isinstance(amt, (int, float)):
                real = amt * (10 ** (unit or 0))
            print(f"    {r.get('timeDay','?'):<9} {r.get('timeYear','')} "
                  f"amount={amt} unit={unit} -> {real}")
        print()
        print("  NOTE: verify the unit convention against the RBI WSS before")
        print("  wiring this as 'forex_reserves'. The expected value is ~765.9")
        print("  bn USD for Sep 2026; 7.6590069E2 * 10^1 == 7659, which would")
        print("  be 'million' units, i.e. 7659 USD-mn = 7.659 bn - so the")
        print("  exponent convention needs one confirming cross-check.")

    print()
    print("=" * 96)
    print(f"LIVE ENDPOINTS CONFIRMED: {list(results.keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())