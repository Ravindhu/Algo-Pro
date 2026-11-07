"""Paper loop process. Started by the dashboard START button (decision 0029). Paper only.

Each cycle: if the NSE cash session is open, run run_paper.run() on data/paper/signals.csv (if the file exists).
It checks a stop flag every second and writes a heartbeat each cycle. STOP ends it; it does not close positions.

Usage: python scripts/run/paper_loop.py --state-dir data/paper/loop [--interval 60]
Flow stage: Paper trading stage
Status: PHASE 3 IMPLEMENTED (paper only; no live path)
"""
import argparse
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from quant.audit.log import AuditLog  # noqa: E402
from quant.core.calendar import TradingCalendar  # noqa: E402
from quant.monitor.paper_loop import STOP_FILE, now_ist, session_reason, write_state  # noqa: E402
import run_paper  # noqa: E402  (scripts/run_paper.py)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state-dir", required=True)
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--signals", default=str(ROOT / "data" / "paper" / "signals.csv"))
    p.add_argument("--out", default=str(ROOT / "data" / "paper"))
    p.add_argument("--limits", default=str(ROOT / "config" / "risk_limits.yaml"))
    p.add_argument("--calendar", default=str(ROOT / "config" / "calendar" / "holidays.csv"))
    a = p.parse_args()

    state_dir = pathlib.Path(a.state_dir)
    interval = max(5, a.interval)
    cal = TradingCalendar.from_csv(a.calendar) if pathlib.Path(a.calendar).exists() \
        else TradingCalendar(set(), verified=False)
    stop_flag = state_dir / STOP_FILE
    audit = AuditLog(pathlib.Path(a.out) / "audit.jsonl")

    write_state(state_dir, status="running", pid=os.getpid(), heartbeat_ts=time.time(),
                interval_s=interval, last_error=None)
    audit.append("loop_started", {"pid": os.getpid(), "interval_s": interval, "mode": "paper"})

    while not stop_flag.exists():
        now = now_ist()
        is_open, reason = session_reason(now, cal)
        result, error = None, None
        try:
            if not is_open:
                result = f"idle: {reason}"
            elif not pathlib.Path(a.signals).exists():
                result = "no signals file"
            else:
                # Paper broker only: run() without a broker object uses the in-process paper broker.
                s = run_paper.run(a.signals, a.limits, a.out)
                result = f"ran: approved {s['approved']}, filled {s['filled']}, rejected {s['rejected']}, blocked {s['blocked']}"
        except Exception as exc:  # keep the loop alive; the error is shown on the page and audited
            error = repr(exc)[:300]
            audit.append("loop_error", {"error": error})
        write_state(state_dir, heartbeat_ts=time.time(), session=("open" if is_open else reason),
                    last_cycle=now.isoformat(timespec="seconds"), last_result=result, last_error=error)
        waited = 0
        while waited < interval and not stop_flag.exists():
            time.sleep(1)
            waited += 1

    write_state(state_dir, status="stopped", heartbeat_ts=time.time(), last_result="stopped by request")
    stop_flag.unlink(missing_ok=True)
    audit.append("loop_stopped", {"pid": os.getpid()})


if __name__ == "__main__":
    main()
