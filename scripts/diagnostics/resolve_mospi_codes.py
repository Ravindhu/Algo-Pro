"""Resolve the valid filter codes for MoSPI get_data, per dataset.

Why this is needed
------------------
The MoSPI MCP proxy rejects a get_data call with a machine-readable list of the
params it wants, but NOT the values those params accept. So the last step is to
ask the server for its own vocabularies via get_indicators / get_metadata and
report the valid codes for:

    CPI    base_year, series
    NAS    series, frequency_code, indicator_code
    PLFS   indicator_code
    IIP    type

Output is a compact table suitable for copying into a provider's config.

Usage:
    openalgo\\Scripts\\python.exe Scripts\\resolve_mospi_codes.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

MCP_URL = "https://mcp.mospi.gov.in"
HEADERS = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream",
           "User-Agent": "blue-engine/1.0"}
_id = [0]


def rpc(method, params=None, timeout=60):
    _id[0] += 1
    p = {"jsonrpc": "2.0", "id": _id[0], "method": method}
    if params is not None:
        p["params"] = params
    req = urllib.request.Request(MCP_URL, data=json.dumps(p).encode(),
                                 headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", "replace")[:400]
    except Exception as exc:  # noqa: BLE001
        return f"ERR {type(exc).__name__}: {exc}"


def _env(raw):
    if not raw:
        return None
    t = raw.strip()
    if t.startswith("event:") or "data:" in t[:120]:
        ch = [l[5:].strip() for l in t.splitlines() if l.startswith("data:")]
        t = next((c for c in reversed(ch) if c and c != "[DONE]"), "")
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        return None


def tool(name, args, timeout=60):
    doc = _env(rpc("tools/call", {"name": name, "arguments": args}, timeout))
    if not isinstance(doc, dict):
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


DATASETS = ["CPI", "NAS", "PLFS", "IIP"]


def main() -> int:
    print("=" * 98)
    print("MoSPI - resolving valid filter codes per dataset")
    print("=" * 98)

    out = {}
    for ds in DATASETS:
        print("\n" + "-" * 98)
        print(f"DATASET {ds}")
        print("-" * 98)

        inds = tool("get_indicators", {"dataset": ds})
        print(f"  get_indicators -> {json.dumps(inds)[:800]}")

        # Ask for metadata for the first indicator we can discover, which
        # reveals the code vocabularies for that dataset.
        codes = []
        if isinstance(inds, dict):
            data = inds.get("data") or inds.get("indicators") or []
            if isinstance(data, list):
                for it in data[:6]:
                    if isinstance(it, dict):
                        c = (it.get("indicator_code") or it.get("code")
                             or it.get("id"))
                        if c is not None:
                            codes.append(str(c))
        print(f"  indicator codes discovered: {codes[:6]}")

        for c in codes[:2]:
            meta = tool("get_metadata", {"dataset": ds, "indicator_code": c})
            print(f"\n  get_metadata({ds}, indicator_code={c}) ->")
            print(f"    {json.dumps(meta)[:900]}")

        out[ds] = {"indicators": inds}

    print()
    print("=" * 98)
    print("SUMMARY")
    print("=" * 98)
    print("Use the codes printed above as the provider's default filters.")
    return 0


if __name__ == "__main__":
    sys.exit(main())