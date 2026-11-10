"""Manual engine start for the dashboard button.

Plan: docs/SCHEDULER_PLAN.md (Engine section).

The button starts scripts/run/run_supervised.py, which runs its own six gates
(kill switch, OpenAlgo reachable, positionbook verifiable, and so on) before the
engine starts. This module only refuses two things up front: a tripped kill
switch, and a second engine while one is already running.
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
SUPERVISOR = ROOT / "scripts" / "run" / "run_supervised.py"


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
                 supervisor: Path = SUPERVISOR, now: Optional[float] = None) -> Tuple[bool, str, Optional[int]]:
    """Spawn the supervisor detached. Returns (ok, message, pid)."""
    existing = running_pid(pid_path)
    allowed, message = decide_start(kill_active, existing is not None)
    if not allowed:
        return False, message, existing
    if not supervisor.is_file():
        return False, f"refused: supervisor not found at {supervisor}", None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        log.write(f"\n--- manual start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
        kwargs = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL, "cwd": str(ROOT)}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([sys.executable, str(supervisor)], **kwargs)
    pid_path.write_text(json.dumps({"pid": proc.pid, "started": int(now or time.time())}) + "\n",
                        encoding="utf-8")
    return True, f"engine started (pid {proc.pid}); its gates decide whether it trades", proc.pid
