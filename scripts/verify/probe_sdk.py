"""Quick SDK contract probe against the RUNNING OpenAlgo server."""
import sys
from pathlib import Path

ENV = (Path(__file__).resolve().parents[2] / ".env")
if not ENV.is_file():                     # some deployments keep it in _restored/
    ENV = Path(__file__).resolve().parents[2] / "_restored" / ".env"

key = None
for line in ENV.read_text(encoding="utf-8").splitlines():
    s = line.strip()
    if s.startswith("OPENALGO_API_KEY") and "=" in s:
        v = s.split("=", 1)[1].strip().strip("'").strip('"')
        if v:
            key = v

if not key:
    print("NO KEY FOUND in", ENV)
    sys.exit(1)

print("key loaded, length =", len(key))
from openalgo import api
c = api(api_key=key, host="http://127.0.0.1:5000")

for name in ("analyzerstatus", "funds", "orderbook", "positionbook"):
    try:
        r = getattr(c, name)()
        print("  [OK]  ", name, "->", r.get("status") if isinstance(r, dict) else "n/a")
    except Exception as e:
        print("  [FAIL]", name, "->", type(e).__name__, e)

try:
    r = c.quotes(symbol="INFY", exchange="NSE")
    print("  [OK]   quotes ->", r.get("status") if isinstance(r, dict) else "n/a",
          "| ltp =", (r.get("data") or {}).get("ltp") if isinstance(r, dict) else "?")
except Exception as e:
    print("  [FAIL] quotes ->", type(e).__name__, e)