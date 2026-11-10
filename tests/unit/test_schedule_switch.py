"""Pre-open settings, installer plan and run-time engine toggle.

Pure logic only. No cron, Task Scheduler or systemd is touched (plan: docs/SCHEDULER_PLAN.md).
"""
import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from quant.core.schedule_settings import ScheduleSettings, load, save

ROOT = Path(__file__).resolve().parents[2]


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "ops" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inst = _load_script("install_schedule")
flag = _load_script("read_engine_flag")

IST = 19800


# ---------------------------------------------------------- settings store
def test_defaults_are_engine_manual_and_0850(tmp_path):
    settings, warning = load(tmp_path / "missing.json")
    assert settings.engine_start is False
    assert settings.start_time == "08:50"
    assert settings.broker == "mstock"
    assert warning is None


def test_old_switch_fields_are_gone_from_the_model():
    fields = set(ScheduleSettings.__dataclass_fields__)
    assert "enabled" not in fields
    assert "broker_login" not in fields


def test_old_file_with_retired_keys_still_loads(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"enabled": True, "broker_login": False, "engine_start": True}),
                    encoding="utf-8")
    settings, warning = load(path)
    assert warning is None
    assert settings.engine_start is True


def test_corrupt_file_gives_engine_manual(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    settings, warning = load(path)
    assert settings.engine_start is False
    assert "engine auto start OFF" in warning


def test_invalid_time_gives_engine_manual(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"engine_start": True, "start_time": "8:50pm"}), encoding="utf-8")
    settings, warning = load(path)
    assert settings.engine_start is False
    assert "HH:MM" in warning


def test_save_then_load_round_trip(tmp_path):
    path = tmp_path / "s.json"
    save(ScheduleSettings(engine_start=True, start_time="08:55"), path)
    settings, warning = load(path)
    assert warning is None
    assert settings.engine_start is True and settings.start_time == "08:55"


def test_save_refuses_invalid_settings(tmp_path):
    with pytest.raises(ValueError):
        save(ScheduleSettings(start_time="25:00"), tmp_path / "s.json")
    assert not (tmp_path / "s.json").exists()


# ----------------------------------------------------------- run-time flag
def test_engine_flag_is_zero_when_file_missing(tmp_path):
    assert flag.engine_flag(tmp_path / "none.json") == "0"


def test_engine_flag_follows_toggle(tmp_path):
    path = tmp_path / "s.json"
    save(ScheduleSettings(engine_start=True), path)
    assert flag.engine_flag(path) == "1"
    save(ScheduleSettings(engine_start=False), path)
    assert flag.engine_flag(path) == "0"


