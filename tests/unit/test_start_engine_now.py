"""START ENGINE button path: login first, then engine. No timed job is left in the code."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

from quant.core import engine_control as eng

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("start_engine_now", ROOT / "scripts" / "ops" / "start_engine_now.py")
sen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sen)


def test_login_runs_first_then_engine_starts(tmp_path):
    order = []

    def fake_runner(cmd, **kw):
        order.append("login")
        return SimpleNamespace(returncode=0)

    def fake_start(*, kill_active):
        order.append("engine")
        return True, "started", 123

    rc = sen.run(root=tmp_path, system_name="Windows", python="py", runner=fake_runner,
                 start=fake_start, kill_active=False)
    assert rc == 0 and order == ["login", "engine"]


def test_failed_login_means_engine_not_started(tmp_path):
    started = []
    rc = sen.run(root=tmp_path, system_name="Windows", python="py",
                 runner=lambda cmd, **kw: SimpleNamespace(returncode=2),
                 start=lambda **kw: started.append(1) or (True, "x", 1), kill_active=False)
    assert rc == 2 and started == []


def test_login_env_allows_login_and_keeps_engine_off():
    env = sen.login_env({"PATH": "x"})
    assert env["BROKER_AUTOLOGIN"] == "1" and env["PREOPEN_ENGINE_ALLOWED"] == "0"


def test_windows_login_uses_the_wrapper_and_ubuntu_uses_the_shell_launcher(tmp_path):
    assert sen.script_command("Windows", tmp_path, "py")[1] == "/c"
    assert sen.script_command("Linux", tmp_path, "python")[0] == "/bin/bash"


def test_button_still_refused_when_kill_switch_on(tmp_path):
    ok, msg, pid = eng.request_manual_start(kill_active=True, pid_path=tmp_path / "e.pid",
                                            log_path=tmp_path / "m.log")
    assert ok is False and pid is None and "kill switch" in msg
    assert not (tmp_path / "m.log").exists()


def test_button_still_refused_when_engine_already_running(tmp_path):
    pid_file = tmp_path / "e.pid"
    pid_file.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    ok, msg, _ = eng.request_manual_start(kill_active=False, pid_path=pid_file, log_path=tmp_path / "m.log")
    assert ok is False and "already running" in msg


def test_button_points_at_the_new_script_and_no_manual_flag():
    assert eng.RUNNER == ROOT / "scripts" / "ops" / "start_engine_now.py"
    text = (ROOT / "src" / "quant" / "core" / "engine_control.py").read_text(encoding="utf-8")
    assert '"--manual"' not in text


def test_timed_pre_open_job_is_gone_from_code():
    assert not (ROOT / "scripts" / "ops" / "run_preopen_once.py").exists()
    assert not (ROOT / "scripts" / "ops" / "install_schedule.py").exists()
    html = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    assert "install_schedule" not in html and "run_preopen_once" not in html
