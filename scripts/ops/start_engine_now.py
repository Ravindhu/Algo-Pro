r"""start_engine_now.py - the dashboard START ENGINE button: start the bot.

WHAT THIS IS NOW (owner decision 2026-11-27)
-------------------------------------------
START ENGINE starts the BOT. It does not log anyone in.

The broker + OpenAlgo login moved to the DASHBOARD SIGN-IN: a successful dashboard sign-in
already runs `quant.core.broker_signin.login_after_signin` in a background thread, which calls
`broker_auth.run_scheduled_login` (OpenAlgo session, then broker TOTP). That path was built
2026-11-24 and is unchanged. Doing the login here as well would hit the broker cooldown for a
session the operator had just created, so the login step was removed.

WHERE ORDERS GO
---------------
Nothing in this script decides that. The bot sends through `execution/openalgo_client.py`,
which refuses unless it can positively read OpenAlgo's analyzer (sandbox) switch:
    analyzer ON  -> orders stay in the OpenAlgo sandbox (fake money)
    analyzer OFF -> orders reach the broker (real money)
The switch is the owner's, in the OpenAlgo UI. `--mode` only picks which bot entry point runs:
    paper -> scripts/run/run_strategy_bot.py       (PaperSender: simulated, never leaves the process)
    live  -> scripts/run/run_strategy_bot_live.py  (LiveSender: OpenAlgo decides sandbox or real)

A failed gate means the bot does not start. There is no fallback and no retry loop here; the
supervisor inside scripts/ops/supervise_bot.py owns restarts and halts.

    python scripts/ops/start_engine_now.py                # paper (default)
    python scripts/ops/start_engine_now.py --mode live    # live / sandbox per the analyzer switch

Exit codes: 0 started, 4 refused by a gate.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def login_env(base: Optional[dict] = None) -> dict:
    """The environment for a LOGIN run. Kept for the pre-open job; START ENGINE does not call it.

    BROKER_AUTOLOGIN=1 allows the login, PREOPEN_ENGINE_ALLOWED=0 stops the login job from
    also starting an engine.
    """
    import os
    return dict(base if base is not None else os.environ,
                BROKER_AUTOLOGIN="1", PREOPEN_ENGINE_ALLOWED="0")


def ensure_screener_session(source: Optional[Path] = None,
                            runner: Optional[Callable[[], dict]] = None) -> dict:
    """Make the Screener.in session usable for THIS start. Never raises, never blocks the bot.

    Owner request 2026-11-27: "screener session on every start when login is needed".
    `screen_refresh.ensure_session` already knows when a login is needed and is cheap when it
    is not: a session saved in the current trading session is reused with NO network call, and
    a marker written before today's 09:00 IST boundary no longer counts. So calling it on every
    START ENGINE costs nothing in the common case and refreshes exactly when needed.

    A refused login is REPORTED, not fatal: the bot must still start so it can manage exits.
    """
    try:
        from quant.data.screen_refresh import ensure_session
        note = (runner or ensure_session)()
    except Exception as exc:  # noqa: BLE001 - a screener problem must never stop the bot
        return {"usable": False, "action": "failed", "reason": f"{type(exc).__name__}: {exc}"[:160]}
    return note if isinstance(note, dict) else {"usable": False, "action": "failed",
                                                 "reason": "session step returned no note"}


def run(*, root: Path = ROOT, system_name: Optional[str] = None, python: Optional[str] = None,
        mode: str = "paper", runner: Callable = None, start: Optional[Callable] = None,
        kill_active: Optional[bool] = None, session_runner: Optional[Callable] = None) -> int:
    """Start the bot under the supervisor. Returns a process exit code.

    `runner`, `start` and `session_runner` are test seams. In production only
    `engine_control.start_engine` runs, which spawns the supervisor detached.
    """
    from quant.core import engine_control

    if kill_active is None:
        from quant.risk.kill_switch import KillSwitch
        kill_active = KillSwitch(root / "data" / "paper" / "kill_switch.json").active

    # Screener session FIRST, so the bot's first cycle has a usable universe. Not fatal.
    session = ensure_screener_session(runner=session_runner)
    print(f"start-engine: screener session {session.get('action')} - {session.get('reason', '')}")

    print(f"start-engine: mode={mode} (broker login happens at dashboard sign-in)")
    started = start or engine_control.start_engine
    ok, message, pid = started(kill_active=kill_active, mode=mode)
    print(message)
    return 0 if ok else 4


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Start the strategy bot (dashboard START ENGINE).")
    ap.add_argument("--mode", choices=("paper", "live", "analyze"), default="paper",
                    help="paper (default) or live; live still obeys bot.yaml and the OpenAlgo analyzer")
    args = ap.parse_args(argv)
    return run(mode=args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
