"""Fetch India's official series from data.rbi.org.in (DBIE).

Reverse-engineered from the DBIE Angular bundle. Four gateways exist:

    /CIMS_Gateway_LOGIN/GATEWAY/SERVICES    security_generateSessionToken
    /CIMS_Gateway_DBIE/GATEWAY/SERVICES     the statistical data
    /CIMS_Gateway_RBIDATA/GATEWAY/SERVICES  RBIDATA
    /CIMS_Gateway_SAARC/GATEWAY/SERVICES    SAARC

Flow recovered from the bundle:
    POST login/security_generateSessionToken   {"body": {}}   -> sessionId
    POST dbie/<endpoint>                       {"body": {}}   + authorization: <sessionId>

Headers on every call:
    Content-Type: application/json
    datatype: application/json
    channelkey: key2
    authorization: <sessionId>        (login only, omitted on the token call)

Values come back HTML-entity-encoded ("&quot;"), which is why a naive
json.loads() fails on the raw body - that was the earlier 200-with-garbage.

The token call returns only {"header": {...success...}}. The session itself
arrives as a Set-Cookie, so a JSON-only client gets nothing; this keeps a
cookie jar and reads the value back out.

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


def post(url: str, payload: dict, token: str | None = None, timeout: int = 45):
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
    """Parse a DBIE body, which is HTML-entity encoded JSON."""
    for candidate in (raw, html.unescape(raw)):
        try:
            return json.loads(candidate)
        except Exception:  # noqa: BLE001
            continue
    return None


def main() -> int:
    print("=" * 92)
    print("data.rbi.org.in (DBIE) - official data fetch")
    print("=" * 92)

    # --- 1. session token -------------------------------------------------
    print("\n[1] POST login/security_generateSessionToken")
    status, raw = post(f"{LOGIN}/security_generateSessionToken", {"body": {}})
    print(f"    HTTP {status}")
    parsed = as_json(raw)
    print(f"    body: {json.dumps(parsed)[:200] if parsed else raw[:200]}")

    print(f"\n    cookies received: {len(JAR)}")
    token = None
    for c in JAR:
        print(f"      {c.name} = {str(c.value)[:36]}... (len {len(str(c.value))})")
        if c.name.lower() in ("sessionid", "jsessionid", "session", "token"):
            token = c.value
    if token is None and len(JAR):
        token = list(JAR)[0].value

    if token is None:
        print("\n    No session cookie issued; cannot continue.")
        return 1
    print(f"    using token: {str(token)[:24]}... (len {len(str(token))})")

    # --- 2. data endpoints -------------------------------------------------
    endpoints = [
        ("FX reserves", "dbie_foreignExchangeReserves"),
        ("report catalogue", "dbie_getAllDBIEReports"),
        ("main menu", "dbie_mainMenuList"),
    ]
    hits = []
    for label, ep in endpoints:
        print(f"\n[2] POST dbie/{ep}")
        st, rw = post(f"{DBIE}/{ep}", {"body": {}}, token=str(token))
        print(f"    HTTP {st}")
        p = as_json(rw)
        if p is None:
            print(f"    body: {rw[:240]}")
            continue
        # DBIE answers 200 even on failure, with the real status nested in
        # header.status. Treating HTTP 200 as success is how you end up
        # parsing an error envelope as if it were data.
        hdr = (p.get("header") or {}) if isinstance(p, dict) else {}
        ok = str(hdr.get("status", "")).lower() == "success"
        print(f"    header.status: {hdr.get('status')} "
              f"errorCode={hdr.get('errorCode')}")
        if hdr.get("errorMessage"):
            print(f"    errorMessage: {hdr['errorMessage'][:120]}")
        print(f"    body: {json.dumps(p)[:500]}")
        if st == 200 and ok:
            hits.append((label, ep, p))

    print()
    print("=" * 92)
    if hits:
        print(f"WORKING: {[h[1] for h in hits]}")
    else:
        print("No data endpoint returned usable JSON.")
    return 0


if __name__ == "__main__":
    sys.exit(main())