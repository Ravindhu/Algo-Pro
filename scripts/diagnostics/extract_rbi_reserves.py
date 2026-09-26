"""Extract FX reserves from the RBI WSS page - and prove the value is real.

The feasibility run reported "PASS" for the WSS page on the strength of HTTP
200 alone, which is not evidence that the *number* is parseable. This pulls the
actual figure and checks it against the known-good value (765.9 bn USD).

If the number cannot be extracted reliably, that is reported plainly and the
parameter is marked NOT implementable -- a 200 OK is not a data source.

Usage:
    openalgo\\Scripts\\python.exe Scripts\\extract_rbi_reserves.py
"""

from __future__ import annotations

import re
import ssl
import sys
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

URLS = [
    "https://www.rbi.org.in/scripts/WSSView.aspx?Id=28307",
    "https://www.rbi.org.in/scripts/WSSView.aspx",
]

# Known-good cross-check (RBI WSS, Sep 2026), and the Homepage FX table.
EXPECTED_BN = 765.9


def get(url: str, timeout: int = 45) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,*/*",
        "Accept-Language": "en-IN,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout,
                                context=ssl.create_default_context()) as r:
        return r.read().decode("utf-8", "replace")


def strip_tags(h: str) -> str:
    h = re.sub(r"<script.*?</script>", " ", h, flags=re.S | re.I)
    h = re.sub(r"<style.*?</style>", " ", h, flags=re.S | re.I)
    h = re.sub(r"<[^>]+>", " ", h)
    h = (h.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"[ \t\r\f\v]+", " ", h)


def main() -> int:
    for url in URLS:
        print("=" * 92)
        print(f"  {url}")
        try:
            html = get(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  FETCH FAILED: {type(exc).__name__}: {exc}")
            continue
        text = strip_tags(html)
        print(f"  bytes={len(html):,}  text_chars={len(text):,}")

        # Locate the reserves block.
        idx = text.lower().find("foreign exchange reserves")
        print(f"  'foreign exchange reserves' found at char {idx}")

        if idx >= 0:
            window = text[idx: idx + 900]
            print("\n  --- context window ---")
            print("  " + window.strip()[:700].replace("\n", " "))

            # Candidate numeric patterns near the label.
            nums = re.findall(r"(\d{1,3}(?:,\d{2,3})*\.\d{2})", window)
            print(f"\n  numeric candidates: {nums[:8]}")
            for n in nums[:8]:
                v = float(n.replace(",", ""))
                # Reserves in US$ mn are ~7,659,000 ; in bn ~765.9
                if 500 <= v <= 1200:
                    verdict = "PLAUSIBLE (bn USD)"
                    if abs(v - EXPECTED_BN) < 0.6:
                        verdict = f"MATCHES expected {EXPECTED_BN}"
                    print(f"    {n} -> {v}  {verdict}")
                elif 5_000_000 <= v <= 12_000_000:
                    print(f"    {n} -> {v}  PLAUSIBLE (US$ mn -> {v/1000:.1f} bn)")

        # Also check the homepage FX table, which we know parses.
        try:
            home = strip_tags(get("https://www.rbi.org.in/"))
            for lbl in ("Foreign Currency Assets", "Total Reserves",
                        "Foreign Exchange Reserves"):
                i = home.find(lbl)
                if i >= 0:
                    print(f"\n  homepage '{lbl}': {home[i:i+140].strip()[:130]}")
        except Exception:  # noqa: BLE001
            pass

    print()
    print("=" * 92)
    print("VERDICT")
    print("=" * 92)
    print("If no plausible value was extracted above, forex_reserves is NOT")
    print("reliably parseable from these pages and should stay a dated static")
    print("value rather than a fake 'live' provider.")
    return 0


if __name__ == "__main__":
    sys.exit(main())