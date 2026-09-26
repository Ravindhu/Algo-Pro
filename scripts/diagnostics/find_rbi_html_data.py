"""Find which RBI HTML pages actually contain parseable numbers.

The WSS pages return HTTP 200 with only a 3.4KB template - the data loads by
JS, so they are NOT scrapeable. This checks the other RBI HTML surfaces for
real numeric content, so the "implementable" list is based on pages that
genuinely carry data rather than on a 200 status.

Known-good baseline: the homepage already parses for the FBIL reference rate,
which proves the technique works when the number IS in the HTML.

Usage:
    openalgo\\Scripts\\python.exe Scripts\\find_rbi_html_data.py
"""

from __future__ import annotations

import re
import ssl
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

PAGES = [
    ("homepage", "https://www.rbi.org.in/"),
    ("WSS view", "https://www.rbi.org.in/scripts/WSSView.aspx?Id=28307"),
    ("WSS index", "https://www.rbi.org.in/scripts/WSS.aspx"),
    ("Press Releases", "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx"),
    ("Notifications", "https://www.rbi.org.in/Scripts/NotificationUser.aspx"),
    ("Statistics", "https://www.rbi.org.in/Scripts/Statistics.aspx"),
    ("Database (DBIE)", "https://www.rbi.org.in/Scripts/DataBaseonIndianEconomy.aspx"),
    ("Reference Rate archive",
     "https://www.rbi.org.in/Scripts/ReferenceRateArchive.aspx"),
]

# Things worth having, and how each would look in HTML.
TARGETS = {
    "USD/INR reference": r"INR\s*/\s*1\s*USD",
    "FX reserves": r"[Ff]oreign [Ee]xchange [Rr]eserves",
    "repo rate": r"[Rr]epo [Rr]ate",
    "CPI inflation": r"[Cc]onsumer [Pp]rice",
    "GDP": r"\bGDP\b",
    "10Y / bond": r"(?:10[-\s]?[Yy]ear|G-?Sec)",
}


def get(url: str, timeout: int = 45) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,*/*",
        "Accept-Language": "en-IN,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout,
                                context=ssl.create_default_context()) as r:
        return r.read().decode("utf-8", "replace")


def main() -> int:
    print("=" * 100)
    print("Which RBI HTML pages carry parseable numbers?")
    print("=" * 100)

    findings = {}
    for label, url in PAGES:
        try:
            html = get(url)
        except Exception as exc:  # noqa: BLE001
            print(f"\n[{label}] FETCH FAILED {type(exc).__name__}: {str(exc)[:60]}")
            continue

        text = re.sub(r"<[^>]+>", " ", html)
        print(f"\n[{label}]  {len(html):,} bytes  ({url})")

        found = []
        for name, pat in TARGETS.items():
            if re.search(pat, text or html):
                found.append(name)
        findings[label] = found
        print(f"   numeric content present: {found or 'NONE'}")

        # Show an actual figure if we can find one near a reserves label.
        m = re.search(r"[Ff]oreign [Ee]xchange [Rr]eserves[\s\S]{0,220}", html)
        if m:
            snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(0)))
            print(f"   reserves context: {snippet[:150]}")
        m2 = re.search(r"INR\s*/\s*1\s*USD[\s\S]{0,120}", html)
        if m2:
            snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m2.group(0)))
            print(f"   USD/INR context : {snippet[:110]}")

    print()
    print("=" * 100)
    print("SUMMARY - pages that actually contain each target")
    print("=" * 100)
    for target in TARGETS:
        pages = [p for p, f in findings.items() if target in f]
        flag = "YES" if pages else "NO "
        print(f"  [{flag}] {target:<20} {pages}")

    print()
    print("NOTE: 'present' means the phrase is in the HTML, not that the number")
    print("is parseable. Only the homepage's USD/INR is proven end-to-end;")
    print("anything else needs its own extraction test before being wired.")
    return 0


if __name__ == "__main__":
    main()