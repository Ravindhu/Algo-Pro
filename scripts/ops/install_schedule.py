"""Install or remove the pre-open schedule on THIS machine only.

Plan: docs/SCHEDULER_PLAN.md (D4-D8).

* Windows -> one Task Scheduler job (BlueEngine\\PreOpen). No cron.
* Ubuntu  -> one cron file (/etc/cron.d/mts-preopen). No Task Scheduler.
* The machine is refused when its clock is not IST or it does not match an
  explicit os_target. Nothing is written in that case.

Default is a dry run that prints the plan. Pass --apply to change the machine.

    python scripts/ops/install_schedule.py --status
    python scripts/ops/install_schedule.py            # dry run
    python scripts/ops/install_schedule.py --apply    # install or remove
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.core.schedule_settings import (  # noqa: E402
    DEFAULT_PATH,
    SCHEDULER_LABEL,
    ScheduleSettings,
    load,
)

IST_OFFSET_SECONDS = 19800
CRON_FILE = Path("/etc/cron.d/mts-preopen")
WINDOWS_TASK = "BlueEngine\\PreOpen"
OLD_WINDOWS_TASK = "BlueEngine\\BrokerLogin"       # retired by D6
UBUNTU_TIMER = "pov-preopen.timer"


# --------------------------------------------------------------- pure logic
def detect_os(system_name: str, has_systemd: bool) -> str:
    """'windows', 'ubuntu', or 'unsupported'. Pure: the caller passes facts in."""
    if system_name == "Windows":
        return "windows"
    if system_name == "Linux" and has_systemd:
        return "ubuntu"
    return "unsupported"


def resolve_target(settings: ScheduleSettings, detected: str) -> Optional[str]:
    """The OS this machine will schedule for, or None (refuse)."""
    if detected not in ("windows", "ubuntu"):
        return None
    if settings.os_target == "auto":
        return detected
    return settings.os_target if settings.os_target == detected else None


def clock_is_ist(utc_offset_seconds: int) -> bool:
    return int(utc_offset_seconds) == IST_OFFSET_SECONDS


def cron_file_text(settings: ScheduleSettings, root: Path, user: str) -> str:
    """The whole /etc/cron.d file. CRON_TZ pins the schedule to IST."""
    hour, minute = settings.start_time.split(":")
    login = "1" if settings.broker_login else "0"
    engine_flag = "" if settings.engine_start else " --no-engine"
    job = (
        f"BROKER_AUTOLOGIN={login} /bin/bash {root}/scripts/ops/preopen_launch.sh"
        f"{engine_flag} >> {root}/log/preopen/cron.log 2>&1"
    )
    return (
        "# managed by scripts/ops/install_schedule.py - do not edit by hand\n"
        "CRON_TZ=Asia/Kolkata\n"
        f"{int(minute)} {int(hour)} * * 1-5 {user} cd {root} && {job}\n"
    )


def schtasks_create_args(settings: ScheduleSettings, root: Path) -> List[str]:
    """schtasks argument list for the Windows pre-open job."""
    flags = f"{1 if settings.broker_login else 0} {1 if settings.engine_start else 0}"
    script = root / "scripts" / "ops" / "SCHEDULED_PREOPEN.cmd"
    run = f'cmd /c ""{script}" {flags}"'
    return ["schtasks", "/Create", "/TN", WINDOWS_TASK, "/SC", "WEEKLY",
            "/D", "MON,TUE,WED,THU,FRI", "/ST", settings.start_time,
            "/TR", run, "/RL", "LIMITED", "/F"]


def schtasks_delete_args(name: str) -> List[str]:
    return ["schtasks", "/Delete", "/TN", name, "/F"]


def build_plan(settings: ScheduleSettings, *, detected: str,
               utc_offset_seconds: int, root: Path = ROOT,
               user: str = "algo") -> Dict[str, object]:
    """Decide what would change. Pure: no file or command is touched here."""
    base: Dict[str, object] = {
        "enabled": settings.enabled, "detected_os": detected,
        "scheduler": None, "actions": [], "refused": None,
    }
    if not settings.valid:
        base["refused"] = "settings invalid: " + "; ".join(settings.errors())
        return base
    target = resolve_target(settings, detected)
    if target is None:
        base["refused"] = (f"this machine is {detected!r} but settings target "
                           f"{settings.os_target!r}; nothing installed here")
        return base
    if not clock_is_ist(utc_offset_seconds):
        base["refused"] = ("machine clock is not IST (UTC+05:30); 08:50 would be "
                           "the wrong time. Fix the timezone first.")
        return base

    base["scheduler"] = SCHEDULER_LABEL[target]
    if target == "ubuntu":
        if settings.enabled:
            base["actions"] = [
                {"do": "write", "path": str(CRON_FILE),
                 "text": cron_file_text(settings, root, user)},
                {"do": "disable", "unit": UBUNTU_TIMER},
            ]
        else:
            base["actions"] = [
                {"do": "remove", "path": str(CRON_FILE)},
                {"do": "disable", "unit": UBUNTU_TIMER},
            ]
    else:
        if settings.enabled:
            base["actions"] = [
                {"do": "run", "argv": schtasks_create_args(settings, root)},
                {"do": "run", "argv": schtasks_delete_args(OLD_WINDOWS_TASK),
                 "ignore_error": True},
            ]
        else:
            base["actions"] = [
                {"do": "run", "argv": schtasks_delete_args(WINDOWS_TASK),
                 "ignore_error": True},
                {"do": "run", "argv": schtasks_delete_args(OLD_WINDOWS_TASK),
                 "ignore_error": True},
            ]
    return base


# ------------------------------------------------------------- side effects
def _has_systemd() -> bool:
    return Path("/run/systemd/system").is_dir()


def _utc_offset_seconds() -> int:
    return int(_dt.datetime.now().astimezone().utcoffset().total_seconds())


def _apply(plan: Dict[str, object]) -> int:
    failures = 0
    for action in plan["actions"]:  # type: ignore[index]
        kind = action["do"]
        if kind == "write":
            path = Path(action["path"])
            path.write_text(action["text"], encoding="utf-8")
            os.chmod(path, 0o644)
            print(f"wrote {path}")
        elif kind == "remove":
            path = Path(action["path"])
            if path.exists():
                path.unlink()
                print(f"removed {path}")
        elif kind == "disable":
            subprocess.run(["systemctl", "disable", "--now", action["unit"]],
                           check=False)
            print(f"disabled {action['unit']}")
        elif kind == "run":
            result = subprocess.run(action["argv"], check=False,
                                    capture_output=True, text=True)
            if result.returncode and not action.get("ignore_error"):
                failures += 1
                print(f"FAILED: {' '.join(action['argv'][:3])} ({result.returncode})")
            elif result.returncode == 0:
                print(f"ok: {' '.join(action['argv'][:3])}")
    return failures


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--apply", action="store_true",
                        help="change this machine (default is a dry run)")
    parser.add_argument("--settings", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--user", default=os.environ.get("SERVICE_USER", "algo"))
    args = parser.parse_args(argv)

    settings, warning = load(args.settings)
    if warning:
        print(f"WARN: {warning}")
    detected = detect_os(platform.system(), _has_systemd())
    plan = build_plan(settings, detected=detected,
                      utc_offset_seconds=_utc_offset_seconds(), user=args.user)

    print(f"enabled={settings.enabled} broker_login={settings.broker_login} "
          f"engine_start={settings.engine_start} start_time={settings.start_time} "
          f"os_target={settings.os_target} detected={detected}")
    if args.status:
        return 0
    if plan["refused"]:
        print(f"REFUSED: {plan['refused']}")
        return 3
    for action in plan["actions"]:  # type: ignore[index]
        print(f"plan: {action['do']} {action.get('path') or action.get('unit') or ''}"
              f"{' ' + ' '.join(action['argv'][:3]) if 'argv' in action else ''}")
    if not args.apply:
        print("dry run - nothing changed. Add --apply to change this machine.")
        return 0
    return 1 if _apply(plan) else 0


if __name__ == "__main__":
    sys.exit(main())
