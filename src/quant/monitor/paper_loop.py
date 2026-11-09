"""Paper loop control (decision 0029). Runs run_paper.run() on a signals file during NSE cash hours.

START and STOP on the dashboard use these functions. The loop is paper-only: it never uses analyze or live.
Signals are read from data/paper/signals.csv. No live signal source exists yet, so until a file is there,
each cycle only records "no signals file". STOP ends the loop. It does not close positions: the kill switch does that.
Session: a trading day in config/calendar/holidays.csv, 09:15 to 15:30 IST. Special sessions (Muhurat) are not traded.

Flow stage: Paper trading stage
Status: PHASE 3 IMPLEMENTED (paper only; no live path)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from quant.core.calendar import TradingCalendar

IST = timezone(timedelta(hours=5, minutes=30))  # India has no DST, so a fixed offset is exact
OPEN_T = dtime(9, 15)
CLOSE_T = dtime(15, 30)
STATE_FILE = "state.json"
STOP_FILE = "stop.flag"
STALE_FACTOR = 3  # a heartbeat older than this many intervals means the loop is not running


def now_ist() -> datetime:
    return datetime.now(IST)


def session_reason(now: datetime, cal: TradingCalendar) -> Tuple[bool, str]:
    """(is_open, reason). `now` is IST local time. Fails closed when the calendar is not verified."""
    if not cal.verified:
        return False, "holiday calendar not verified"
    if now.date().weekday() >= 5:
        return False, "weekend"
    if now.date() in cal.holidays:
        return False, "exchange holiday"
    t = now.time()
    if t < OPEN_T:
        return False, "before 09:15 IST"
    if t >= CLOSE_T:
        return False, "after 15:30 IST"
    return True, "NSE cash session"


def _read(state_dir: Path) -> dict:
    try:
        return json.loads((state_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state_dir: Path, **fields) -> None:
    """Merge fields into state.json. Written to a temp file first, then swapped in."""
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {**_read(state_dir), **fields}
    tmp = state_dir / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, state_dir / STATE_FILE)


def loop_status(state_dir: Path, now_ts: float, interval_s: int) -> Dict[str, object]:
    st = _read(state_dir)
    hb = st.get("heartbeat_ts")
    fresh = hb is not None and now_ts - float(hb) < STALE_FACTOR * interval_s
    running = fresh and st.get("status") in ("starting", "running")
    return {
        "status": "running" if running else "stopped",
        "pid": st.get("pid") if running else None,
        "session": st.get("session"),
        "last_cycle": st.get("last_cycle"),
        "last_result": st.get("last_result"),
        "last_error": st.get("last_error"),
        "stop_requested": (state_dir / STOP_FILE).exists(),
        "mode": "paper",
    }


def start_loop(state_dir: Path, kill_active: bool, interval_s: int,
               spawn: Callable[[], object], now_ts: Optional[float] = None) -> Tuple[bool, str]:
    """Refused while the kill switch is on or a loop is already running. `spawn` starts the process."""
    now_ts = time.time() if now_ts is None else now_ts
    if kill_active:
        return False, "Kill switch is on. Reset it before starting the loop."
    if loop_status(state_dir, now_ts, interval_s)["status"] == "running":
        return False, "Loop is already running."
    (state_dir / STOP_FILE).unlink(missing_ok=True)
    # Mark it as starting now, so a second START is refused before the new process writes its heartbeat.
    write_state(state_dir, status="starting", heartbeat_ts=now_ts, started_ts=now_ts,
                interval_s=interval_s, last_error=None)
    try:
        spawn()
    except OSError as exc:
        write_state(state_dir, status="stopped", last_error=f"could not start: {exc}")
        return False, f"Could not start the loop: {exc}"
    return True, "Loop start requested. The status updates within one cycle."


def stop_loop(state_dir: Path, interval_s: int, now_ts: Optional[float] = None) -> Tuple[bool, str]:
    now_ts = time.time() if now_ts is None else now_ts
    if loop_status(state_dir, now_ts, interval_s)["status"] != "running":
        return True, "Loop is not running."
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / STOP_FILE).write_text("stop", encoding="utf-8")
    return True, ("Stop requested. The loop stops within a few seconds. "
                  "Open positions are not closed here; the kill switch does that.")


def spawn_loop(root: Path, state_dir: Path, interval_s: int) -> subprocess.Popen:
    """Start scripts/run/paper_loop.py as a background process that outlives the web request."""
    cmd = [sys.executable, str(root / "scripts" / "run" / "paper_loop.py"),
           "--state-dir", str(state_dir), "--interval", str(interval_s)]
    kwargs = {"cwd": str(root), "stdin": subprocess.DEVNULL,
              "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)
