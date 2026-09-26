"""Pull LIVE India macro data from the official MoSPI eSankhyiki MCP server.

This is the working path. Earlier attempts failed for a mundane reason: the
tool schemas were never inspected, so `get_data` was called with invented
arguments ("limit") instead of its real required shape.

Confirmed working (2026-09-26):
    POST https://mcp.mospi.gov.in   JSON-RPC 2.0
    initialize -> tools/list -> tools/call
    tools: list_datasets, get_indicators, get_metadata, get_data

Key discovery: the response carries `structuredContent`, which is plain JSON.
The `content[0].text` field is the SAME payload as a STRING, so a client that
only reads `text` ends up with a quoted blob and has to parse twice. Reading
`structuredContent` directly avoids that entirely.

Pipe: list_datasets -> get_indicators(dataset) -> get_metadata(dataset, code)
      -> get_data(dataset, filters)

Usage:
    openalgo\\Scripts\\python.exe Scripts\\fetch_mospi_live.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

# The server's schema descriptions contain non-cp1252 characters (e.g. Greek
# gamma in "Cp = Γ"). Windows consoles default to cp1252, so printing them
# raises UnicodeEncodeError and kills the run. Force UTF-8 on the output
# streams, with a lossy fallback so a stray glyph can never abort a fetch.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

MCP_URL = "https://mcp.mospi.gov.in"
PROTOCOL = "2025-06-18"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "User-Agent": "blue-engine/1.0 (+trading-research)",
}

_id = [0]


def rpc(method: str, params: dict | None = None, timeout: int = 60):
    _id[0] += 1
    payload = {"jsonrpc": "2.0", "id": _id[0], "method": method}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        MCP_URL, data=json.dumps(payload).encode(),
        headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:600]
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def _envelope(raw: str):
    """Decode an MCP reply into the JSON-RPC envelope (SSE-aware).

    MCP may answer either as plain JSON or as Server-Sent Events, and which one
    you get is not stable between calls on this server. Everything must go
    through here before touching the envelope, or a `tools/list` that happened
    to arrive as SSE will crash on json.loads().
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("event:") or "data:" in text[:120]:
        chunks = [l[5:].strip() for l in text.splitlines()
                  if l.startswith("data:")]
        text = next((c for c in reversed(chunks) if c and c != "[DONE]"), "")
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


def unwrap(raw: str):
    """Return the tool payload from either structuredContent or text."""
    doc = _envelope(raw)
    if doc is None:
        return None
    res = doc.get("result")
    if not isinstance(res, dict):
        return doc.get("error")
    if res.get("structuredContent") is not None:
        return res["structuredContent"]
    for c in res.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except Exception:  # noqa: BLE001
                return c["text"]
    return res


def call(name: str, args: dict, timeout: int = 60, verbose: bool = False):
    st, raw = rpc("tools/call", {"name": name, "arguments": args}, timeout)
    if verbose:
        doc = _envelope(raw)
        if isinstance(doc, dict):
            print(f"  isError={((doc.get('result') or {}).get('isError'))}")
    return unwrap(raw)


def main() -> int:
    print("=" * 96)
    print("MoSPI eSankhyiki MCP - live India macro data")
    print("=" * 96)

    st, raw = rpc("initialize", {
        "protocolVersion": PROTOCOL, "capabilities": {},
        "clientInfo": {"name": "blue-engine", "version": "1.0"}})
    if not raw:
        print("initialize failed")
        return 1
    print("initialized OK")

    # --- tool schemas (so arguments come from the server, not from guessing) -
    print("\n" + "=" * 96)
    print("Tool schemas")
    print("=" * 96)
    _, raw = rpc("tools/list", {})
    doc = _envelope(raw)
    schemas = {}
    for t in ((doc or {}).get("result") or {}).get("tools") or []:
        schemas[t["name"]] = t
        props = (t.get("inputSchema") or {}).get("properties") or {}
        req = (t.get("inputSchema") or {}).get("required") or []
        print(f"\n  {t['name']}")
        print(f"    required: {req}")
        for p, spec in props.items():
            print(f"      {p:<14} {spec.get('type','?'):<8} "
                  f"{str(spec.get('description',''))[:74]}")

    def arg_description(tool: str, field: str):
        return (((schemas.get(tool) or {}).get("inputSchema") or {})
                .get("properties") or {}).get(field) or {}

    # --- CPI: indicators + metadata ---------------------------------------
    print("\n" + "=" * 96)
    print("CPI (Consumer Price Index) -> 'inflation'")
    print("=" * 96)
    inds = call("get_indicators", {"dataset": "CPI"})
    print(f"  indicators: {json.dumps(inds)[:400]}")

    meta = call("get_metadata", {"dataset": "CPI", "indicator_code":
                "base_year" if "base_year" in str(inds) else "1"})
    print(f"  metadata  : {json.dumps(meta)[:600]}")

    # --- attempt a real get_data with server-shaped arguments -------------    # --- real data pulls --------------------------------------------------
    # Filters below were supplied by the SERVER: the required keys came from
    # get_data's own "missing_required" message, and the valid key set from its
    # "valid_params" list. Nothing here is guessed.
    print("\n" + "=" * 96)
    print("LIVE PULLS")
    print("=" * 96)

    targets = [
        ("inflation (CPI)", "CPI",
         {"base_year": "2024", "series": "Current", "limit": "6"}),
        ("gdp_growth (NAS)", "NAS",
         {"base_year": "2022-23", "limit": "6"}),
        ("employment (PLFS)", "PLFS",
         {"frequency_code": "1", "limit": "6"}),
        ("iip (industrial)", "IIP",
         {"base_year": "2011-12", "frequency": "Monthly", "limit": "6"}),
    ]

    ok, fail = [], []
    for label, dataset, filters in targets:
        out = call("get_data", {"dataset": dataset, "filters": filters})
        print(f"\n--- {label}  [{dataset}] ---")
        print(f"    filters: {json.dumps(filters)}")
        if isinstance(out, dict) and out.get("valid") is False:
            print(f"    REJECTED: {json.dumps(out)[:220]}")
            fail.append(label)
            continue

        data = out.get("data") if isinstance(out, dict) else out
        recs = None
        if isinstance(data, dict):
            for k in ("records", "data", "rows", "results"):
                if isinstance(data.get(k), list):
                    recs = data[k]
                    break
        elif isinstance(data, list):
            recs = data
        if recs:
            print(f"    records: {len(recs)}")
            print(f"    first  : {json.dumps(recs[0])[:280]}")
            ok.append(label)
        else:
            print(f"    payload: {json.dumps(out)[:380]}")
            fail.append(label)

    print()
    print("=" * 96)
    print("RESULT")
    print("=" * 96)
    print(f"  returning data : {ok}")
    print(f"  rejected/empty : {fail}")
    print()
    print("  source: https://esankhyiki.mospi.gov.in via the official MCP")
    print("  server https://mcp.mospi.gov.in - govt, no API key, no scraping")
    return 0


if __name__ == "__main__":
    sys.exit(main())