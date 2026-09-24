r"""
================================================================================
sample_order.py - PLACE A SAMPLE ORDER IN OPENALGO (ANALYZER / PAPER MODE)
================================================================================

PURPOSE
-------
This is a quick smoke-test script for the OpenAlgo platform. It places one
virtual order through the REST API and then reads it back from the
orderbook to confirm:

  1. The OpenAlgo server is running and reachable.
  2. The OpenAlgo API key is valid.
  3. The platform is in `analyze` (paper) mode, so no real money is touched.
  4. The REST endpoints /api/v1/placeorder and /api/v1/orderbook work.
  5. The fill is reported back at a simulated price (in INR).

USAGE
-----
    # 1. Make sure the OpenAlgo server is running:
    #       python app.py
    #
    # 2. Activate the venv (one-off):
    #       .\.venv\Scripts\Activate.ps1
    #
    # 3. Run this script:
    #       python pov_engine/sample_order.py
    #
    # Expected output:
    #       HTTP 200
    #       {"mode":"analyze","orderid":"...","status":"success"}
    #       ORDERBOOK shows the order with status=complete

WHEN TO USE THIS
----------------
  * First time you bring up a fresh OpenAlgo install - verify the chain works.
  * After changing your .env / API key - confirm auth still works.
  * Before running a real POV engine - confirm paper trading still simulates.
  * When debugging a "why is my order rejected" issue - place one here and
    inspect the rejection_reason field in the orderbook.

CUSTOMISATION
-------------
Edit the CONFIG block at the top of the file:

  SYMBOL       = "INFY"        # any NSE symbol, e.g. "RELIANCE", "TCS"
  EXCHANGE     = "NSE"         # NSE, BSE, NFO, MCX, ...
  ACTION       = "BUY"         # BUY or SELL
  QUANTITY     = 1             # any positive integer
  PRICE_TYPE   = "MARKET"      # MARKET or LIMIT
  PRODUCT      = "CNC"         # CNC (delivery) or MIS (intraday)
  STRATEGY     = "sample-test" # tag visible in orderbook; useful for filtering

For a LIMIT order, set PRICE_TYPE = "LIMIT" and adjust LIMIT_PRICE.

EXPECTED OUTPUT (REAL EXAMPLE)
-----------------------------
    ============================================================
    PLACING ORDER
    ============================================================
    {
      "apikey": "****09c",
      "strategy": "sample-test",
      "symbol": "INFY",
      "action": "BUY",
      "exchange": "NSE",
      "pricetype": "MARKET",
      "product": "CNC",
      "quantity": 1
    }
    ============================================================
    HTTP 200
    {"mode":"analyze","orderid":"26090504264223","status":"success"}

    ============================================================
    ORDERBOOK (latest 3 orders)
    ============================================================
    Total orders: 1
    Statistics: { "total_buy_orders": 1, "total_completed_orders": 1, ... }
    Latest orders:
    {
      "action": "BUY",
      "average_price": 1243.0,
      "filled_quantity": 1,
      "order_status": "complete",
      "orderid": "26090504264223",
      "symbol": "INFY",
      ...
    }

TROUBLESHOOTING
---------------
  * HTTP 400 "Unknown field"     -> the field name is wrong.
                                      Note: OpenAlgo uses `pricetype`
                                      (not `price_type`).
  * HTTP 401 Unauthorized         -> bad API key. Generate a new one in the
                                      OpenAlgo UI: API Keys section.
  * HTTP 404 Not Found            -> wrong endpoint path, or server not running.
  * "Cannot connect"              -> OpenAlgo server is down. Start it with
                                      `python app.py`.
  * mode != "analyze"             -> the platform is in LIVE mode. Switch to
                                      PAPER (Analyzer) mode in the UI before
                                      running this script for paper trading.
  * order_status = "rejected"     -> check `rejection_reason` in the orderbook.
                                      Common: market closed, bad symbol, bad qty.

NOTES
-----
  * This script does NOT require the broker to be connected. The analyzer
    simulates everything locally.
  * The same payload structure is used by the POV engine, so this doubles
    as a minimal reference of the placeorder contract.
  * For LIMIT orders, OpenAlgo expects a string price like "1500.00" - the
    `requests` library will JSON-serialise the float correctly.
"""

# =============================================================================
# IMPORTS
# =============================================================================
import json
import os
import sys
import requests


