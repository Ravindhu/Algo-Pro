"""Flow stage: Algo router (engine control, dashboard button).
Status: TESTS for the engine stop button. Stop ends the engine process only.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from quant.core import engine_control


def test_stop_refuses_when_no_engine_running(tmp_path):
    ok, message, pid = engine_control.stop_engine(pid_path=tmp_path / "engine.pid")
    assert not ok
    assert pid is None
    assert "no engine" in message


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX signals")
def test_stop_terminates_only_the_engine_pid_and_removes_pid_file(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pid_path = tmp_path / "engine.pid"
    pid_path.write_text(json.dumps({"pid": child.pid, "started": int(time.time())}), encoding="utf-8")
    try:
        ok, message, pid = engine_control.stop_engine(pid_path=pid_path)
        assert ok, message
        assert pid == child.pid
        assert child.wait(timeout=10) is not None      # the engine process ended
        assert not pid_path.exists()                    # PID file removed
        assert "not changed" in message                 # broker and positions are not touched
    finally:
        if child.poll() is None:
            child.kill()


def test_stop_does_not_touch_kill_switch_or_broker(tmp_path, monkeypatch):
    """Stop must never reach the kill switch, the broker client or the order path."""
    calls = []
    monkeypatch.setattr(engine_control, "running_pid", lambda pid_path=None: None)
    ok, _, _ = engine_control.stop_engine(pid_path=tmp_path / "x.pid")
    assert not ok and calls == []
    import inspect
    src = inspect.getsource(engine_control.stop_engine)
    for forbidden in ("KillSwitch", "logout", "placeorder", "cancel", "close_position"):
        assert forbidden not in src
