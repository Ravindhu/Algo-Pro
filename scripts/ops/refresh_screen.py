"""Refresh data/screened/latest.json - the bot's screen - with verified price bands.

Run this on a schedule during the session (every 5-10 minutes) and before the
bot's first cycle. The bot itself never refreshes the screen; it only reads it.

Why it exists
-------------
``write_screen_file`` had no production caller, and the Screener.in payload has
no ``circuit_pct``, so every row was dropped and the file was written as
``{"results": []}``. The bot then saw zero symbols and no strategy could fire.
This job is the missing caller: it reads the saved screen, takes the price band
from the broker's own quote reply (``quant.data.circuit_band``), and writes the
file the bot reads.

Safety
------
* Read only. It sends no order; the only network calls are ``/api/v1/quotes``
  and ``/api/v1/history`` through the existing read-only client.
* Fails closed. A symbol without a verified, symmetric circuit band, or whose
  ASM/GSM flag the broker does not publish, is written out as DROPPED with its
  reason - never with an invented band.
* Writes atomically through ``write_screen_file`` (temp file + os.replace), so a
  crash mid-write leaves the previous screen intact.

Usage
-----
    python scripts/ops/refresh_screen.py
    python scripts/ops/refresh_screen.py --dry-run        # report, write nothing
    python scripts/ops/refresh_screen.py --source data/screened/last_screen.json
    python scripts/ops/refresh_screen.py --symbols INFY,TCS --dry-run

Exit codes
----------
    0  wrote at least one tradable symbol
    1  band source unreachable (OpenAlgo down / session expired)
    2  wrote the file but nothing passed the band filter (screen is empty)

Flow stage: Liquidity screener -> screen file
Status: IMPLEMENTED 2026-11-24 (read-only; no order path)
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.data.screen_refresh import load_candidates, refresh_once  # noqa: E402
from quant.execution.openalgo_client import OpenAlgoClient, http_transport  # noqa: E402
from quant.execution.readonly_client import OpenAlgoReadOnly  # noqa: E402

DEFAULT_SOURCE = ROOT / "data" / "screened" / "last_screen.json"
DEFAULT_TARGET = ROOT / "data" / "screened" / "latest.json"


def _load_candidates(path: pathlib.Path, symbols: str | None) -> list:
    """Explicit symbols win; otherwise the selected rows of a saved screen."""
    if symbols:
        return [{"symbol": s.strip().upper(), "selected": True}
                for s in symbols.split(",") if s.strip()]
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [dict(r) for r in (payload.get("results") or [])
            if isinstance(r, dict) and r.get("selected")]


def main() -> int:
    ap = argparse.ArgumentParser(description="Refresh the bot's screen with real price bands.")
    ap.add_argument("--source", type=pathlib.Path, default=DEFAULT_SOURCE,
                    help="saved Screener.in output to read candidates from")
    ap.add_argument("--target", type=pathlib.Path, default=DEFAULT_TARGET,
                    help="screen file the bot reads")
    ap.add_argument("--symbols", default=None,
                    help="comma list, overrides --source")
    ap.add_argument("--exchange", default="NSE")
    ap.add_argument("--base-url", default=os.environ.get("OPENALGO_URL", "http://127.0.0.1:5000"))
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    args = ap.parse_args()

    candidates = load_candidates(args.source, args.symbols)
    if not candidates:
        print(f"[FAIL] no candidates: {args.source} missing or has no selected rows",
              file=sys.stderr)
        return 2

    api_key = OpenAlgoClient.api_key_from_env()
    if not api_key:
        print("[FAIL] OPENALGO_API_KEY is not set; no band source", file=sys.stderr)
        return 1

    reader = OpenAlgoReadOnly(args.base_url, http_transport)
    from quant.data.bse_symbol import openalgo_bse_lookup

    if args.dry_run:
        from quant.data.bse_symbol import split_for_screen
        from quant.data.screen_refresh import enrich_parallel
        nse_list, bse_list, unresolved = split_for_screen(candidates, openalgo_bse_lookup)
        rows = enrich_parallel(nse_list, lambda s, e: reader.quotes(api_key, s, e), args.exchange)
        rows += enrich_parallel(bse_list, lambda s, e: reader.quotes(api_key, s, "BSE"), "BSE")
        print(f"candidates : {len(candidates)}  (dry run: nothing written)")
        for item in unresolved:
            print(f"  unresolved BSE code {item['code']} (OpenAlgo symbol: {item['symbol']})")
        return 0

    result = refresh_once(candidates, lambda s, e: reader.quotes(api_key, s, e), args.target,
                          lookup_bse=openalgo_bse_lookup)
    for item in result["unresolved"]:  # a BSE code the OpenAlgo master does not know: reported, never traded
        print(f"  unresolved BSE code {item['code']} (OpenAlgo symbol: {item['symbol']})")
    print(f"candidates : {result['candidates']}")
    print(f"enriched   : {result['enriched']}")
    print(f"selected   : {result['selected']}")
    print(f"dropped    : {result['dropped']}")
    print(f"wrote      : {args.target} ({result['wrote']} row(s))")
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
