"""engine_entry.py - run the POV engine with the guarded order gateway.

Usage
-----
    python scripts/run/engine_entry.py <config.json>            # analyze (sandbox) run
    python scripts/run/engine_entry.py <config.json> --paper    # paper run, no broker

What it does
------------
The engine never builds its own broker client. This launcher does:
  1. Opens the OpenAlgo SDK client from the config (or the environment).
  2. Reads the OpenAlgo mode. Only ``analyze`` is allowed here.
  3. Wraps the SDK client in an EngineOrderGateway with OpenAlgoClient's
     analyze guard, so every order and cancel checks that the analyzer is on.
  4. Hands the gateway to the engine as ``order_client``.

Live mode is refused here. A live run needs a recorded human LiveApproval, which
this launcher does not create. Any unclear mode is refused too (fail closed).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "src", _ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from quant.engines.core.client import init_client  # noqa: E402
from quant.engines.core.mode import read_mode  # noqa: E402
from quant.engines.core.runner import load_raw_config, run_with_config  # noqa: E402
from quant.execution.openalgo_client import (  # noqa: E402
    EngineOrderGateway,
    OpenAlgoClient,
    http_transport,
)

DEFAULT_HOST = "http://127.0.0.1:5000"


def build_order_client(raw: Dict[str, Any]) -> EngineOrderGateway:
    """Return a guarded gateway for an analyze run. Raises if the mode is not analyze."""
    host = str(raw.get("host") or DEFAULT_HOST)
    sdk = init_client(api_key=raw.get("api_key"), host=raw.get("host"),
                      ws_url=raw.get("ws_url"))
    mode = str(read_mode(sdk)).strip().lower()  # unreadable mode raises: fail closed
    if mode != "analyze":
        raise RuntimeError(
            f"refused: OpenAlgo mode is '{mode}'. This launcher runs analyze or paper "
            "only; a live run needs a recorded LiveApproval.")
    api_key = str(raw.get("api_key") or OpenAlgoClient.api_key_from_env())
    guard = OpenAlgoClient(host, http_transport, mode="analyze")
    return EngineOrderGateway(sdk, guard, api_key)


def run_guarded(raw: Dict[str, Any], paper: bool = False) -> int:
    """Run the engine. Paper uses the recording broker; otherwise an analyze gateway is injected."""
    if paper:
        return run_with_config(raw, paper=True)
    return run_with_config(raw, order_client=build_order_client(raw))


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    paper = "--paper" in argv
    positional = [a for a in argv if not a.startswith("--")]
    if not positional:
        print("usage: engine_entry.py <config.json> [--paper]")
        return 2
    try:
        raw = load_raw_config(positional[0])
    except (FileNotFoundError, ValueError) as exc:
        print(f"[engine_entry] ERROR: {exc}")
        return 2
    try:
        return run_guarded(raw, paper=paper)
    except RuntimeError as exc:
        print(f"[engine_entry] {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
