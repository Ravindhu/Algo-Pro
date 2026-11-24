"""engine_entry.py - run the POV engine with the guarded order gateway.

Usage
-----
    python scripts/run/engine_entry.py <config.json>            # analyze (sandbox) run
    python scripts/run/engine_entry.py <config.json> --paper    # paper run, no broker
    python scripts/run/engine_entry.py <config.json> --live     # live run (every gate must pass)

What it does
------------
The engine never builds its own broker client. This launcher does:
  1. Opens the OpenAlgo SDK client from the config (or the environment).
  2. Reads the OpenAlgo mode. Only ``analyze`` is allowed here.
  3. Wraps the SDK client in an EngineOrderGateway with OpenAlgoClient's
     analyze guard, so every order and cancel checks that the analyzer is on.
  4. Hands the gateway to the engine as ``order_client``.

Live mode needs the explicit --live flag AND OpenAlgo in live mode. It then runs the
same start gates as the live strategy bot (scripts/run/run_live.py startup_checks):
recorded human approval, unchanged limits file, complete limits, kill switch off,
holiday calendar and API key. Its order gate is OpenAlgoClient in
live mode (approval, limits hash and compliance, on every order). Any gate that does
not pass, or any unclear mode, is refused (fail closed). The committed config has
live_enabled false and null book caps, so live stays refused until the owner sets them.
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
from quant.audit.log import AuditLog  # noqa: E402
from quant.execution.openalgo_client import (  # noqa: E402
    EngineOrderGateway,
    OpenAlgoClient,
    http_transport,
)
from quant.risk.compliance import ComplianceConfig, ComplianceGate  # noqa: E402

DEFAULT_HOST = "http://127.0.0.1:5000"
LIMITS_FILE = _ROOT / "config" / "risk_limits.yaml"
LIVE_STATE_DIR = _ROOT / "data" / "engine_live"


def _live_checks():
    """The live strategy bot's start gates (scripts/run/run_live.py), loaded by file path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("run_live_checks", _ROOT / "scripts" / "run" / "run_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_order_client(raw: Dict[str, Any], *, live: bool = False,
                       limits_path: Optional[Path] = None,
                       state_dir: Optional[Path] = None) -> EngineOrderGateway:
    """Return a guarded gateway. Analyze needs the analyzer on; live needs --live and every start gate."""
    host = str(raw.get("host") or DEFAULT_HOST)
    sdk = init_client(api_key=raw.get("api_key"), host=raw.get("host"),
                      ws_url=raw.get("ws_url"))
    mode = str(read_mode(sdk)).strip().lower()  # unreadable mode raises: fail closed
    api_key = str(raw.get("api_key") or OpenAlgoClient.api_key_from_env())

    if live:
        if mode != "live":
            raise RuntimeError(f"refused: --live given but OpenAlgo mode is '{mode}'")
        state = Path(state_dir or LIVE_STATE_DIR)
        state.mkdir(parents=True, exist_ok=True)
        limits = Path(limits_path or LIMITS_FILE)
        checks = _live_checks().startup_checks(
            limits_path=limits, audit=AuditLog(state / "audit.jsonl"),
            kill_path=state / "kill.json")
        if not checks.get("ok") or checks.get("approval") is None:
            raise RuntimeError("live start refused: " + "; ".join(checks.get("reasons", ["no approval"])))
        cap = checks.get("capital_cap")
        if not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0:
            raise RuntimeError("live start refused: no fixed capital cap (live.max_capital_inr)")
        guard = OpenAlgoClient(
            host, http_transport, mode="live", live_approval=checks["approval"],
            limits_path=limits,
            compliance=ComplianceGate(ComplianceConfig(audit_dir=str(state))))
        return EngineOrderGateway(sdk, guard, api_key)

    if mode != "analyze":
        raise RuntimeError(
            f"refused: OpenAlgo mode is '{mode}'. Without --live this launcher runs analyze or paper only.")
    guard = OpenAlgoClient(host, http_transport, mode="analyze")
    return EngineOrderGateway(sdk, guard, api_key)


def run_guarded(raw: Dict[str, Any], paper: bool = False, live: bool = False) -> int:
    """Run the engine. Paper uses the recording broker; otherwise a guarded gateway is injected."""
    if paper:
        return run_with_config(raw, paper=True)
    return run_with_config(raw, order_client=build_order_client(raw, live=live))


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    paper = "--paper" in argv
    live = "--live" in argv
    if paper and live:
        print("[engine_entry] use either --paper or --live, not both")
        return 2
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
        return run_guarded(raw, paper=paper, live=live)
    except RuntimeError as exc:
        print(f"[engine_entry] {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