def test_engine_flag_fails_closed_on_corrupt_file(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("[]", encoding="utf-8")
    assert flag.engine_flag(path) == "0"


def test_engine_flag_main_prints_one_line():
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert flag.main([]) == 0
    assert buf.getvalue().strip() in ("0", "1")


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
def test_windows_install_creates_one_task_and_no_cron(tmp_path):
    plan = inst.build_plan(ScheduleSettings(), detected="windows", utc_offset_seconds=IST, root=tmp_path)
    assert plan["scheduler"] == "Task Scheduler" and plan["mode"] == "install"
    assert "write" not in [a["do"] for a in plan["actions"]]
    create = plan["actions"][0]["argv"]
    assert create[create.index("/ST") + 1] == "08:50"
    assert create[create.index("/D") + 1] == "MON,TUE,WED,THU,FRI"


def test_windows_install_removes_retired_login_task(tmp_path):
    plan = inst.build_plan(ScheduleSettings(), detected="windows", utc_offset_seconds=IST, root=tmp_path)
    names = [a["argv"][a["argv"].index("/TN") + 1] for a in plan["actions"] if "/TN" in a["argv"]]
    assert names == ["BlueEngine\\PreOpen", "BlueEngine\\BrokerLogin"]


def test_windows_remove_deletes_both_tasks(tmp_path):
    plan = inst.build_plan(ScheduleSettings(), detected="windows", utc_offset_seconds=IST,
                           root=tmp_path, remove=True)
    assert plan["mode"] == "remove"
    names = [a["argv"][a["argv"].index("/TN") + 1] for a in plan["actions"]]
    assert names == ["BlueEngine\\PreOpen", "BlueEngine\\BrokerLogin"]
    assert all(a["argv"][0] == "schtasks" and a["argv"][1] == "/Delete" for a in plan["actions"])


def test_ubuntu_install_writes_cron_and_disables_timer(tmp_path):
    plan = inst.build_plan(ScheduleSettings(), detected="ubuntu", utc_offset_seconds=IST, root=tmp_path)
    write = next(a for a in plan["actions"] if a["do"] == "write")
    assert write["path"] == str(inst.CRON_FILE)
    assert "CRON_TZ=Asia/Kolkata" in write["text"]
    assert "50 8 * * 1-5" in write["text"]
    assert any(a["do"] == "disable" and a["unit"] == "pov-preopen.timer" for a in plan["actions"])


def test_ubuntu_remove_removes_cron(tmp_path):
    plan = inst.build_plan(ScheduleSettings(), detected="ubuntu", utc_offset_seconds=IST,
                           root=tmp_path, remove=True)
    assert {"do": "remove", "path": str(inst.CRON_FILE)} in plan["actions"]


def test_cron_text_does_not_change_when_engine_toggle_flips(tmp_path):
    on = inst.cron_file_text(ScheduleSettings(engine_start=True), tmp_path, "algo")
    off = inst.cron_file_text(ScheduleSettings(engine_start=False), tmp_path, "algo")
    assert on == off  # the toggle is read at run time, not baked into cron
    assert "BROKER_AUTOLOGIN" not in on and "--no-engine" not in on


def test_refuses_when_clock_not_ist(tmp_path):
    plan = inst.build_plan(ScheduleSettings(), detected="ubuntu", utc_offset_seconds=0, root=tmp_path)
    assert plan["refused"] and "not IST" in plan["refused"]
    assert plan["actions"] == []


def test_refuses_os_mismatch_and_touches_nothing(tmp_path):
    plan = inst.build_plan(ScheduleSettings(os_target="ubuntu"), detected="windows",
                           utc_offset_seconds=IST, root=tmp_path)
    assert plan["refused"] and plan["actions"] == []


def test_refuses_invalid_settings(tmp_path):
    plan = inst.build_plan(ScheduleSettings(start_time="99:00"), detected="ubuntu",
                           utc_offset_seconds=IST, root=tmp_path)
    assert plan["refused"] and plan["actions"] == []


# ---------------------------------------------- shell + wrapper wiring (regression)
def test_preopen_defines_record_only_before_first_use():
    lines = (ROOT / "scripts" / "ops" / "preopen_launch.sh").read_text().splitlines()
    define = next(i for i, l in enumerate(lines) if l.startswith("record_only()"))
    first_use = next(i for i, l in enumerate(lines) if l.strip().startswith("record_only 1"))
    assert define < first_use


def test_preopen_login_is_always_on_and_engine_reads_toggle():
    text = (ROOT / "scripts" / "ops" / "preopen_launch.sh").read_text()
    assert 'BROKER_AUTOLOGIN=1 "$VENV_PY"' in text
    assert "read_engine_flag.py" in text
    assert text.index("read_engine_flag.py") < text.index('info "starting the engine')


def test_windows_wrapper_logs_in_always_and_reads_toggle():
    text = (ROOT / "scripts" / "ops" / "SCHEDULED_PREOPEN.cmd").read_text()
    assert 'set "BROKER_AUTOLOGIN=1"' in text
    assert "read_engine_flag.py" in text


def test_pre_open_time_is_0850_in_launcher():
    launch = (ROOT / "Servers" / "launch_ubuntu_server.sh").read_text()
    assert 'PREOPEN_TIME="08:50"' in launch
    assert "OnCalendar=Mon..Fri *-*-* 08:50:00" in launch
    assert "08:45" not in launch
