"""Fetch real 1-minute INFY history from the live OpenAlgo broker bridge.

Credentials are read from .env and used in the request body ONLY. They are
never printed, logged, or written to any output file - per the project's
credential rules.

Writes the result to Temp/infy_1min.csv (a data cache, no secrets).
"""
from __future__ import annotations

import csv
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent / "infy_1min.csv"


def load_env(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


env = {}
for p in (ROOT / "openalgo" / ".env", ROOT / ".env"):
    env.update(load_env(p))

host = (env.get("HOST_SERVER") or "http://127.0.0.1:5000").rstrip("/")
api_key = env.get("OPENALGO_API_KEY", "")

if not api_key:
    print("OPENALGO_API_KEY not found in .env; cannot call the API")
    sys.exit(1)

print(f"host: {host}")
print(f"api key present: yes (len={len(api_key)})")


def history(symbol: str, exchange: str, interval: str,
            start: str, end: str) -> dict:
    body = json.dumps({
        "apikey": api_key,
        "symbol": symbol,
        "exchange": exchange,
        "interval": interval,
        "start_date": start,
        "end_date": end,
        "source": "api",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/v1/history", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


# INFY 1-minute, in monthly windows to keep each response manageable.
windows = [
    ("2026-06-01", "2026-06-30"),
    ("2026-07-01", "2026-07-31"),
    ("2026-08-01", "2026-08-31"),
    ("2026-09-01", "2026-09-28"),
]

rows = []
for start, end in windows:
    try:
        resp = history("INFY", "NSE", "1m", start, end)
    except urllib.error.HTTPError as exc:
        # Read the server's error body: it explains WHICH field is wrong.
        # The request body is never echoed (it carries the API key).
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:400]
        except Exception:
            pass
        print(f"  {start}..{end}: HTTP {exc.code} {exc.reason}")
        if detail:
            print(f"    server said: {detail}")
        continue
    except Exception as exc:
        print(f"  {start}..{end}: {type(exc).__name__}: {exc}")
        continue

    status = resp.get("status")
    data = resp.get("data")
    if status != "success":
        # Show the message only - never the request body (it holds the key).
        print(f"  {start}..{end}: status={status} msg={resp.get('message')}")
        continue
    if not data:
        print(f"  {start}..{end}: success but 0 rows")
        continue

    print(f"  {start}..{end}: {len(data)} rows")
    rows.extend(data)

print(f"\ntotal rows fetched: {len(rows)}")
if not rows:
    print("\nNo data returned. Nothing written.")
    sys.exit(1)

# Normalise to the column set the simulators expect.
cols = ["datetime", "open", "high", "low", "close", "volume"]
keys = set(rows[0].keys())
print("source columns:", sorted(keys))

ts_key = next((k for k in ("datetime", "timestamp", "date", "time")
               if k in keys), None)
if ts_key is None:
    print("no timestamp column found; cannot write")
    sys.exit(1)

written = 0
with OUT.open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=cols)
    w.writeheader()
    for r in rows:
        try:
            w.writerow({
                "datetime": r.get(ts_key),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r.get("volume") or 0),
            })
            written += 1
        except (KeyError, TypeError, ValueError):
            continue

print(f"wrote {written} rows to {OUT}")

# Data-quality report.
bad = [r for r in rows
       if float(r.get("high", 0)) < float(r.get("low", 0))]
zero_vol = [r for r in rows if not float(r.get("volume") or 0)]
print(f"quality: {len(bad)} rows with high<low, "
      f"{len(zero_vol)} rows with zero volume")