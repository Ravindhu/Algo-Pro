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
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, check=False).stdout
        # Exact match on the quoted PID column: a plain substring test made PID 12 match 1234.
        return any(f'"{pid}"' in line for line in out.splitlines())
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


START_LOCK_STALE_SECONDS = 60.0


def _start_lock_path(pid_path: Path) -> Path:
    return pid_path.with_name("engine.lock")


def _acquire_start_lock(lock_path: Path, now: Optional[float] = None) -> bool:
    """Take the start lock. False if another start is in progress.

    The lock is held from the 'already running?' check until the PID file is written, so two
    START presses cannot both pass the check (audit bugs 8 and 20). A lock whose holder is
    dead, or that is older than START_LOCK_STALE_SECONDS, is cleared.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                holder = int(lock_path.read_text(encoding="utf-8").strip() or 0)
                age = (now or time.time()) - lock_path.stat().st_mtime
            except (OSError, ValueError):
                holder, age = 0, START_LOCK_STALE_SECONDS + 1
            if _pid_alive(holder) and age < START_LOCK_STALE_SECONDS:
                return False
            try:
                lock_path.unlink()
            except OSError:
                return False
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return True
    return False


def _release_start_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except OSError:
        pass


def _spawn_supervisor(*, mode: str, pid_path: Path, log_path: Path, supervisor: Path,
                      now: Optional[float] = None) -> int:
    """The ONE spawn path for the bot. The PID written is the supervisor's (the long-lived process).

    The supervisor leads its own session on POSIX, so stop_engine can signal the whole group,
    which includes the bot child the supervisor started.
    """
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
    return proc.pid


def start_engine(*, kill_active: bool, pid_path: Path = PID_FILE, log_path: Path = LOG_FILE,
                 supervisor: Path = SUPERVISOR, mode: str = "paper",
                 now: Optional[float] = None) -> Tuple[bool, str, Optional[int]]:
    """Spawn the supervisor detached. Returns (ok, message, pid).

    ``mode`` is passed straight to the supervisor as ``--mode``; it decides WHICH bot entry
    point the supervisor runs (paper: run_strategy_bot.py, live: run_strategy_bot_live.py).
    It does not relax anything: a live run is still refused by the bot unless config/bot.yaml
    says ``mode: live`` AND ``live_enabled: true`` AND the live approval + limits hash + a
    passing startup reconciliation all hold.
    """
    if not supervisor.is_file():
        return False, f"refused: supervisor not found at {supervisor}", None
    lock = _start_lock_path(pid_path)
    if not _acquire_start_lock(lock, now=now):
        return False, "refused: an engine start is already in progress.", None
    try:
        existing = running_pid(pid_path)
        # Phase 3 (audit items 2, 13): every owner kill file blocks a start, not only the paper one.
        from quant.risk.kill_switch import operator_kill_active
        any_kill, kill_reason = operator_kill_active(ROOT)
        allowed, message = decide_start(bool(kill_active or any_kill), existing is not None)
        if not allowed:
            return False, (message if not any_kill else f"refused: {kill_reason}"), existing
        pid = _spawn_supervisor(mode=mode, pid_path=pid_path, log_path=log_path,
                                supervisor=supervisor, now=now)
    finally:
        _release_start_lock(lock)
    return True, (f"bot start requested ({mode} mode, pid {pid}); "
                  f"its gates decide whether it trades"), pid


STOP_WAIT_SECONDS = 20.0


def _signal_engine(pid: int, sig: int) -> None:
    """Send ``sig`` to the supervisor and, on POSIX, to its whole process group (the bot too)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, check=False)
        return
    try:
        is_group_leader = os.getpgid(pid) == pid
    except ProcessLookupError:
        return
    if is_group_leader:
        os.killpg(pid, sig)   # the supervisor started its own session; this also reaches the bot
    else:
        os.kill(pid, sig)     # never signal a group we did not start


def _wait_for_exit(pid: int, timeout: float) -> bool:
    """True once the process has exited. Reaps it first if it is our own child."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return True
        except OSError:
            pass   # not our child (the normal case for the detached supervisor)
        if not _pid_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def stop_engine(*, pid_path: Path = PID_FILE,
                wait_seconds: float = STOP_WAIT_SECONDS) -> Tuple[bool, str, Optional[int]]:
    """Stop the engine process only. Returns (ok, message, pid).

    This does NOT log out of the broker, does NOT change the kill switch and does NOT
    change any open order or position. It sends a terminate signal to the supervisor (and its
    process group), then waits for the process to exit. The PID file is removed ONLY after the
    exit is confirmed. If the engine is still running after the wait, the PID file is kept so
    the dashboard still shows it as running and a second start stays refused.
    """
    pid = running_pid(pid_path)
    if pid is None:
        return False, "refused: no engine is running", None
    import signal as _signal
    try:
        _signal_engine(pid, _signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        return False, f"refused: could not signal engine pid {pid}: {exc}", pid
    if not _wait_for_exit(pid, wait_seconds):
        return False, (f"stop sent to engine pid {pid}, but it is still running after "
                       f"{wait_seconds:.0f}s. PID file kept: the engine is NOT confirmed stopped. "
                       "Check it before starting again."), pid
    try:
        pid_path.unlink()
    except OSError:
        pass
    return True, f"engine stopped (pid {pid}). Broker session and positions are not changed.", pid


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
                         runner: Optional[Path] = None) -> Tuple[bool, str, Optional[int]]:
    """Button press: start the bot. NO broker login here (owner decision 2026-11-27).

    The OpenAlgo + broker login runs on the DASHBOARD SIGN-IN (broker_signin.login_after_signin).
    Logging in again from this button would hit the broker cooldown for a session the operator
    has just created, so this path only starts the bot.

    Refused when the kill switch is on or a bot is already running. `mode` (paper|live|analyze)
    is handed to the supervisor; it does not relax any gate.

    Phase 1 (2026-11-27): this used to spawn scripts/ops/start_engine_now.py, which wrote the PID
    file only after a slow screener login, so a second press in that window started a second bot.
    It now uses the same single start path as start_engine (lock, then PID written at once).
    The `runner` argument is accepted for old callers and ignored.
    """
    if mode not in ("paper", "live", "analyze"):
        return False, f"refused: unknown mode {mode!r} (expected paper, live or analyze)", None
    return start_engine(kill_active=kill_active, pid_path=pid_path, log_path=log_path,
                        supervisor=SUPERVISOR, mode=mode)
