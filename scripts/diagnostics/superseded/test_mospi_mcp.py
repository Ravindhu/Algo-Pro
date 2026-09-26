"""Test the official MoSPI eSankhyiki MCP server for live India data.

Source: https://mcp.mospi.gov.in  (Server Card:
https://mcp.mospi.gov.in/.well-known/mcp/server-card.json)

This is an OFFICIAL Government of India endpoint, documented and advertised
for machine access -- unlike data.rbi.org.in, which is an undocumented SPA
behind bot protection. That difference is exactly why this is worth testing.

Documented workflow (from the server card):
    1. list_datasets     discover datasets
    2. get_indicators    browse indicators
    3. get_metadata      get filter options
    4. get_data          fetch the data

MCP speaks JSON-RPC 2.0 over HTTP (Streamable HTTP transport). The client must
send `Accept: application/json, text/event-stream`, and the server may reply as
SSE (`event: message\\ndata: {...}`) rather than plain JSON -- so both shapes
must be handled.

Relevant datasets for this project (from llms.txt / ai-catalog.json):
    cpi   Consumer Price Index      <- 'inflation'
    nas   National Accounts         <- 'gdp_growth'
    plfs  Periodic Labour Force     <- 'employment'
    iip   Index of Industrial Prod. <- new signal

Usage:
    openalgo\\Scripts\\python.exe Scripts\\test_mospi_mcp.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

MCP_URL = "https://mcp.mospi.gov.in"
PROTOCOL = "2025-06-18"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "User-Agent": "blue-engine/1.0 (+trading-research)",
}

_id = [0]


def rpc(method: str, params: dict | None = None, timeout: int = 45):
    _id[0] += 1
    payload = {"jsonrpc": "2.0", "id": _id[0], "method": method}
    if params is not None:
        payload["params"] = params

    req = urllib.request.Request(
        MCP_URL, data=json.dumps(payload).encode(),
        headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:600]
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def parse_mcp(raw: str):
    """MCP may answer with SSE framing; unwrap it before JSON parsing."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("event:") or "data:" in text[:200]:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    return json.loads(chunk)
                except Exception:  # noqa: BLE001
                    continue
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


def show(label: str, status: int, raw: str, limit: int = 700) -> object:
    print(f"\n--- {label} ---")
    print(f"HTTP {status}")
    parsed = parse_mcp(raw)
    if parsed is None:
        print(f"raw: {raw[:limit]}")
        return None
    print(json.dumps(parsed)[:limit])
    return parsed


def tool_result(parsed):
    """Unwrap a tools/call result into its structured content."""
    if not isinstance(parsed, dict):
        return None
    res = parsed.get("result")
    if not isinstance(res, dict):
        return parsed.get("error")
    # MCP tool results carry content:[{type:'text',text:'...'}]
    content = res.get("content")
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                try:
                    return json.loads(c["text"])
                except Exception:  # noqa: BLE001
                    return c["text"]
    return res.get("structuredContent") or res


def call_tool(name: str, args: dict, timeout: int = 60):
    return rpc("tools/call", {"name": name, "arguments": args}, timeout=timeout)


def main() -> int:
    print("=" * 96)
    print("MoSPI eSankhyiki MCP server - official Government of India data API")
    print("=" * 96)

    # --- initialise --------------------------------------------------------
    st, raw = rpc("initialize", {
        "protocolVersion": PROTOCOL,
        "capabilities": {},
        "clientInfo": {"name": "blue-engine", "version": "1.0"},
    })
    init = show("initialize", st, raw, 400)
    if init is None:
        print("\nMCP initialize failed; endpoint may not speak JSON-RPC at "
              "this path.")
        return 1

    # --- list tools --------------------------------------------------------
    st, raw = rpc("tools/list", {})
    tl = show("tools/list", st, raw, 900)
    tools = []
    if isinstance(tl, dict):
        for t in ((tl.get("result") or {}).get("tools") or []):
            tools.append(t.get("name"))
    print(f"\nTOOLS: {tools}")

    if not tools:
        print("No tools exposed; stopping.")
        return 1

    # --- 1. discover datasets ---------------------------------------------
    for name in ("list_datasets", "listDatasets"):
        if name in tools:
            st, raw = call_tool(name, {})
            got = show(f"{name}", st, raw, 1200)
            print(f"\nparsed: {json.dumps(tool_result(got))[:900]}")
            break

    # --- 2. indicators for CPI --------------------------------------------
    for name in ("get_indicators", "getIndicators"):
        if name in tools:
            st, raw = call_tool(name, {"dataset": "cpi"})
            got = show(f"{name}(cpi)", st, raw, 1200)
            print(f"\nparsed: {json.dumps(tool_result(got))[:900]}")
            break

    # --- 3/4. try a real data pull for CPI --------------------------------
    for name in ("get_data", "getData"):
        if name in tools:
            st, raw = call_tool(name, {"dataset": "cpi", "limit": 6})
            got = show(f"{name}(cpi)", st, raw, 1400)
            print(f"\nparsed: {json.dumps(tool_result(got))[:1100]}")
            break

    print()
    print("=" * 96)
    print("If the calls above returned data, this is a genuinely usable LIVE")
    print("source: official, documented, no key, and it covers CPI (inflation),")
    print("NAS (gdp), PLFS (employment) and IIP - four of the missing params.")
    return 0


if __name__ == "__main__":
    sys.exit(main())