# =============================================================================
# CONFIG  (edit these values to test different symbols / actions)
# =============================================================================
# The API key is NEVER hardcoded here. It is read from the environment, falling
# back to _restored/.env (the source of truth for local state). A committed or
# shared script carrying a live broker key would hand over trading access to
# anyone who can read the file.
#
#   PowerShell:  $env:OPENALGO_API_KEY = "<your-key>"
#   or put OPENALGO_API_KEY=<key> in my-trading-system\_restored\.env
#
# To get a key: OpenAlgo UI -> API Keys -> generate/copy.
def _load_api_key() -> str:
    """Resolve the OpenAlgo API key from env, else _restored/.env, else .env."""
    key = os.getenv("OPENALGO_API_KEY", "").strip()
    if key:
        return key
    from pathlib import Path
    for candidate in (
        Path(__file__).resolve().parent.parent / "_restored" / ".env",
        Path(__file__).resolve().parent.parent / "openalgo" / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("#") or "=" not in s:
                continue
            name, _, val = s.partition("=")
            if name.strip() == "OPENALGO_API_KEY":
                v = val.strip().strip("'").strip('"')
                if v:
                    return v
    return ""


API_KEY = _load_api_key()
BASE_URL = os.getenv("OPENALGO_URL", "http://127.0.0.1:5000").rstrip("/")

# --- Order parameters ---
SYMBOL = "INFY"           # any NSE symbol
EXCHANGE = "NSE"          # NSE, BSE, NFO, MCX
ACTION = "BUY"            # BUY or SELL
QUANTITY = 1              # positive integer
PRICE_TYPE = "MARKET"     # MARKET or LIMIT
PRODUCT = "CNC"           # CNC (delivery) or MIS (intraday)
STRATEGY = "sample-test"  # tag for filtering in the UI
LIMIT_PRICE = 1500.0      # only used when PRICE_TYPE == "LIMIT"


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================
def _mask_api_key(key: str) -> str:
    """Return a redacted API key like '****09c' for safe printing."""
    if not key or len(key) < 4:
        return "****"
    return f"****{key[-4:]}"


def place_order() -> requests.Response:
    """
    POST a single order to /api/v1/placeorder.

    Returns the requests.Response object so the caller can inspect
    status_code, json(), and the raw body.

    IMPORTANT schema notes:
      * The order-type field is `pricetype`  (NOT `price_type`).
      * The API key can be sent in the JSON body OR as the `X-API-KEY`
        header. We send it in the body because that is what the POV
        engine does internally.
      * For LIMIT orders, `price` is required. We omit it for MARKET.
    """
    payload = {
        "apikey": API_KEY,
        "strategy": STRATEGY,
        "symbol": SYMBOL,
        "action": ACTION,
        "exchange": EXCHANGE,
        "pricetype": PRICE_TYPE,
        "product": PRODUCT,
        "quantity": QUANTITY,
    }
    if PRICE_TYPE.upper() == "LIMIT":
        payload["price"] = float(LIMIT_PRICE)

    # ---- Print the request (with masked key) ----
    safe_payload = dict(payload)
    safe_payload["apikey"] = _mask_api_key(payload["apikey"])
    print("=" * 60)
    print("PLACING ORDER")
    print("=" * 60)
    print(json.dumps(safe_payload, indent=2))
    print("=" * 60)

    # ---- Send ----
    try:
        resp = requests.post(
            f"{BASE_URL}/api/v1/placeorder",
            json=payload,
            timeout=10,
        )
    except requests.exceptions.ConnectionError:
        print(f"ERROR: cannot connect to {BASE_URL}.")
        print("       Is the OpenAlgo server running? Start it with:")
        print("           python app.py")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("ERROR: request timed out after 10s.")
        sys.exit(1)

    print(f"HTTP {resp.status_code}")
    print(resp.text)
    return resp


def fetch_orderbook(max_show: int = 3) -> None:
    """
    POST to /api/v1/orderbook (the endpoint accepts a JSON body with
    the apikey) and pretty-print the latest N orders.

    Args:
        max_show: how many of the most recent orders to print
    """
    print("\n" + "=" * 60)
    print(f"ORDERBOOK (latest {max_show} orders)")
    print("=" * 60)

    try:
        resp = requests.post(
            f"{BASE_URL}/api/v1/orderbook",
            json={"apikey": API_KEY},
            timeout=10,
        )
    except requests.exceptions.ConnectionError:
        print(f"ERROR: cannot connect to {BASE_URL}.")
        return

    if resp.status_code != 200:
        print(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return

    try:
        body = resp.json()
    except ValueError:
        print(f"Non-JSON response: {resp.text[:300]}")
        return

    data = body.get("data", {}) or {}
    orders = data.get("orders", []) or []
    stats = data.get("statistics", {}) or {}
    mode = body.get("mode", "?")

    print(f"Mode:         {mode}")
    print(f"Total orders: {len(orders)}")
    if stats:
        print(f"Statistics:   {json.dumps(stats, indent=2)}")
    if not orders:
        print("(no orders yet)")
        return

    print(f"\nLatest {min(max_show, len(orders))} order(s):")
    for o in orders[-max_show:]:
        print("-" * 40)
        print(json.dumps(o, indent=2, default=str))

    # ---- Highlight the most recent order's outcome ----
    last = orders[-1]
    status = last.get("order_status", "?")
    oid = last.get("orderid", "?")
    sym = last.get("symbol", "?")
    qty = last.get("quantity", 0)
    avg = last.get("average_price", 0)
    if status == "complete":
        print(f"\n[OK] {oid}: {ACTION} {qty} {sym} filled at Rs.{avg}")
    elif status == "rejected":
        print(f"\n[REJECTED] {oid}: {last.get('rejection_reason', 'no reason given')}")
    else:
        print(f"\n[{status.upper()}] {oid}")


def fetch_positions() -> None:
    """
    POST to /api/v1/positionbook and print the open positions.
    Useful after a SELL to confirm the BUY was closed.
    """
    print("\n" + "=" * 60)
    print("POSITION BOOK")
    print("=" * 60)
    try:
        resp = requests.post(
            f"{BASE_URL}/api/v1/positionbook",
            json={"apikey": API_KEY},
            timeout=10,
        )
    except requests.exceptions.ConnectionError:
        print(f"ERROR: cannot connect to {BASE_URL}.")
        return
    if resp.status_code != 200:
        print(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return
    try:
        body = resp.json()
    except ValueError:
        print(f"Non-JSON response: {resp.text[:200]}")
        return
    print(json.dumps(body, indent=2, default=str))


# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    # 0. Fail loudly if the key is missing. Without this guard the script would
    #    POST an empty apikey and surface a confusing 401 from the server.
    if not API_KEY:
        print("ERROR: OPENALGO_API_KEY is not set.")
        print("       Set it in the environment, or place it in")
        print("       my-trading-system\\_restored\\.env, then re-run.")
        sys.exit(2)

    # 1. Place the order
    place_order()

    # 2. Read the orderbook to confirm the order is recorded
    fetch_orderbook(max_show=3)

    # 3. (Optional) show current positions
    fetch_positions()

