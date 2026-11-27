r"""start_engine_now.py - the dashboard START ENGINE button: broker login first, then the engine.

This replaces the manual path of the old pre-open job. There is no timed run any more: this
script runs only when the owner presses START ENGINE. Order is fixed:
  1. broker login (the platform login script, with the engine turned off)
  2. engine start (engine_control.start_engine, which checks the kill switch and "already running")
A failed login means the engine does not start.

    python scripts/ops/start_engine_now.py      # normally started by the dashboard button
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent


def script_command(system_name: str, root: Path, python: str) -> List[str]:
    """The platform login job (engine off). Windows: the cmd wrapper. Ubuntu: the shell launcher."""
    if system_name == "Windows":
        return ["cmd", "/c", str(root / "scripts" / "ops" / "SCHEDULED_PREOPEN.cmd")]
    return ["/bin/bash", str(root / "scripts" / "ops" / "preopen_launch.sh"), "--runner-gated"]


def login_env(base: Optional[dict] = None) -> dict:
    """Login is allowed for this run only, and the login job must not start the engine."""
    return dict(base if base is not None else os.environ, BROKER_AUTOLOGIN="1", PREOPEN_ENGINE_ALLOWED="0")


def run(*, root: Path = ROOT, system_name: Optional[str] = None, python: Optional[str] = None,
        runner: Callable = subprocess.run, start: Optional[Callable] = None,
        kill_active: Optional[bool] = None) -> int:
    from quant.core import engine_control
    from quant.risk.kill_switch import KillSwitch

    system_name = system_name or platform.system()
    python = python or sys.executable
    print("start-engine: broker login first")
    login = runner(script_command(system_name, root, python), cwd=str(root), env=login_env(), check=False)
    if login.returncode != 0:
        print(f"broker login failed (exit {login.returncode}) - engine NOT started")
        return login.returncode
    if kill_active is None:
        kill_active = KillSwitch(root / "data" / "paper" / "kill_switch.json").active
    ok, message, _pid = (start or engine_control.start_engine)(kill_active=kill_active)
    print(message)
    return 0 if ok else 4


def main(argv: Optional[List[str]] = None) -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
