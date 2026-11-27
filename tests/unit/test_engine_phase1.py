"""Phase 1 live blockers (2026-11-27): one bot, a reliable stop.

Covers audit items 8 and 20 (duplicate bot from a stale PID; wrapper PID recorded),
item 13 (stop unlinks the PID file without confirming exit) and item 1 (one spawn path).
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from quant.core import engine_control as eng

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="uses POSIX signals and process groups")


class _FakePopen:
    spawned = []

    def __init__(self, cmd, **kw):
        _FakePopen.spawned.append(cmd)
        self.pid = 5151


@pytest.fixture
def fake_spawn(monkeypatch):
    _FakePopen.spawned = []
    monkeypatch.setattr(eng.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(eng, "running_pid", lambda *a, **k: None)
    return _FakePopen


def test_button_writes_the_pid_file_at_once_with_the_supervisor_pid(tmp_path, fake_spawn):
    pid_file = tmp_path / "engine.pid"
    ok, msg, pid = eng.request_manual_start(kill_active=False, mode="paper", pid_path=pid_file,
                                            log_path=tmp_path / "m.log")
    assert ok and pid == 5151
    # The PID file exists as soon as the button returns (no screener login in between).
    assert json.loads(pid_file.read_text(encoding="utf-8"))["pid"] == 5151
    # One spawn path: the supervisor, never the old start_engine_now runner.
    cmd = fake_spawn.spawned[-1]
    assert cmd[1] == str(eng.SUPERVISOR)
    assert "start_engine_now" not in " ".join(cmd)
    assert cmd[-2:] == ["--mode", "paper"]


def test_second_start_is_refused_while_a_start_holds_the_lock(tmp_path, fake_spawn):
    pid_file = tmp_path / "engine.pid"
    lock = tmp_path / "engine.lock"
    lock.write_text(str(os.getpid()), encoding="utf-8")   # a live holder, mid-start
    ok, msg, pid = eng.request_manual_start(kill_active=False, pid_path=pid_file,
                                            log_path=tmp_path / "m.log")
    assert ok is False and "already in progress" in msg
    assert fake_spawn.spawned == []                       # no second bot
    assert lock.exists()                                  # someone else's lock is not removed


def test_lock_is_released_after_a_start(tmp_path, fake_spawn):
    eng.request_manual_start(kill_active=False, pid_path=tmp_path / "engine.pid",
                             log_path=tmp_path / "m.log")
    assert not (tmp_path / "engine.lock").exists()


def test_stale_lock_from_a_dead_process_is_cleared(tmp_path, fake_spawn):
    lock = tmp_path / "engine.lock"
    lock.write_text("999999999", encoding="utf-8")        # no such process
    ok, _, pid = eng.request_manual_start(kill_active=False, pid_path=tmp_path / "engine.pid",
                                          log_path=tmp_path / "m.log")
    assert ok and pid == 5151


def test_refusal_takes_no_lock_and_writes_nothing(tmp_path, fake_spawn):
    ok, msg, _ = eng.request_manual_start(kill_active=True, pid_path=tmp_path / "engine.pid",
                                          log_path=tmp_path / "m.log")
    assert ok is False and "kill switch" in msg
    assert not (tmp_path / "engine.lock").exists()
    assert not (tmp_path / "engine.pid").exists()


@POSIX_ONLY
def test_stop_keeps_the_pid_file_when_the_engine_survives_sigterm(tmp_path):
    # An engine that ignores SIGTERM must NOT be reported as stopped.
    child = subprocess.Popen([sys.executable, "-c",
                              "import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                              "print('ready', flush=True); time.sleep(60)"],
                             stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "ready"   # handler is installed before SIGTERM
    pid_path = tmp_path / "engine.pid"
    pid_path.write_text(json.dumps({"pid": child.pid, "started": int(time.time())}), encoding="utf-8")
    try:
        ok, message, pid = eng.stop_engine(pid_path=pid_path, wait_seconds=1.0)
        assert ok is False
        assert pid == child.pid
        assert pid_path.exists()                          # still shown as running
        assert "NOT confirmed stopped" in message
        # And a second start stays refused while it is alive.
        assert eng.running_pid(pid_path) == child.pid
    finally:
        child.kill()
        child.wait(timeout=10)


@POSIX_ONLY
def test_stop_signals_the_whole_group_for_a_group_leader(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(eng.os, "killpg", lambda pid, sig: calls.append(("killpg", pid, sig)))
    monkeypatch.setattr(eng.os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    monkeypatch.setattr(eng.os, "getpgid", lambda pid: pid)          # pid leads its group
    monkeypatch.setattr(eng, "_wait_for_exit", lambda pid, timeout: True)
    monkeypatch.setattr(eng, "running_pid", lambda *a, **k: 777)
    ok, _, _ = eng.stop_engine(pid_path=tmp_path / "engine.pid")
    assert ok
    assert calls == [("killpg", 777, signal.SIGTERM)]


@POSIX_ONLY
def test_stop_never_signals_a_group_it_did_not_start(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(eng.os, "killpg", lambda pid, sig: calls.append(("killpg", pid, sig)))
    monkeypatch.setattr(eng.os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    monkeypatch.setattr(eng.os, "getpgid", lambda pid: 1)            # someone else's group
    monkeypatch.setattr(eng, "_wait_for_exit", lambda pid, timeout: True)
    monkeypatch.setattr(eng, "running_pid", lambda *a, **k: 777)
    eng.stop_engine(pid_path=tmp_path / "engine.pid")
    assert calls == [("kill", 777, signal.SIGTERM)]


def test_stop_does_not_remove_pid_file_before_exit_is_confirmed(tmp_path, monkeypatch):
    pid_path = tmp_path / "engine.pid"
    pid_path.write_text(json.dumps({"pid": 777}), encoding="utf-8")
    monkeypatch.setattr(eng, "running_pid", lambda *a, **k: 777)
    monkeypatch.setattr(eng, "_signal_engine", lambda pid, sig: None)
    monkeypatch.setattr(eng, "_wait_for_exit", lambda pid, timeout: False)
    ok, _, _ = eng.stop_engine(pid_path=pid_path, wait_seconds=0.1)
    assert ok is False and pid_path.exists()
