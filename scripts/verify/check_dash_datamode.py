"""Check the served `data-mode` on the Dash page shell.

The mode banner (a Dash component rendered from the layout) and the
`data-mode` body attribute (from the hand-written index_string) are set in
two different places. If they disagree, the banner text says one thing while
the CSS palette paints the other -- which is exactly the confusion the
analyzer/live distinction exists to prevent.

Usage:  openalgo\\Scripts\\python.exe Scripts\\check_dash_datamode.py
"""

from __future__ import annotations

import re
import sys
import urllib.request

PATHS = ["/", "/portfolio", "/analytics-portfolio", "/risk", "/analytics",
         "/economic", "/infrastructure"]
BODY_RE = re.compile(r'<body[^>]*data-mode="([^"]*)"')


def main() -> int:
    mismatch = 0
    print(f"{'route':<24} {'data-mode':<12} note")
    print("-" * 62)
    for path in PATHS:
        try:
            html = urllib.request.urlopen(
                "http://127.0.0.1:8050" + path, timeout=20
            ).read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            print(f"{path:<24} {'ERROR':<12} {str(exc)[:40]}")
            continue
        m = BODY_RE.search(html)
        got = m.group(1) if m else "NOT FOUND"
        note = ""
        if got not in ("analyzer", "live"):
            note = "<-- not a valid palette key"
            mismatch += 1
        print(f"{path:<24} {got:<12} {note}")
    print("-" * 62)
    print("Expected: 'analyzer' (OpenAlgo reports analyze_mode=true).")
    print("MISMATCHES:", mismatch)
    return 0


if __name__ == "__main__":
    sys.exit(main())