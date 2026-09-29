"""Probe the OpenAlgo server for reachability.

Deliberately NEVER prints key/secret/token values - only whether they are
present and their length, so no credential leaks into console output, logs
or a transcript.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path) -> dict:
    """Parse a .env file into a dict WITHOUT logging any values."""
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

# Presence flags only - never the values.
print("=== credential presence (values never printed) ===")
for k in ("OPENALGO_API_KEY", "BROKER_API_KEY", "BROKER_API_SECRET",
          "HOST_SERVER", "WEBSOCKET_URL"):
    v = env.get(k) or os.environ.get(k)
    print(f"  {k:<22}: {'present' if v else 'MISSING'}"
          + (f" (len={len(v)})" if v else ""))

host = env.get("HOST_SERVER") or os.environ.get("HOST_SERVER")
print(f"\n  HOST_SERVER           : {host}")

if not host:
    print("\ncannot probe: HOST_SERVER not configured")
    sys.exit(1)

import json
import urllib.error
import urllib.request

base = host.rstrip("/")
probes = [
    ("ping", f"{base}/api/v1/ping"),
    ("quotes NSE:INFY", f"{base}/api/v1/quotes?symbol=INFY&exchange=NSE"),
]

for name, url in probes:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read(400).decode("utf-8", "ignore")
            print(f"\n=== {name} ===")
            print(f"  HTTP {resp.status}")
            print(f"  body: {body[:300]}")
    except urllib.error.HTTPError as exc:
        print(f"\n=== {name} ===")
        print(f"  HTTP {exc.code}: {exc.reason}")
    except Exception as exc:
        print(f"\n=== {name} ===")
        print(f"  unreachable: {type(exc).__name__}: {exc}")