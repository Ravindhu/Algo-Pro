"""Install or remove the pre-open schedule on THIS machine only.

Plan: docs/SCHEDULER_PLAN.md (updated 2026-11-10, missed runs).

Both OSes run scripts/ops/run_preopen_once.py, which decides day and time.
* Windows -> one Task Scheduler job (BlueEngine\\PreOpen): weekday trigger at the
  start time, a logon trigger, and "start as soon as possible" for missed runs.
  WakeToRun wakes the machine from sleep. A machine fully powered off cannot run it.
* Ubuntu  -> one cron file (/etc/cron.d/mts-preopen): a daily line at the start time,
  plus @reboot so a run missed while the server was off happens after boot.
* Refused when the clock is not IST or the machine does not match os_target.

Default is a dry run. --apply changes the machine. --remove removes the schedule.

    python scripts/ops/install_schedule.py --status
    python scripts/ops/install_schedule.py --apply
    python scripts/ops/install_schedule.py --remove --apply
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional
from xml.sax.saxutils import escape

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
OLD_WINDOWS_TASK = "BlueEngine\\BrokerLogin"       # retired earlier, always removed
UBUNTU_TIMER = "pov-preopen.timer"
RUNNER = Path("scripts") / "ops" / "run_preopen_once.py"
TASK_XML = Path("Temp") / "run" / "preopen_task.xml"


# --------------------------------------------------------------- pure logic
def detect_os(system_name: str, has_systemd: bool) -> str:
    """'windows', 'ubuntu', or 'unsupported'. Pure: the caller passes facts in."""
    if system_name == "Windows":
        return "windows"
    if system_name == "Linux" and has_systemd:
        return "ubuntu"
    return "unsupported"


def resolve_target(settings: ScheduleSettings, detected: str) -> Optional[str]:
    if detected not in ("windows", "ubuntu"):
        return None
    if settings.os_target == "auto":
        return detected
    return settings.os_target if settings.os_target == detected else None


def clock_is_ist(utc_offset_seconds: int) -> bool:
    return int(utc_offset_seconds) == IST_OFFSET_SECONDS


def cron_file_text(settings: ScheduleSettings, root: Path, user: str, python: str) -> str:
    """The /etc/cron.d file. CRON_TZ pins the daily line to IST; @reboot covers a missed run."""
    hour, minute = settings.start_time.split(":")
    job = f"cd {root} && {python} {root / RUNNER} >> {root}/log/preopen/cron.log 2>&1"
    return (
        "# managed by scripts/ops/install_schedule.py - do not edit by hand\n"
        "CRON_TZ=Asia/Kolkata\n"
        f"{int(minute)} {int(hour)} * * 1-5 {user} {job}\n"
        f"@reboot {user} {job}\n"
    )


def windows_task_xml(settings: ScheduleSettings, root: Path, python: str) -> str:
    """Task Scheduler XML: weekday trigger at the start time, logon trigger, run missed tasks."""
    hour, minute = settings.start_time.split(":")
    days = "".join(f"<{d} />" for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"))
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>Blue Engine pre-open: broker login, then engine if auto start is on. "
        "Runs at the start time, and on logon. A missed run is not skipped.</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <CalendarTrigger>\n"
        "      <StartBoundary>2026-01-05T{h}:{m}:00</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "      <ScheduleByWeek><DaysOfWeek>{days}</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>\n"
        "    </CalendarTrigger>\n"
        "    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author"><LogonType>InteractiveToken</LogonType>'
        "<RunLevel>LeastPrivilege</RunLevel></Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <WakeToRun>true</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT6H</ExecutionTimeLimit>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "  </Settings>\n"
        '  <Actions Context="Author"><Exec>'
        "<Command>{cmd}</Command><Arguments>{args}</Arguments>"
        "<WorkingDirectory>{cwd}</WorkingDirectory></Exec></Actions>\n"
        "</Task>\n"
    ).format(h=hour, m=minute, days=days, cmd=escape(python),
             args=escape(f'"{root / RUNNER}"'), cwd=escape(str(root)))


def schtasks_import_args(xml_path: Path) -> List[str]:
    return ["schtasks", "/Create", "/TN", WINDOWS_TASK, "/XML", str(xml_path), "/F"]


def schtasks_delete_args(name: str) -> List[str]:
    return ["schtasks", "/Delete", "/TN", name, "/F"]


def build_plan(settings: ScheduleSettings, *, detected: str, utc_offset_seconds: int,
               root: Path = ROOT, user: str = "algo", python: str = "python3",
               remove: bool = False) -> Dict[str, object]:
    """Decide what would change. Pure: no file or command is touched here."""
    base: Dict[str, object] = {"detected_os": detected, "scheduler": None, "actions": [],
                               "refused": None, "mode": "remove" if remove else "install"}
    if not settings.valid:
        base["refused"] = "settings invalid: " + "; ".join(settings.errors())
        return base
    target = resolve_target(settings, detected)
    if target is None:
        base["refused"] = (f"this machine is {detected!r} but settings target "
                           f"{settings.os_target!r}; nothing changed here")
        return base
    if not clock_is_ist(utc_offset_seconds):
        base["refused"] = ("machine clock is not IST (UTC+05:30); the start time would be "
                           "wrong. Fix the timezone first.")
        return base

    base["scheduler"] = SCHEDULER_LABEL[target]
    if target == "ubuntu":
        if remove:
            base["actions"] = [{"do": "remove", "path": str(CRON_FILE)},
                               {"do": "disable", "unit": UBUNTU_TIMER}]
        else:
            base["actions"] = [{"do": "write", "path": str(CRON_FILE),
                                "text": cron_file_text(settings, root, user, python)},
                               {"do": "disable", "unit": UBUNTU_TIMER}]
    else:
        if remove:
            base["actions"] = [
                {"do": "run", "argv": schtasks_delete_args(WINDOWS_TASK), "ignore_error": True},
                {"do": "run", "argv": schtasks_delete_args(OLD_WINDOWS_TASK), "ignore_error": True},
            ]
        else:
            xml_path = root / TASK_XML
            base["actions"] = [
                {"do": "write_bytes", "path": str(xml_path),
                 "data": windows_task_xml(settings, root, python).encode("utf-16")},
                {"do": "run", "argv": schtasks_import_args(xml_path)},
                {"do": "run", "argv": schtasks_delete_args(OLD_WINDOWS_TASK), "ignore_error": True},
            ]
    return base


# ------------------------------------------------------------- side effects
def _has_systemd() -> bool:
    return Path("/run/systemd/system").is_dir()


def _utc_offset_seconds() -> int:
    return int(_dt.datetime.now().astimezone().utcoffset().total_seconds())


def _python_for(root: Path, system_name: str) -> str:
    """The venv interpreter if it exists, else this one. The job runs the runner with it."""
    venv = root / ".venv" / ("Scripts/python.exe" if system_name == "Windows" else "bin/python")
    return str(venv) if venv.exists() else sys.executable


def _apply(plan: Dict[str, object]) -> int:
    failures = 0
    for action in plan["actions"]:  # type: ignore[index]
        kind = action["do"]
        if kind == "write":
            path = Path(action["path"])
            path.write_text(action["text"], encoding="utf-8")
            os.chmod(path, 0o644)
            print(f"wrote {path}")
        elif kind == "write_bytes":
            path = Path(action["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(action["data"])
            print(f"wrote {path}")
        elif kind == "remove":
            path = Path(action["path"])
            if path.exists():
                path.unlink()
                print(f"removed {path}")
        elif kind == "disable":
            subprocess.run(["systemctl", "disable", "--now", action["unit"]], check=False)
            print(f"disabled {action['unit']}")
        elif kind == "run":
            result = subprocess.run(action["argv"], check=False, capture_output=True, text=True)
            if result.returncode and not action.get("ignore_error"):
                failures += 1
                print(f"FAILED: {' '.join(action['argv'][:3])} ({result.returncode})")
            elif result.returncode == 0:
                print(f"ok: {' '.join(action['argv'][:3])}")
    return failures


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Install or remove the pre-open schedule on this machine.")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--apply", action="store_true", help="change this machine (default is a dry run)")
    parser.add_argument("--remove", action="store_true", help="remove the schedule instead of installing it")
    parser.add_argument("--settings", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--user", default=os.environ.get("SERVICE_USER", "algo"))
    args = parser.parse_args(argv)

    settings, warning = load(args.settings)
    if warning:
        print(f"WARN: {warning}")
    system_name = platform.system()
    detected = detect_os(system_name, _has_systemd())
    plan = build_plan(settings, detected=detected, utc_offset_seconds=_utc_offset_seconds(),
                      root=ROOT, user=args.user, python=_python_for(ROOT, system_name),
                      remove=args.remove)

    print(f"start_time={settings.start_time} broker={settings.broker} os_target={settings.os_target} "
          f"engine_start={settings.engine_start} detected={detected}")
    if args.status:
        return 0
    if plan["refused"]:
        print(f"REFUSED: {plan['refused']}")
        return 3
    for action in plan["actions"]:  # type: ignore[index]
        label = action.get("path") or action.get("unit") or " ".join(action["argv"][:3])
        print(f"plan: {plan['mode']} {action['do']} {label}")
    if not args.apply:
        print("dry run - nothing changed. Add --apply to change this machine.")
        return 0
    return 1 if _apply(plan) else 0


if __name__ == "__main__":
    sys.exit(main())
