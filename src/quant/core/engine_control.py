"""Manual engine start for the dashboard button.

Status: PHASE 3 IMPLEMENTED (start refusal checks; the supervisor's gates decide the rest)
Plan: SCHEDULER_PLAN.md (deleted 2026-11-20, in git history) (Engine section).

The button starts scripts/ops/supervise_bot.py, which runs the STRATEGY BOT under
scripts/../quant/engines/core/supervisor.py. That supervisor runs its own gates (kill
switch, OpenAlgo reachable, broker session, positionbook verifiable, working orders
reconciled, sane quote) before every (re)start. This module only refuses two things up
front: a tripped kill switch, and a second engine while one is already running.

WAS: it spawned scripts/run/run_supervised.py, which does not exist in this repo (it is
parked in Temp/Stag/old_runner/), so the button logged in to the broker and then stopped
silently with no engine and no bot. Fixed 2026-11-27.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parents[3]
PID_FILE = ROOT / "data" / "run" / "engine.pid"
LOG_FILE = ROOT / "Temp" / "run" / "engine_manual.log"
# The supervised BOT wrapper. run_supervised.py used to live here and never existed in a
# checkout, so start_engine() refused AFTER the broker login had already succeeded.
SUPERVISOR = ROOT / "scripts" / "ops" / "supervise_bot.py"


def decide_start(kill_active: bool, already_running: bool) -> Tuple[bool, str]:
    """Pure decision. (allowed, message)."""
    if kill_active:
        return False, "refused: kill switch is on. Reset it first (operator action)."
    if already_running:
        return False, "refused: an engine is already running."
    return True, "engine start requested"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, check=False).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_pid(pid_path: Path = PID_FILE) -> Optional[int]:
    """The live engine PID, or None. A stale PID file is removed."""
    if not pid_path.is_file():
        return None
    try:
        pid = int(json.loads(pid_path.read_text(encoding="utf-8")).get("pid", 0))
    except (OSError, ValueError, AttributeError):
        pid = 0
    if _pid_alive(pid):
        return pid
    try:
        pid_path.unlink()
    except OSError:
        pass
    return None


def start_engine(*, kill_active: bool, pid_path: Path = PID_FILE, log_path: Path = LOG_FILE,
                 supervisor: Path = SUPERVISOR, mode: str = "paper",
                 now: Optional[float] = None) -> Tuple[bool, str, Optional[int]]:
    """Spawn the supervisor detached. Returns (ok, message, pid).

    ``mode`` is passed straight to the wrapper as ``--mode``; it decides WHICH bot entry
    point the supervisor runs (paper: run_strategy_bot.py, live: run_strategy_bot_live.py).
    It does not relax anything: a live run is still refused by the bot unless config/bot.yaml
    says ``mode: live`` AND ``live_enabled: true`` AND the live approval + limits hash + a
    passing startup reconciliation all hold.
    """
    existing = running_pid(pid_path)
    allowed, message = decide_start(kill_active, existing is not None)
    if not allowed:
        return False, message, existing
    if not supervisor.is_file():
        return False, f"refused: supervisor not found at {supervisor}", None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        log.write(f"\n--- manual start ({mode}) {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
        kwargs = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL, "cwd": str(ROOT)}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([sys.executable, str(supervisor), "--mode", mode], **kwargs)
    pid_path.write_text(json.dumps({"pid": proc.pid, "started": int(now or time.time()),
                                    "mode": mode}) + "\n",
                        encoding="utf-8")
    return True, (f"bot start requested ({mode} mode, pid {proc.pid}); "
                  f"its gates decide whether it trades"), proc.pid


def stop_engine(*, pid_path: Path = PID_FILE) -> Tuple[bool, str, Optional[int]]:
    """Stop the engine process only. Returns (ok, message, pid).

    This does NOT log out of the broker, does NOT change the kill switch and does NOT
    change any open order or position. It sends a terminate signal to the PID in the
    PID file, then removes the file. Open positions are left as they are.
    """
    pid = running_pid(pid_path)
    if pid is None:
        return False, "refused: no engine is running", None
    import signal as _signal
    try:
        os.kill(pid, _signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        return False, f"refused: could not signal engine pid {pid}: {exc}", pid
    try:
        pid_path.unlink()
    except OSError:
        pass
    return True, f"engine stop sent (pid {pid}). Broker session and positions are not changed.", pid


MANUAL_LOG = ROOT / "Temp" / "run" / "manual_start.log"
RUNNER = ROOT / "scripts" / "ops" / "start_engine_now.py"
BOT_CONFIG = ROOT / "config" / "bot.yaml"


def bot_mode(config_path: Path = BOT_CONFIG) -> str:
    """The mode the dashboard's START ENGINE should request: 'paper' or 'live'.

    Read from `config/bot.yaml` so the button and the bot cannot disagree. Live is reported
    only when BOTH `mode: live` and `live_enabled: true` are set; anything else - a missing
    file, a parse error, `analyze` - reports 'paper'. Fail closed: an unreadable config must
    never turn the button into a live-order button by accident.

    This does not decide where orders go. The OpenAlgo analyzer (sandbox) switch does that.
    """
    try:
        import yaml
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - unreadable config -> paper, never a guess
        return "paper"
    if not isinstance(raw, dict):
        return "paper"
    live = str(raw.get("mode", "paper")).strip().lower() == "live"
    enabled = raw.get("live_enabled") is True
    return "live" if (live and enabled) else "paper"


def request_manual_start(*, kill_active: bool, mode: str = "paper", pid_path: Path = PID_FILE,
                         log_path: Path = MANUAL_LOG,
                         runner: Path = RUNNER) -> Tuple[bool, str, Optional[int]]:
    """Button press: start the bot. NO broker login here (owner decision 2026-11-27).

    The OpenAlgo + broker login runs on the DASHBOARD SIGN-IN (broker_signin.login_after_signin).
    Logging in again from this button would hit the broker cooldown for a session the operator
    has just created, so this path only starts the bot.

    Refused when the kill switch is on or a bot is already running. `mode` (paper|live) is
    handed to the supervisor, which picks the runner; it does not relax any gate.
    """
    if mode not in ("paper", "live"):
        return False, f"refused: unknown mode {mode!r} (expected paper or live)", None
    existing = running_pid(pid_path)
    allowed, message = decide_start(kill_active, existing is not None)
    if not allowed:
        return False, message, existing
    if not runner.is_file():
        return False, f"refused: runner not found at {runner}", None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        log.write(f"\n--- manual start ({mode}) {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
        kwargs = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL, "cwd": str(ROOT)}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([sys.executable, str(runner), "--mode", mode], **kwargs)
    return True, f"bot start requested ({mode} mode; progress in {log_path.name})", proc.pid
