"""Fetch the NSE trading-holiday list and write config/calendar/holidays.csv.

Usage (run on your own machine, which has internet access to nseindia.com):
    python scripts/research/fetch_nse_holidays.py [--year 2026]

What it does:
  1. Opens the NSE homepage first (NSE's API needs the session cookies it sets).
  2. Calls the public holiday-master API (type=trading).
  3. Keeps only the rows for the requested year (default: current year).
  4. Writes date,exchange,segment,description to config/calendar/holidays.csv.

Safety: if the request fails or no rows parse, the existing file is NOT touched.
Scope: NSE segments only. BSE equity holidays normally match NSE CM, but BSE is
NOT fetched here; confirm BSE dates separately before relying on them.

Status: UNVERIFIED RESPONSE FORMAT. The field names used below ("tradingDate",
"description", segment keys) are the usual NSE shape but were not checked against
a live response from this sandbox (NSE is blocked here). The script prints the
segment keys it found on every run, so a format change is visible immediately.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "config" / "calendar" / "holidays.csv"
HOME = "https://www.nseindia.com"
API = HOME + "/api/holiday-master?type=trading"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _get(opener, url: str, accept: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9", "Referer": HOME + "/"})
    with opener.open(req, timeout=20) as resp:
        return resp.read()


def fetch_payload() -> dict:
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    _get(opener, HOME, "text/html")  # sets the session cookies NSE requires
    return json.loads(_get(opener, API, "application/json").decode("utf-8"))


def parse_rows(payload: dict, year: int) -> list:
    rows = []
    for segment, entries in payload.items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            raw = (e or {}).get("tradingDate") or ""
            try:
                day = dt.datetime.strptime(raw, "%d-%b-%Y").date()
            except ValueError:
                continue
            if day.year != year:
                continue
            rows.append({"date": day.isoformat(), "exchange": "NSE",
                         "segment": segment,
                         "description": (e.get("description") or "").strip()})
    rows.sort(key=lambda r: (r["date"], r["segment"]))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=dt.date.today().year)
    args = ap.parse_args()
    try:
        payload = fetch_payload()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"FETCH FAILED: {exc}. Existing holidays.csv left unchanged.", file=sys.stderr)
        return 1
    print("segment keys in response:", sorted(payload.keys()))
    rows = parse_rows(payload, args.year)
    if not rows:
        print(f"NO ROWS for {args.year} parsed. Check the segment keys above. "
              "Existing holidays.csv left unchanged.", file=sys.stderr)
        return 2
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "exchange", "segment", "description"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows for {args.year} to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
