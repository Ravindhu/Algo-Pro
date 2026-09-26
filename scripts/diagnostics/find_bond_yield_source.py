"""Find a scriptable source for the India 10Y G-Sec YIELD (not the index).

Context: the OpenAlgo SDK exposes `NIFTYGS10YR` which quotes 2647.02 -- a bond
INDEX level, not a yield. Confirmed independently: the actual 10Y yield is
~6.68%. Feeding the index into a signal that expects a percentage would pin the
score at -1 forever, so it cannot be used.

FBIL and CCIL both render via JavaScript and expose no data endpoint, so they
are ruled out for the same reason data.rbi.org.in was.

This checks the remaining realistic options:
  A. NSE's public bond page / API (yield is quoted there)
  B. RBI homepage - already proven parseable for FBIL, does it carry the yield?
  C. FRED (St. Louis Fed) - mirrors India 10Y for some series, free + scriptable

Run:
    openalgo\\Scripts\\python.exe Scripts\\find_bond_yield_source.py
"""

from __future__ import annotations

import json
import re
import ssl
import sys
import urllib.error
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def get(url, timeout=30, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*",
         "Accept-Language": "en-IN,en;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout,
                                context=ssl.create_default_context()) as r:
        return r.status, r.read().decode("utf-8", "replace")


def main() -> int:
    print("=" * 98)
    print("India 10Y G-Sec YIELD - source feasibility")
    print("=" * 98)

    # --- A. NSE ------------------------------------------------------------
    print("\n[A] NSE public endpoints")
    nse_urls = [
        ("https://www.nseindia.com/api/allIndices", "allIndices"),
        ("https://www.nseindia.com/api/liveBonds-traded-on-cm",
         "liveBonds-traded-on-cm"),
        ("https://www.nseindia.com/api/corporates-debt-market?index=debt",
         "debt market"),
    ]
    for url, label in nse_urls:
        try:
            st, body = get(url, timeout=25, headers={
                "Accept": "application/json",
                "Referer": "https://www.nseindia.com/"})
            is_json = body.strip().startswith(("{", "["))
            print(f"  [{st}] {label:<26} json={is_json} bytes={len(body)}")
            if is_json:
                print(f"        {body[:180]}")
                if "GS" in body or "yield" in body.lower():
                    print("        !! contains bond/yield material")
        except Exception as exc:  # noqa: BLE001
            print(f"  [ERR] {label:<26} {type(exc).__name__}")

    # --- B. RBI homepage (proven parseable) --------------------------------
    print("\n[B] rbi.org.in homepage (already parses for FBIL)")
    try:
        st, body = get("https://www.rbi.org.in/", timeout=40)
        text = re.sub(r"<[^>]+>", " ", body)
        for pat, label in [
            (r"(?:10[-\s]?[Yy]ear|10Y)[^.]{0,60}?(\d+\.\d{1,2})", "10Y mention"),
            (r"G-?Sec[^.]{0,60}?(\d+\.\d{1,2})", "G-Sec mention"),
            (r"[Yy]ield[^.]{0,60}?(\d+\.\d{1,2})", "yield mention"),
        ]:
            m = re.search(pat, text)
            print(f"  {label:<16} {'MATCH: ' + m.group(0)[:80] if m else 'no match'}")
        print(f"  page bytes {len(body):,}")
        # Policy rates ARE on the homepage and are a usable monotone proxy.
        for name, pat in [("repo", r"[Rr]epo\s*[Rr]ate[^0-9]{0,40}(\d+\.\d{1,2})"),
                          ("reverse repo",
                           r"[Rr]everse\s*[Rr]epo[^0-9]{0,40}(\d+\.\d{1,2})"),
                          ("MSF", r"MSF[^0-9]{0,40}(\d+\.\d{1,2})"),
                          ("Bank Rate",
                           r"[Bb]ank\s*[Rr]ate[^0-9]{0,40}(\d+\.\d{1,2})")]:
            m = re.search(pat, text)
            print(f"  {name:<14} {'-> ' + m.group(1) if m else 'not found'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ERR {type(exc).__name__}: {exc}")

    # --- C. FRED -----------------------------------------------------------
    print("\n[C] FRED (St. Louis Fed) - free, scriptable, no key for CSV")
    fred = [
        ("INDIRLT01STM", "India LT govt bond yield, monthly"),
        ("IRLTLT01INM156N", "India long-term rate (OECD)"),
        ("INDIRLTLT01STQ", "India LT rate, quarterly"),
    ]
    for sid, label in fred:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
        try:
            st, body = get(url, timeout=30)
            lines = [ln for ln in body.strip().splitlines() if ln]
            print(f"  [{st}] {sid:<16} {label}")
            print(f"        rows={len(lines)} header={lines[0][:60]}")
            if len(lines) > 1:
                print(f"        latest: {lines[-1]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [ERR] {sid:<16} {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())