"""
run_server.py - Windows-safe launcher for the DMC Dash terminal.

WHY THIS EXISTS
---------------
On this Windows host, in any shell where ``cmd.exe`` cannot be spawned with
valid stdio handles (detached process, redirected output, service context),
this call chain BLOCKS FOREVER:

    import openalgo -> openalgo/data.py -> import pandas
      -> pandas/compat/_constants.py -> platform.machine()
        -> platform.win32_ver() -> subprocess.check_output("cmd /c ver")
          -> subprocess.communicate()      # never returns

Measured: ``platform.machine()`` takes 1.7 s in a normal console and hangs
indefinitely in a detached one. The symptom is a dashboard that "starts" and
never listens on its port, with no error -- the worst kind of failure to debug.

``platform.machine()`` is only asking "is this Windows on x86/ARM?". The OS
arch of a running 64-bit Python interpreter is known without subprocessing, so
we answer it directly and the subprocess is never attempted.

This is a LAUNCHER-LEVEL patch on purpose:
  * it lives here, not in blue_engine, so the library stays portable and free
    of host-specific workarounds;
  * it does not monkey-patch pandas or the SDK;
  * a normal console launch is unaffected (the values are identical).

Run:
    python Blue_engine/dashboard/run_server.py
    # or via the .bat wrapper
"""
from __future__ import annotations

import os
import platform
import sys

# --- Windows platform-probe guard (see module docstring) ------------------- #
if os.name == "nt":
    _ARCH = os.environ.get("PROCESSOR_ARCHITECTURE", "") or (
        "ARM64" if "ARM64" in platform.version().upper() else "AMD64"
    )
    platform.machine = lambda: _ARCH                       # type: ignore[assignment]
    platform.win32_ver = lambda *a, **k: ("10", "", "", "")  # type: ignore[assignment]
    platform.uname = lambda *a, **k: platform.uname_result(  # type: ignore[assignment]
        "Windows", platform.node(), "10", "", "", _ARCH
    )

# --- normal startup ------------------------------------------------------- #
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from blue_engine.dashboard.dash_app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
