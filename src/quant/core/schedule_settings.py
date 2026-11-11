"""Pre-open settings: scheduler details and the auto engine start toggle.

Status: PHASE 3 IMPLEMENTED (settings file with fail-closed defaults; tests in tests/unit/test_schedule_switch.py)
Plan: docs/SCHEDULER_PLAN.md (updated 2026-11-10).

There is no on/off switch. Once the scheduler is installed on a machine, the
broker login runs at the start time every weekday. The only runtime toggle is
engine_start: ON starts the engine after a good login, OFF leaves it for the
START ENGINE button.

The settings file is runtime state, so it is git-ignored. A missing or corrupt
file means engine_start is OFF (fail closed).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = ROOT / "data" / "schedule_settings.json"

OS_CHOICES = ("auto", "windows", "ubuntu")
BROKER_CHOICES = ("mstock",)
SCHEDULER_LABEL = {"windows": "Task Scheduler", "ubuntu": "cron"}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class ScheduleSettings:
    """Scheduler details plus the engine toggle. Every field has a safe default."""

    engine_start: bool = False       # ON: engine starts after login. OFF: button only.
    broker: str = "mstock"
    os_target: str = "auto"          # auto | windows | ubuntu
    start_time: str = "08:50"        # IST; 25 minutes before the 09:15 open

    def errors(self) -> List[str]:
        problems: List[str] = []
        if self.broker not in BROKER_CHOICES:
            problems.append(f"broker must be one of {BROKER_CHOICES}")
        if self.os_target not in OS_CHOICES:
            problems.append(f"os_target must be one of {OS_CHOICES}")
        if not _TIME_RE.match(str(self.start_time)):
            problems.append("start_time must be HH:MM (24-hour, IST)")
        if not isinstance(self.engine_start, bool):
            problems.append("engine_start must be true or false")
        return problems

    @property
    def valid(self) -> bool:
        return not self.errors()

    def to_dict(self) -> dict:
        return asdict(self)


def _from_mapping(raw: dict) -> ScheduleSettings:
    # Unknown keys (for example the retired "enabled" and "broker_login") are ignored.
    known = {f: raw[f] for f in ScheduleSettings.__dataclass_fields__ if f in raw}
    return ScheduleSettings(**{**asdict(ScheduleSettings()), **known})


def load(path: Path = DEFAULT_PATH) -> Tuple[ScheduleSettings, Optional[str]]:
    """Return (settings, warning). A missing or bad file gives the safe defaults."""
    if not path.is_file():
        return ScheduleSettings(), None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("top level is not an object")
        settings = _from_mapping(raw)
    except (OSError, ValueError) as exc:
        return ScheduleSettings(), f"settings file unreadable, engine auto start OFF: {exc}"
    problems = settings.errors()
    if problems:
        return ScheduleSettings(), "settings invalid, engine auto start OFF: " + "; ".join(problems)
    return settings, None


def save(settings: ScheduleSettings, path: Path = DEFAULT_PATH) -> None:
    """Validate, then write atomically. Refuses to save an invalid set."""
    problems = settings.errors()
    if problems:
        raise ValueError("refusing to save invalid settings: " + "; ".join(problems))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".sched-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(settings.to_dict(), stream, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
