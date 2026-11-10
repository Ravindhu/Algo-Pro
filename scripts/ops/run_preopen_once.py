"""Run the pre-open job once per trading day, even if the machine was off at the start time.

Plan: docs/SCHEDULER_PLAN.md (Missed runs, 2026-11-10).

Both triggers call this file: the daily start-time trigger and the power-on or
logon trigger. Whichever comes first does the work. The other one finds today's
marker and does nothing. Rules, all in IST:

* Not a trading day (weekend or NSE holiday) -> skip, and say so.
* Before the start time -> wait until it, then run.
* After the start time but before the close (15:30) -> run now (catch-up).
* After 15:30 -> skip today, and say so. Nothing is retried.
* Broker login always runs when the job runs.
* The engine starts only when auto engine start is ON and the run is before 09:15.
  Otherwise the engine waits for the dashboard button.

    python scripts/ops/run_preopen_once.py            # the scheduled path
    python scripts/ops/run_preopen_once.py --dry-run  # print the decision, run nothing
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import csv  # noqa: E402
from quant.core.schedule_settings import load  # noqa: E402

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))   # IST has no DST
ENGINE_CUTOFF = _dt.time(9, 15)                           # engine auto start only before the open
MARKET_CLOSE = _dt.time(15, 30)
RUN_DIR = ROOT / "data" / "run"
CALENDAR = ROOT / "config" / "calendar" / "holidays.csv"


def load_holidays(path: Path = CALENDAR) -> set:
    """NSE holiday dates from the CSV (stdlib only, so no pandas is needed). Missing file = none."""
    if not path.is_file():
        return set()
    with path.open(encoding="utf-8", newline="") as stream:
        return {_dt.date.fromisoformat(row["date"].strip())
                for row in csv.DictReader(stream) if row.get("date", "").strip()}


# --------------------------------------------------------------- pure logic
def plan_run(now: _dt.datetime, start: _dt.time, trading_day: bool, already_done: bool,
             engine_toggle: bool) -> Dict[str, object]:
    """What to do right now. Pure: no clock read, no file touched."""
    if not trading_day:
        return {"action": "skip", "wait_seconds": 0.0, "engine_allowed": False,
                "reason": "not a trading day (weekend or NSE holiday)"}
    if already_done:
        return {"action": "skip", "wait_seconds": 0.0, "engine_allowed": False,
                "reason": "already ran today (marker present)"}
    if now.time() > MARKET_CLOSE:
        return {"action": "skip", "wait_seconds": 0.0, "engine_allowed": False,
                "reason": "after 15:30 - today's pre-open run is not made up"}
    if now.time() < start:
        wait = (_dt.datetime.combine(now.date(), start, tzinfo=now.tzinfo) - now).total_seconds()
        return {"action": "wait", "wait_seconds": max(0.0, wait), "engine_allowed": False,
                "reason": f"waiting until {start.strftime('%H:%M')} IST"}
    engine_ok = bool(engine_toggle) and now.time() < ENGINE_CUTOFF
    reason = ("catch-up: started after the start time" if now.time() > start
              else "start time reached")
    if engine_toggle and not engine_ok:
        reason += "; after 09:15, engine left for the dashboard button"
    elif not engine_toggle:
        reason += "; auto engine start is OFF"
    return {"action": "run", "wait_seconds": 0.0, "engine_allowed": engine_ok, "reason": reason}


def script_command(system_name: str, root: Path, python: str) -> List[str]:
    """The platform job that does the login and engine start."""
    if system_name == "Windows":
        return ["cmd", "/c", str(root / "scripts" / "ops" / "SCHEDULED_PREOPEN.cmd")]
    return ["/bin/bash", str(root / "scripts" / "ops" / "preopen_launch.sh"), "--runner-gated"]


# ------------------------------------------------------------ side effects
def ist_now() -> _dt.datetime:
    return _dt.datetime.now(IST)


def marker_path(day: _dt.date, run_dir: Path = RUN_DIR) -> Path:
    return run_dir / f"preopen_done_{day.isoformat()}.json"


def claim_marker(path: Path, payload: Dict[str, object]) -> bool:
    """Create today's marker atomically. False when another run already claimed it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    return True


def _update_marker(path: Path, payload: Dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _wait(seconds: float) -> None:
    """Sleep in short steps so a long wait stays responsive and never sleeps past the target."""
    deadline = time.monotonic() + seconds
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(60.0, left))


def run_manual() -> int:
    """Login first (the platform script with the engine turned off), then start the engine.

    Only a successful login starts the engine. The manual path does not write the
    daily marker, so the scheduled run is not affected.
    """
    from quant.core import engine_control
    from quant.risk.kill_switch import KillSwitch

    env = dict(os.environ, BROKER_AUTOLOGIN="1", PREOPEN_ENGINE_ALLOWED="0")
    print(f"[{ist_now().isoformat(timespec='seconds')}] manual: broker login first")
    login = subprocess.run(script_command(platform.system(), ROOT, sys.executable),
                           cwd=str(ROOT), env=env, check=False)
    if login.returncode != 0:
        print(f"broker login failed (exit {login.returncode}) - engine NOT started")
        return login.returncode
    ok, message, pid = engine_control.start_engine(
        kill_active=KillSwitch(ROOT / "data" / "paper" / "kill_switch.json").active)
    print(message)
    return 0 if ok else 4


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the pre-open job once per trading day.")
    parser.add_argument("--dry-run", action="store_true", help="print the decision, run nothing")
    parser.add_argument("--manual", action="store_true",
                        help="dashboard button: login now, then start the engine (no day or time rules)")
    args = parser.parse_args(argv)

    settings, warning = load()
    if warning:
        print(f"WARN: {warning}")
    if args.manual:
        return run_manual()
    start = _dt.time(*map(int, settings.start_time.split(":")))
    now = ist_now()
    holidays = load_holidays()
    trading = now.weekday() < 5 and now.date() not in holidays
    marker = marker_path(now.date())
    decision = plan_run(now, start, trading, marker.exists(), settings.engine_start)
    print(f"[{now.isoformat(timespec='seconds')}] {decision['action']}: {decision['reason']}")
    if args.dry_run or decision["action"] == "skip":
        return 0

    if decision["action"] == "wait":
        _wait(float(decision["wait_seconds"]))
        now = ist_now()
        decision = plan_run(now, start, trading, marker.exists(), settings.engine_start)
        if decision["action"] != "run":
            print(f"after waiting: {decision['action']}: {decision['reason']}")
            return 0

    claimed = {"started_at": now.isoformat(timespec="seconds"), "engine_allowed": decision["engine_allowed"],
               "reason": decision["reason"], "state": "in_progress"}
    if not claim_marker(marker, claimed):
        print("another run already holds today's marker - not starting a second login")
        return 0

    env = dict(os.environ, PREOPEN_ENGINE_ALLOWED="1" if decision["engine_allowed"] else "0")
    argv_cmd = script_command(platform.system(), ROOT, sys.executable)
    result = subprocess.run(argv_cmd, cwd=str(ROOT), env=env, check=False)
    _update_marker(marker, {**claimed, "state": "finished" if result.returncode == 0 else "failed",
                            "exit": result.returncode,
                            "finished_at": ist_now().isoformat(timespec="seconds")})
    print(f"pre-open job finished with exit {result.returncode}")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
