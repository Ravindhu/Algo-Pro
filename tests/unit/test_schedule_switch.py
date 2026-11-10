"""Pre-open settings, installer plan, missed-run runner and run-time engine toggle.

Pure logic only. No cron, Task Scheduler or systemd is touched (plan: docs/SCHEDULER_PLAN.md).
"""
import datetime as dt
import importlib.util
import io
import json
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from quant.core.schedule_settings import ScheduleSettings, load, save

ROOT = Path(__file__).resolve().parents[2]
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "ops" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inst = _load_script("install_schedule")
flag = _load_script("read_engine_flag")
run = _load_script("run_preopen_once")
OFFSET = 19800


def at(hour, minute, day=10):
    """An IST datetime on 2026-11-<day>. 10 Nov 2026 is a Tuesday."""
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=IST)


START = dt.time(8, 50)


# ---------------------------------------------------------- settings store
def test_defaults_are_engine_manual_and_0850(tmp_path):
    settings, warning = load(tmp_path / "missing.json")
    assert settings.engine_start is False and settings.start_time == "08:50"
    assert settings.broker == "mstock" and warning is None


def test_retired_switch_fields_are_gone():
    fields = set(ScheduleSettings.__dataclass_fields__)
    assert "enabled" not in fields and "broker_login" not in fields


def test_old_file_with_retired_keys_still_loads(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"enabled": True, "broker_login": False, "engine_start": True}), encoding="utf-8")
    settings, warning = load(path)
    assert warning is None and settings.engine_start is True


