"""START ENGINE wiring: the button starts the BOT, supervised. It does not log anyone in.

Owner decision 2026-11-27:
  1. OpenAlgo + broker login happens at DASHBOARD SIGN-IN (quant.core.broker_signin).
  2. START ENGINE starts the bot. Paper trading accounting loop stays as it is
     (scripts/run/paper_loop.py); the OpenAlgo sandbox analyzer is the fake-money path
     for the strategy bot.

These tests exist because the button was wired to scripts/run/run_supervised.py, a file that
does not exist in a checkout, and every test injected a fake `start`, so nothing ever noticed.
Every path the button can reach is now asserted to EXIST here.
"""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

from quant.core import engine_control as eng
from quant.data import screener_session as ss

ROOT = Path(__file__).resolve().parents[2]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sen = _load("start_engine_now", "scripts/ops/start_engine_now.py")
svb = _load("supervise_bot", "scripts/ops/supervise_bot.py")


# ---------------------------------------------------------------- paths must exist

def test_every_path_the_button_reaches_exists():
    """The defect this file guards against: a dead end that no test walked into."""
    assert eng.RUNNER.is_file(), f"button runner missing: {eng.RUNNER}"
    assert eng.SUPERVISOR.is_file(), f"supervisor missing: {eng.SUPERVISOR}"
    assert eng.SUPERVISOR == ROOT / "scripts" / "ops" / "supervise_bot.py"
    for mode, script in svb.BOT_SCRIPTS.items():
        assert script.is_file(), f"bot runner for {mode} missing: {script}"


def test_supervisor_runs_the_strategy_bot_not_the_parked_pov_engine():
    """The parked run_supervised.py drove engines/input/master.json, a different system."""
    for mode in ("paper", "live"):
        cmd = " ".join(svb.bot_command(mode))
        assert "run_strategy_bot" in cmd
        assert "engine_entry.py" not in cmd and "run_supervised.py" not in cmd


def test_paper_and_live_pick_different_runners():
    paper = svb.bot_command("paper")
    live = svb.bot_command("live")
    assert paper[1].endswith("run_strategy_bot.py")
    assert live[1].endswith("run_strategy_bot_live.py")
    assert "--base-url" in paper and "--base-url" in live


# ---------------------------------------------------------------- no login at start

def test_start_does_not_run_the_broker_login(tmp_path):
    """Login is the sign-in's job. A second login hits the broker cooldown."""
    seen = {}

    def fake_start(*, kill_active, mode):
        seen["mode"] = mode
        return True, "started", 123

    rc = sen.run(root=tmp_path, mode="paper", start=fake_start, kill_active=False,
                 session_runner=lambda: {"usable": True, "action": "reused", "reason": ""})
    assert rc == 0 and seen == {"mode": "paper"}


def test_start_passes_the_mode_to_the_supervisor():
    recorded = {}

    class _Popen:
        def __init__(self, cmd, **kw):
            recorded["cmd"] = cmd
            self.pid = 4242

    original = eng.subprocess.Popen
    eng.subprocess.Popen = _Popen
    try:
        ok, message, pid = eng.start_engine(kill_active=False, mode="live",
                                            pid_path=Path(os.devnull + ".pid"),
                                            log_path=Path(os.devnull + ".log"))
    finally:
        eng.subprocess.Popen = original
        for p in (Path(os.devnull + ".pid"), Path(os.devnull + ".log")):
            p.unlink(missing_ok=True)
    assert ok and pid == 4242
    assert recorded["cmd"][-2:] == ["--mode", "live"]
    assert "live" in message


def test_unknown_mode_is_refused_before_spawning(tmp_path):
    ok, message, pid = eng.request_manual_start(kill_active=False, mode="yolo",
                                                pid_path=tmp_path / "e.pid",
                                                log_path=tmp_path / "m.log")
    assert ok is False and pid is None and "unknown mode" in message
    assert not (tmp_path / "m.log").exists()


def test_button_still_refused_when_kill_switch_on(tmp_path):
    ok, msg, pid = eng.request_manual_start(kill_active=True, pid_path=tmp_path / "e.pid",
                                            log_path=tmp_path / "m.log")
    assert ok is False and pid is None and "kill switch" in msg
    assert not (tmp_path / "m.log").exists()


def test_button_still_refused_when_bot_already_running(tmp_path):
    pid_file = tmp_path / "e.pid"
    pid_file.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    ok, msg, _ = eng.request_manual_start(kill_active=False, pid_path=pid_file,
                                          log_path=tmp_path / "m.log")
    assert ok is False and "already running" in msg


# ---------------------------------------------------------------- screener on every start

def test_screener_session_runs_on_every_start():
    calls = []
    sen.run(root=ROOT, start=lambda **kw: (True, "ok", 1), kill_active=False,
            session_runner=lambda: calls.append(1) or {"usable": True, "action": "logged in",
                                                       "reason": "saved"})
    assert calls == [1]


def test_a_refused_screener_session_does_not_block_the_bot():
    """The bot must start even with no screener session: it still has to manage exits."""
    rc = sen.run(root=ROOT, start=lambda **kw: (True, "ok", 1), kill_active=False,
                 session_runner=lambda: {"usable": False, "action": "failed",
                                         "reason": "credentials missing in .env"})
    assert rc == 0


def test_screener_session_failure_is_reported_not_raised():
    def boom():
        raise RuntimeError("screener.in is down")

    note = sen.ensure_screener_session(runner=boom)
    assert note["usable"] is False and "RuntimeError" in note["reason"]


# ---------------------------------------------------------------- morning boundary

def _marker(env_file: Path, written_at: float) -> None:
    marker = ss._marker_path(env_file)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(ss._ist_today(), encoding="utf-8")
    os.utime(marker, (written_at, written_at))


def test_a_session_saved_before_0900_ist_is_refreshed(monkeypatch, tmp_path):
    """"Screener session on every start when login is needed" (owner 2026-11-27).

    A cookie is a session cookie: a login at 08:00 must not be reused after the 09:00
    boundary just because the marker's date still reads today.
    """
    import datetime as dt
    import time

    monkeypatch.setenv(ss.SESSION_KEY, "abc")
    env_file = tmp_path / ".env"
    _marker(env_file, dt.datetime.combine(dt.date.today(), dt.time(8, 0)).timestamp())
    assert ss._saved_today(env_file) is False


def test_a_session_saved_after_0900_ist_is_reused(monkeypatch, tmp_path):
    import time

    monkeypatch.setenv(ss.SESSION_KEY, "abc")
    env_file = tmp_path / ".env"
    _marker(env_file, time.time())
    assert ss._saved_today(env_file) is True


def test_a_marker_without_a_cookie_never_counts(monkeypatch, tmp_path):
    import time

    monkeypatch.delenv(ss.SESSION_KEY, raising=False)
    env_file = tmp_path / ".env"
    _marker(env_file, time.time())
    assert ss._saved_today(env_file) is False
