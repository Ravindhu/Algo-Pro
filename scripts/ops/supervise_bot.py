r"""supervise_bot.py - run the strategy bot under the live supervisor (START ENGINE path).

WHY THIS EXISTS
---------------
The dashboard's START ENGINE button spawned scripts/run/run_supervised.py, which is not in
this repo (it is parked in Temp/Stag/old_runner/), and even that file ran
scripts/run/engine_entry.py, which is also gone. So the button logged in to the broker and
then stopped silently: no engine, no bot, no order. Fixed 2026-11-27.

This script is the missing middle. It runs the STRATEGY BOT - `execution/strategy_bot.py`,
the same bot `run_strategy_bot.py` (paper) and `run_strategy_bot_live.py` (live) drive -
behind the existing supervisor in `quant.engines.core.supervisor`.

WHAT IT DOES NOT DO
-------------------
* It never places, cancels or modifies an order. Only the risk gate may do that, and this
  module does not import the order client.
* It never re-arms a kill switch and never retries a safety stop. The supervisor halts.
* It does not relax the bot's own gates. `StrategyBot.recon` stays None until the bot's
  startup reconciliation runs, which means ENTRIES STAY BLOCKED until it does. That is the
  bot's fail-closed default, not something this wrapper changes.

MODES
-----
    --mode paper   (default) scripts/run/run_strategy_bot.py       simulated fills, no order
    --mode live              scripts/run/run_strategy_bot_live.py  still gated by bot.yaml
                             (mode: live + live_enabled: true), LiveApproval, limits hash and
                             a passing startup reconciliation. This script adds nothing to
                             those gates and cannot bypass them.

GATES (before the first start, and before every restart)
--------------------------------------------------------
kill switch, OpenAlgo reachable, position book verifiable, no unreconciled working orders,
sane quote. The MARKET-HOURS gate is deliberately NOT applied: this is the desk's manual
start button, and the bot already refuses new entries after square-off (15:10 MIS flatten,
square-off time from config/bot.yaml). The bot is also required for EXITS, and a market-hours
gate would make a position unmanageable outside 09:15-15:30.

USAGE
-----
    python scripts/ops/supervise_bot.py                      # paper, supervised
    python scripts/ops/supervise_bot.py --mode live          # live, still gated by bot.yaml
    python scripts/ops/supervise_bot.py --mode paper --once  # one attempt, no restart
    python scripts/ops/supervise_bot.py --no-preflight       # paper/debug only: skip the gates

Exit codes mirror the bot run: 0 clean stop, 4 safety halt (operator review).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

DEFAULT_HOST = "http://127.0.0.1:5000"
DEFAULT_JOURNAL = ROOT / "data" / "supervisor_journal.jsonl"

#: The two bot entry points this wrapper may supervise. Both already exist and are used.
BOT_SCRIPTS = {
    "paper": ROOT / "scripts" / "run" / "run_strategy_bot.py",
    "live": ROOT / "scripts" / "run" / "run_strategy_bot_live.py",
    # Sandbox (2026-11-27): the live runner with --sandbox; orders go to OpenAlgo's analyzer.
    "analyze": ROOT / "scripts" / "run" / "run_strategy_bot_live.py",
}


def bot_command(mode: str, base_url: str = DEFAULT_HOST, python: str | None = None,
                config: Path | None = None, limits: Path | None = None) -> list:
    """The exact command the supervisor will run for ``mode``. Pure; no process started."""
    if mode not in BOT_SCRIPTS:
        raise ValueError(f"unknown mode {mode!r}; expected one of {sorted(BOT_SCRIPTS)}")
    cmd = [python or sys.executable, str(BOT_SCRIPTS[mode]), "--base-url", base_url]
    if config is not None:
        cmd += ["--config", str(config)]
    if limits is not None:
        cmd += ["--limits", str(limits)]
    if mode == "analyze":
        cmd += ["--sandbox"]
    return cmd


def owner_kill_gate():
    """Preflight kill gate (Phase 3, audit items 2 and 13).

    Blocks when ANY owner kill file is on (dashboard, paper bot, live bot), and still applies the
    supervisor's own drawdown switch. Before this, the preflight read only the drawdown switch,
    so an operator kill did not stop the supervised start.
    """
    from quant.engines.core.supervisor import GateResult, kill_switch_gate
    from quant.risk.kill_switch import operator_kill_active
    active, reason = operator_kill_active(ROOT)
    if active:
        return GateResult("kill_switch", False, f"refused: {reason}")
    return kill_switch_gate()


#: Phase 2 (audit items 12, 18): the probe must be a TRADABLE EQUITY. NSE:NIFTY is an index;
#: a broker that quotes only equities failed the gate forever and the bot never started.
PREFLIGHT_SYMBOL = "RELIANCE"
PREFLIGHT_EXCHANGE = "NSE"


def build_gates(host: str = DEFAULT_HOST, symbol: str = PREFLIGHT_SYMBOL,
                exchange: str = PREFLIGHT_EXCHANGE, *, api_key: str | None = None):
    """The supervisor's gate chain for the bot run. Market hours are intentionally excluded.

    Fails CLOSED: if the OpenAlgo client cannot be built the gates cannot run, so the
    returned list starts with a gate that always blocks.
    """
    from quant.engines.core.supervisor import (GateResult, broker_session_gate,
                                               openalgo_reachable_gate,
                                               positionbook_gate, quote_gate,
                                               working_orders_gate)

    try:
        from quant.engines.core.client import init_client
        client = init_client(api_key=api_key, host=host)
    except Exception as exc:  # noqa: BLE001 - no client means no proof, so no start
        # Bind the message now: `exc` is cleared when the except block exits, so a lambda
        # that closed over it would raise NameError exactly when the gate fires.
        reason = f"cannot build the OpenAlgo client, entries cannot be verified: {exc!r}"
        return [lambda _reason=reason: GateResult("openalgo_client", False, _reason)]

    return [
        owner_kill_gate,
        lambda: openalgo_reachable_gate(host),
        lambda: broker_session_gate(client, symbol, exchange),
        lambda: positionbook_gate(client),
        lambda: working_orders_gate(client),
        lambda: quote_gate(client, symbol, exchange),
    ]


def main(argv: list[str] | None = None) -> int:
    from quant.engines.core.supervisor import (EXIT_SAFETY_STOP, Supervisor,
                                               SupervisorConfig)

    ap = argparse.ArgumentParser(description="Run the strategy bot under the live supervisor.")
    ap.add_argument("--mode", choices=sorted(BOT_SCRIPTS), default="paper",
                    help="paper (default) or live (live is still gated by bot.yaml)")
    ap.add_argument("--base-url", default=DEFAULT_HOST, help="OpenAlgo server URL")
    ap.add_argument("--config", type=Path, default=None, help="bot config YAML (default: repo)")
    ap.add_argument("--limits", type=Path, default=None, help="risk limits YAML (default: repo)")
    ap.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL, help="JSONL event journal")
    ap.add_argument("--once", action="store_true", help="one attempt; no restart on failure")
    ap.add_argument("--max-restarts", type=int, default=5)
    ap.add_argument("--restart-window", type=float, default=3600.0)
    ap.add_argument("--restart-delay", type=float, default=5.0)
    ap.add_argument("--no-preflight", action="store_true",
                    help="skip the safety gates (paper/debug only)")
    args = ap.parse_args(argv)

    script = BOT_SCRIPTS[args.mode]
    if not script.is_file():
        print(f"bot runner not found: {script}")
        return 2

    # Owner request 2026-11-27: screener session on every start. Moved here from the button's
    # runner (Phase 1): it runs inside the long-lived supervisor, so the dashboard button no
    # longer waits for the screener login. A failed login is reported, never fatal.
    from start_engine_now import ensure_screener_session  # scripts/ops is sys.path[0] when run
    session = ensure_screener_session()
    print(f"supervise_bot: screener session {session.get('action')} - {session.get('reason', '')}")

    command = bot_command(args.mode, args.base_url, config=args.config, limits=args.limits)
    cfg = SupervisorConfig(
        command=command,
        restart_delay_seconds=args.restart_delay,
        max_restarts=args.max_restarts,
        restart_window_seconds=args.restart_window,
        preflight_on_start=not args.no_preflight,
        journal_path=str(args.journal),
    )
    gates = [] if args.no_preflight else build_gates(args.base_url)

    print(f"supervise_bot: mode={args.mode} script={script.name}")
    print(f"supervise_bot: {' '.join(command)}")
    supervisor = Supervisor(cfg, gates=gates)
    if args.once:
        code = supervisor.run_once()
        print(f"supervise_bot: single run exit={code} state={supervisor.state}")
        return code

    report = supervisor.run()
    print(f"supervise_bot: state={report['state']} restarts={report['restarts']}")
    if report["halt_reason"]:
        print(f"supervise_bot: halted - {report['halt_reason']}")
    for event in report["events"][-10:]:
        print(f"  {event['ts']}  {event['kind']}  "
              f"{ {k: v for k, v in event.items() if k not in ('ts', 'kind')} }")
    return EXIT_SAFETY_STOP if report["state"] == "HALTED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