def test_corrupt_file_gives_engine_manual(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    settings, warning = load(path)
    assert settings.engine_start is False and "engine auto start OFF" in warning


def test_save_then_load_round_trip_and_refuses_invalid(tmp_path):
    path = tmp_path / "s.json"
    save(ScheduleSettings(engine_start=True, start_time="08:55"), path)
    settings, warning = load(path)
    assert warning is None and settings.engine_start is True and settings.start_time == "08:55"
    with pytest.raises(ValueError):
        save(ScheduleSettings(start_time="25:00"), tmp_path / "bad.json")


# ----------------------------------------------------------- run-time flag
def test_engine_flag_follows_toggle_and_fails_closed(tmp_path):
    path = tmp_path / "s.json"
    assert flag.engine_flag(tmp_path / "none.json") == "0"
    save(ScheduleSettings(engine_start=True), path)
    assert flag.engine_flag(path) == "1"
    path.write_text("[]", encoding="utf-8")
    assert flag.engine_flag(path) == "0"


def test_engine_flag_main_prints_one_line():
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert flag.main([]) == 0
    assert buf.getvalue().strip() in ("0", "1")


# ------------------------------------------------------- runner: missed runs
def test_before_start_time_waits_then_runs():
    d = run.plan_run(at(7, 40), START, True, False, True)
    assert d["action"] == "wait" and d["wait_seconds"] == pytest.approx(70 * 60)
    assert d["engine_allowed"] is False


def test_powered_on_late_runs_now_not_skipped():
    d = run.plan_run(at(10, 5), START, True, False, False)
    assert d["action"] == "run" and "catch-up" in d["reason"]


def test_after_market_close_is_skipped_and_said_so():
    d = run.plan_run(at(16, 0), START, True, False, True)
    assert d["action"] == "skip" and "15:30" in d["reason"]


def test_engine_starts_only_before_0915_when_toggle_on():
    early = run.plan_run(at(8, 55), START, True, False, True)
    assert early["action"] == "run" and early["engine_allowed"] is True
    late = run.plan_run(at(9, 20), START, True, False, True)
    assert late["action"] == "run" and late["engine_allowed"] is False
    assert "dashboard button" in late["reason"]


def test_login_runs_but_engine_waits_when_toggle_off():
    d = run.plan_run(at(8, 55), START, True, False, False)
    assert d["action"] == "run" and d["engine_allowed"] is False


def test_holiday_or_weekend_is_skipped():
    d = run.plan_run(at(9, 0, day=14), START, False, False, True)  # a Saturday
    assert d["action"] == "skip" and "not a trading day" in d["reason"]


def test_second_trigger_on_same_day_is_skipped():
    d = run.plan_run(at(10, 0), START, True, True, True)
    assert d["action"] == "skip" and "already ran" in d["reason"]


def test_marker_claim_is_exclusive(tmp_path):
    path = run.marker_path(dt.date(2026, 11, 10), tmp_path)
    assert run.claim_marker(path, {"state": "in_progress"}) is True
    assert run.claim_marker(path, {"state": "in_progress"}) is False  # second trigger refused


def test_holidays_read_from_csv(tmp_path):
    csv_path = tmp_path / "h.csv"
    csv_path.write_text("date,exchange,name\n2026-11-10,NSE,Test\n", encoding="utf-8")
    assert run.load_holidays(csv_path) == {dt.date(2026, 11, 10)}
    assert run.load_holidays(tmp_path / "none.csv") == set()


def test_runner_command_per_platform():
    win = run.script_command("Windows", ROOT, "py")
    lin = run.script_command("Linux", ROOT, "py")
    assert win[0] == "cmd" and win[-1].endswith("SCHEDULED_PREOPEN.cmd")
    assert lin[0] == "/bin/bash" and lin[-1] == "--runner-gated"


# ------------------------------------------------------------ OS detection
@pytest.mark.parametrize("system,systemd,expected", [
    ("Windows", False, "windows"), ("Linux", True, "ubuntu"),
    ("Linux", False, "unsupported"), ("Darwin", True, "unsupported"),
])
def test_detect_os(system, systemd, expected):
    assert inst.detect_os(system, systemd) == expected


def test_explicit_target_must_match_machine():
    assert inst.resolve_target(ScheduleSettings(os_target="ubuntu"), "windows") is None
    assert inst.resolve_target(ScheduleSettings(os_target="auto"), "ubuntu") == "ubuntu"


# ------------------------------------------------------------- the plans
def test_windows_xml_has_both_triggers_and_missed_run_rule(tmp_path):
    xml = inst.windows_task_xml(ScheduleSettings(), tmp_path, "py.exe")
    root = ET.fromstring(xml)
    ns = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
    assert root.find(f"{ns}Triggers/{ns}CalendarTrigger/{ns}StartBoundary").text.endswith("T08:50:00")
    assert root.find(f"{ns}Triggers/{ns}LogonTrigger") is not None
    assert root.find(f"{ns}Settings/{ns}StartWhenAvailable").text == "true"
    assert root.find(f"{ns}Settings/{ns}WakeToRun").text == "true"


def test_windows_install_imports_xml_and_no_cron(tmp_path):
    plan = inst.build_plan(ScheduleSettings(), detected="windows", utc_offset_seconds=OFFSET,
                           root=tmp_path, python="py.exe")
    kinds = [a["do"] for a in plan["actions"]]
    assert "write" not in kinds and "write_bytes" in kinds
    imp = next(a for a in plan["actions"] if a.get("argv", [""])[0] == "schtasks" and "/XML" in a["argv"])
    assert imp["argv"][imp["argv"].index("/TN") + 1] == "BlueEngine\\PreOpen"


def test_windows_remove_deletes_both_tasks(tmp_path):
    plan = inst.build_plan(ScheduleSettings(), detected="windows", utc_offset_seconds=OFFSET,
                           root=tmp_path, python="py.exe", remove=True)
    names = [a["argv"][a["argv"].index("/TN") + 1] for a in plan["actions"]]
    assert names == ["BlueEngine\\PreOpen", "BlueEngine\\BrokerLogin"]


def test_ubuntu_cron_has_daily_and_reboot_lines_pointing_at_runner(tmp_path):
    text = inst.cron_file_text(ScheduleSettings(), tmp_path, "algo", "/usr/bin/python3")
    assert "CRON_TZ=Asia/Kolkata" in text
    assert "50 8 * * 1-5 algo" in text
    assert "@reboot algo" in text
    assert "run_preopen_once.py" in text


def test_ubuntu_install_disables_timer_and_remove_deletes_cron(tmp_path):
    plan = inst.build_plan(ScheduleSettings(), detected="ubuntu", utc_offset_seconds=OFFSET,
                           root=tmp_path, python="py")
    assert any(a["do"] == "disable" and a["unit"] == "pov-preopen.timer" for a in plan["actions"])
    gone = inst.build_plan(ScheduleSettings(), detected="ubuntu", utc_offset_seconds=OFFSET,
                           root=tmp_path, python="py", remove=True)
    assert {"do": "remove", "path": str(inst.CRON_FILE)} in gone["actions"]


def test_refusals_touch_nothing(tmp_path):
    not_ist = inst.build_plan(ScheduleSettings(), detected="ubuntu", utc_offset_seconds=0, root=tmp_path, python="py")
    mismatch = inst.build_plan(ScheduleSettings(os_target="ubuntu"), detected="windows",
                               utc_offset_seconds=OFFSET, root=tmp_path, python="py")
    invalid = inst.build_plan(ScheduleSettings(start_time="99:00"), detected="ubuntu",
                              utc_offset_seconds=OFFSET, root=tmp_path, python="py")
    for plan in (not_ist, mismatch, invalid):
        assert plan["refused"] and plan["actions"] == []


# ---------------------------------------------- shell + wrapper wiring
def test_preopen_defines_record_only_before_first_use():
    lines = (ROOT / "scripts" / "ops" / "preopen_launch.sh").read_text().splitlines()
    define = next(i for i, l in enumerate(lines) if l.startswith("record_only()"))
    first_use = next(i for i, l in enumerate(lines) if l.strip().startswith("record_only 1"))
    assert define < first_use


def test_preopen_accepts_runner_gate_and_honours_engine_permission():
    text = (ROOT / "scripts" / "ops" / "preopen_launch.sh").read_text()
    assert "--runner-gated" in text
    assert '"$RUNNER_GATED" -eq 0' in text
    assert 'PREOPEN_ENGINE_ALLOWED:-1}" != "0"' in text
    assert 'BROKER_AUTOLOGIN=1 "$VENV_PY"' in text


def test_windows_wrapper_logs_in_and_honours_engine_permission():
    text = (ROOT / "scripts" / "ops" / "SCHEDULED_PREOPEN.cmd").read_text()
    assert 'set "BROKER_AUTOLOGIN=1"' in text
    assert "PREOPEN_ENGINE_ALLOWED" in text and "read_engine_flag.py" in text


def test_pre_open_time_is_0850_in_launcher():
    launch = (ROOT / "Servers" / "launch_ubuntu_server.sh").read_text()
    assert 'PREOPEN_TIME="08:50"' in launch and "08:45" not in launch
