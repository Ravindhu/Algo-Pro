"""run_supervised.py - start the POV engine under the live supervisor.

Usage
-----
    .venv\\Scripts\\python.exe scripts\\run\\run_supervised.py
    .venv\\Scripts\\python.exe scripts\\run\\run_supervised.py --config blue_engine/input/master.json
    .venv\\Scripts\\python.exe scripts\\run\\run_supervised.py --once     # one attempt, no restart

Exit codes mirror the engine: 0 clean stop, 4 safety halt (operator review),
5 order feed unavailable. A HALTED supervisor always exits 4 so a scheduler sees
a non-zero result and never "retries" a safety stop by itself.

What the supervisor does NOT do
-------------------------------
It never re-arms a tripped kill switch and never retries a safety exit. Before
every (re)start it runs the six gates in ``blue_engine.core.supervisor``:
kill switch clear, OpenAlgo reachable, positionbook verifiable, no unreconciled
working orders, sane quote, and a previous run that did not stop for safety.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from blue_engine.core.supervisor import (  # noqa: E402
    EXIT_SAFETY_STOP,
    Supervisor,
    SupervisorConfig,
    build_default_gates,
)

DEFAULT_CONFIG = _ROOT / "Blue_engine" / "input" / "master.json"
DEFAULT_JOURNAL = _ROOT / "data" / "supervisor_journal.jsonl"


def _engine_command(config_path: Path) -> list:
    """The exact command the supervisor will run, using the current python."""
    return [sys.executable, "-m", "blue_engine.core.runner", str(config_path)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                    help="engine config JSON (default: blue_engine/input/master.json)")
    ap.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL,
                    help="JSONL event journal (default: data/supervisor_journal.jsonl)")
    ap.add_argument("--once", action="store_true",
                    help="run the engine once; no restart on failure")
    ap.add_argument("--max-restarts", type=int, default=5)
    ap.add_argument("--restart-window", type=float, default=3600.0,
                    help="seconds over which max-restarts is counted")
    ap.add_argument("--restart-delay", type=float, default=5.0)
    ap.add_argument("--no-preflight", action="store_true",
                    help="skip the safety gates (paper/debug only)")
    args = ap.parse_args(argv)

    if not args.config.is_file():
        print(f"config not found: {args.config}")
        return 2
    try:
        raw = json.loads(args.config.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"config is not valid JSON: {exc!r}")
        return 2

    from blue_engine.core.client import init_client

    symbol = str(raw.get("symbol") or "")
    exchange = str(raw.get("exchange") or "NSE")
    host = str(raw.get("host") or "http://127.0.0.1:5000")
    client = init_client(api_key=raw.get("api_key"), host=raw.get("host"),
                         ws_url=raw.get("ws_url"))

    cfg = SupervisorConfig(
        command=_engine_command(args.config),
        restart_delay_seconds=args.restart_delay,
        max_restarts=args.max_restarts,
        restart_window_seconds=args.restart_window,
        preflight_on_start=not args.no_preflight,
        journal_path=str(args.journal),
    )
    tags = ["POV-EXIT", str(raw.get("strategy_label") or "POV")]
    # Paper (analyze) runs are exempt from the market-hours gate; a live run is
    # not. Unreadable mode counts as LIVE, so the gate fails closed.
    try:
        from blue_engine.core.mode import read_mode
        paper = str(read_mode(client)).strip().lower() == "analyze"
    except Exception:  # noqa: BLE001
        paper = False
    session_cfg = raw.get("market_session") if isinstance(
        raw.get("market_session"), dict) else {}
    gates = [] if args.no_preflight else build_default_gates(
        client, host, symbol, exchange, strategy_tags=tags, paper=paper,
        entry_cutoff=str(session_cfg.get("entry_cutoff", "14:45")))

    supervisor = Supervisor(cfg, gates=gates)
    if args.once:
        code = supervisor.run_once()
        print(f"supervisor: single run exit={code} state={supervisor.state}")
        return code

    report = supervisor.run()
    print(f"supervisor: state={report['state']} restarts={report['restarts']}")
    if report["halt_reason"]:
        print(f"supervisor: halted - {report['halt_reason']}")
    for event in report["events"][-10:]:
        print(f"  {event['ts']}  {event['kind']}  "
              f"{ {k: v for k, v in event.items() if k not in ('ts', 'kind')} }")
    return EXIT_SAFETY_STOP if report["state"] == "HALTED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
