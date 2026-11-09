"""Guard the live path: every live start must go through the supervisor.

Offline checks only (no broker, no openalgo import).
"""
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUN_POV = REPO / "scripts" / "run" / "run_pov.py"
INSTALLER = REPO / "Servers" / "install_ubuntu.sh"


def test_run_pov_refuses_to_run_without_supervisor():
    proc = subprocess.run([sys.executable, str(RUN_POV)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2, proc.stderr
    assert "refused" in proc.stderr
    assert "run_supervised.py" in proc.stderr


def test_ubuntu_pov_runner_starts_through_supervisor():
    text = INSTALLER.read_text(encoding="utf-8")
    unit = re.search(r"\[Service\].*?ExecStart=([^\n]+)", text.split("pov-runner.service")[1], re.S)
    assert unit, "pov-runner ExecStart not found"
    exec_start = unit.group(1)
    assert "run_supervised.py" in exec_start
    assert "run_pov.py" not in exec_start
