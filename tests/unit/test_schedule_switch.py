"""Pre-open schedule switch: settings store and OS-aware installer plan.

Pure logic only. No cron, Task Scheduler or systemd is touched (plan: docs/SCHEDULER_PLAN.md).
"""
import importlib.util
import json
from pathlib import Path

import pytest

from quant.core.schedule_settings import ScheduleSettings, load, save

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "install_schedule", ROOT / "scripts" / "ops" / "install_schedule.py")
inst = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inst)

IST = 19800
ON = ScheduleSettings(enabled=True)
OFF = ScheduleSettings(enabled=False)


# ---------------------------------------------------------- settings store
def test_defaults_are_off_and_0850(tmp_path):
    settings, warning = load(tmp_path / "missing.json")
    assert settings.enabled is False
    assert settings.start_time == "08:50"
    assert settings.broker == "mstock"
    assert warning is None


def test_corrupt_file_fails_closed(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    settings, warning = load(path)
    assert settings.enabled is False
    assert "schedule OFF" in warning


def test_invalid_time_fails_closed(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"enabled": True, "start_time": "8:50pm"}), encoding="utf-8")
    settings, warning = load(path)
    assert settings.enabled is False
    assert "HH:MM" in warning


def test_save_then_load_round_trip(tmp_path):
    path = tmp_path / "s.json"
    save(ScheduleSettings(enabled=True, engine_start=False), path)
    settings, warning = load(path)
    assert warning is None
    assert settings.enabled is True and settings.engine_start is False


def test_save_refuses_invalid_settings(tmp_path):
    with pytest.raises(ValueError):
        save(ScheduleSettings(start_time="25:00"), tmp_path / "s.json")
    assert not (tmp_path / "s.json").exists()


# ------------------------------------------------------------ OS detection
@pytest.mark.parametrize("system,systemd,expected", [
    ("Windows", False, "windows"),
    ("Linux", True, "ubuntu"),
    ("Linux", False, "unsupported"),
    ("Darwin", True, "unsupported"),
])
def test_detect_os(system, systemd, expected):
    assert inst.detect_os(system, systemd) == expected


def test_explicit_target_must_match_machine():
    assert inst.resolve_target(ScheduleSettings(os_target="ubuntu"), "windows") is None
    assert inst.resolve_target(ScheduleSettings(os_target="windows"), "windows") == "windows"
    assert inst.resolve_target(ScheduleSettings(os_target="auto"), "ubuntu") == "ubuntu"


# ------------------------------------------------------------- the plans
def test_windows_enable_writes_only_task_scheduler(tmp_path):
    plan = inst.build_plan(ON, detected="windows", utc_offset_seconds=IST, root=tmp_path)
    assert plan["scheduler"] == "Task Scheduler"
    kinds = [a["do"] for a in plan["actions"]]
    assert "write" not in kinds                      # never a cron file on Windows
    create = plan["actions"][0]["argv"]
    assert create[:2] == ["schtasks", "/Create"]
    assert create[create.index("/ST") + 1] == "08:50"
    assert create[create.index("/D") + 1] == "MON,TUE,WED,THU,FRI"
    assert any("BlueEngine\\BrokerLogin" in a["argv"] for a in plan["actions"] if "argv" in a)


def test_windows_disable_deletes_both_tasks(tmp_path):
    plan = inst.build_plan(OFF, detected="windows", utc_offset_seconds=IST, root=tmp_path)
    names = [a["argv"][a["argv"].index("/TN") + 1] for a in plan["actions"]]
    assert names == ["BlueEngine\\PreOpen", "BlueEngine\\BrokerLogin"]


def test_ubuntu_enable_writes_cron_and_disables_timer(tmp_path):
    plan = inst.build_plan(ON, detected="ubuntu", utc_offset_seconds=IST, root=tmp_path)
    assert plan["scheduler"] == "cron"
    write = next(a for a in plan["actions"] if a["do"] == "write")
    assert write["path"] == str(inst.CRON_FILE)
    text = write["text"]
    assert "CRON_TZ=Asia/Kolkata" in text
    assert "50 8 * * 1-5" in text
    assert "BROKER_AUTOLOGIN=1" in text
    assert "--no-engine" not in text
    assert any(a["do"] == "disable" and a["unit"] == "pov-preopen.timer" for a in plan["actions"])
    assert not any(a.get("argv", [""])[0] == "schtasks" for a in plan["actions"])


def test_ubuntu_disable_removes_cron(tmp_path):
    plan = inst.build_plan(OFF, detected="ubuntu", utc_offset_seconds=IST, root=tmp_path)
    assert {"do": "remove", "path": str(inst.CRON_FILE)} in plan["actions"]


def test_cron_flags_follow_settings(tmp_path):
    text = inst.cron_file_text(ScheduleSettings(enabled=True, broker_login=False,
                                                engine_start=False), tmp_path, "algo")
    assert "BROKER_AUTOLOGIN=0" in text
    assert "--no-engine" in text


def test_refuses_when_clock_not_ist(tmp_path):
    plan = inst.build_plan(ON, detected="ubuntu", utc_offset_seconds=0, root=tmp_path)
    assert plan["refused"] and "not IST" in plan["refused"]
    assert plan["actions"] == []


def test_refuses_os_mismatch_and_touches_nothing(tmp_path):
    settings = ScheduleSettings(enabled=True, os_target="ubuntu")
    plan = inst.build_plan(settings, detected="windows", utc_offset_seconds=IST, root=tmp_path)
    assert plan["refused"] and plan["actions"] == []


def test_refuses_invalid_settings(tmp_path):
    plan = inst.build_plan(ScheduleSettings(enabled=True, start_time="99:00"),
                           detected="ubuntu", utc_offset_seconds=IST, root=tmp_path)
    assert plan["refused"] and plan["actions"] == []


# -------------------------------------------------- shell fix (regression)
def test_preopen_defines_record_only_before_first_use():
    lines = (ROOT / "scripts" / "ops" / "preopen_launch.sh").read_text().splitlines()
    define = next(i for i, l in enumerate(lines) if l.startswith("record_only()"))
    first_use = next(i for i, l in enumerate(lines) if l.strip().startswith("record_only 1"))
    assert define < first_use


def test_pre_open_time_is_0850_everywhere_it_matters():
    launch = (ROOT / "Servers" / "launch_ubuntu_server.sh").read_text()
    assert 'PREOPEN_TIME="08:50"' in launch
    assert "OnCalendar=Mon..Fri *-*-* 08:50:00" in launch
    assert "08:45" not in launch
